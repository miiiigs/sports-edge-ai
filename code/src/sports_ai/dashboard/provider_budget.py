"""Provider request budget: paid-provider gate, persistent cache, quota governor, and circuit breaker.

This module is the single enforcement point for paid API-Football request
consumption. Every paid HTTP entry point in the API-Football fixture and odds
providers must pass through :class:`ProviderRequestBudget` so that the master
gate, the persistent request cache, the quota governor, and the circuit breaker
cannot be bypassed by any application path (startup sync, scheduler, manual
refresh, statistics, odds, fixture lookup).

State is persisted in the operational dashboard SQLite database so it survives
process restarts and Railway redeploys. No secrets or authorization headers are
ever persisted.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from ..locking import canonical_hash
from .models import parse_iso_datetime, utc_now_iso

PROVIDER_API_FOOTBALL = "api-football"

# Paid-provider master gate
ENV_ALLOW_PAID_PROVIDER_CALLS = "SPORTS_AI_ALLOW_PAID_PROVIDER_CALLS"
ENV_API_FOOTBALL_DAILY_BUDGET = "SPORTS_AI_API_FOOTBALL_DAILY_BUDGET"
ENV_QUOTA_RESET_HOUR_UTC = "SPORTS_AI_API_FOOTBALL_QUOTA_RESET_HOUR_UTC"

# Request priorities (used by the quota governor)
PRIORITY_ESSENTIAL = "ESSENTIAL"
PRIORITY_NORMAL = "NORMAL"
PRIORITY_LOW = "LOW"

# Quota governor states
QUOTA_UNKNOWN = "UNKNOWN"
QUOTA_HEALTHY = "HEALTHY"
QUOTA_CONSERVE = "CONSERVE"
QUOTA_LOW = "LOW"
QUOTA_CRITICAL = "CRITICAL"
QUOTA_EXHAUSTED = "EXHAUSTED"

# Circuit breaker states
CIRCUIT_CLOSED = "CLOSED"
CIRCUIT_RATE_LIMITED = "RATE_LIMITED"
CIRCUIT_QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
CIRCUIT_AUTH_ERROR = "AUTH_ERROR"
CIRCUIT_PROVIDER_ERROR = "PROVIDER_ERROR"

# Decision actions returned by ProviderRequestBudget.issue()
ACTION_CACHE_HIT = "CACHE_HIT"
ACTION_ALLOW = "ALLOW"
ACTION_BLOCKED_GATE = "PAID_PROVIDER_CALLS_DISABLED"
ACTION_BLOCKED_QUOTA = "QUOTA_GOVERNOR_BLOCKED"
ACTION_BLOCKED_CIRCUIT = "CIRCUIT_BREAKER_BLOCKED"
ACTION_BLOCKED_COALESCED = "COALESCED_MISS"

# Quota governor thresholds (fraction of the daily allowance remaining)
QUOTA_CONSERVE_THRESHOLD = 0.40
QUOTA_LOW_THRESHOLD = 0.20
QUOTA_CRITICAL_THRESHOLD = 0.10

# Default cache TTLs (seconds) used when a caller has no lifecycle context.
DEFAULT_TTL_LIVE = 90
DEFAULT_TTL_FIXTURE_DETAIL = 300
DEFAULT_TTL_STATISTICS = 300
DEFAULT_TTL_MAPPING = 12 * 60 * 60
DEFAULT_TTL_ODDS_FAR = 2 * 60 * 60
DEFAULT_TTL_SCHEDULE_FAR = 8 * 60 * 60
DEFAULT_TTL_SCHEDULE_NEXT_DAY = 2 * 60 * 60
DEFAULT_TTL_SCHEDULE_TODAY = 30 * 60

# Circuit-breaker backoff (seconds) base and caps.
RATE_LIMIT_BACKOFF_BASE_SECONDS = 60
RATE_LIMIT_BACKOFF_CAP_SECONDS = 3600
PROVIDER_ERROR_BACKOFF_BASE_SECONDS = 30
PROVIDER_ERROR_BACKOFF_CAP_SECONDS = 900
AUTH_ERROR_BACKOFF_SECONDS = 3600
QUOTA_EXHAUSTED_FALLBACK_BLOCK_SECONDS = 24 * 60 * 60

# How long an in-flight coalesced waiter waits before giving up.
INFLIGHT_WAIT_TIMEOUT_SECONDS = 30.0

# Candidate API-Football quota/rate-limit response headers (case-insensitive).
_QUOTA_HEADER_ALIASES = {
    "daily_limit": ("x-ratelimit-limit", "x-ratelimit-day-limit"),
    "daily_remaining": ("x-ratelimit-remaining", "x-ratelimit-day-remaining"),
    "minute_limit": ("x-ratelimit-requests-limit", "x-ratelimit-minute-limit"),
    "minute_remaining": ("x-ratelimit-requests-remaining", "x-ratelimit-minute-remaining"),
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int | None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def request_key(provider: str, endpoint: str, params: Mapping[str, Any] | None) -> str:
    """Deterministic canonical key for one provider endpoint + normalized params."""
    normalized = {str(key): value for key, value in sorted((params or {}).items())}
    return canonical_hash(
        {
            "provider": provider,
            "endpoint": endpoint,
            "params": normalized,
        }
    )


def parse_quota_headers(headers: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract supported quota/rate-limit headers into a small safe dict."""
    result = {
        "daily_limit": None,
        "daily_remaining": None,
        "minute_limit": None,
        "minute_remaining": None,
        "raw": {},
    }
    if not headers:
        return result
    lowered = {str(key).lower(): str(value) for key, value in headers.items()}
    for field, aliases in _QUOTA_HEADER_ALIASES.items():
        for alias in aliases:
            if alias in lowered:
                try:
                    result[field] = int(float(lowered[alias]))
                except (TypeError, ValueError):
                    result[field] = None
                break
    for key, value in lowered.items():
        if key.startswith("x-ratelimit"):
            result["raw"][key] = value
    return result


def classify_provider_response(
    *,
    http_status: int,
    payload: dict[str, Any] | None,
    response_headers: Mapping[str, Any] | None,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Classify an API-Football response into (circuit_state, message, quota).

    Returns circuit_state ``None`` when the response is NOT a global provider
    failure (for example a 200 with empty or per-fixture market data). This is
    deliberately separate from per-fixture ``MARKET_NOT_AVAILABLE`` handling.
    """
    quota = parse_quota_headers(response_headers)
    errors = {}
    if isinstance(payload, dict):
        errors_payload = payload.get("errors")
        if isinstance(errors_payload, dict):
            errors = errors_payload
        elif isinstance(errors_payload, str):
            errors = {"errors": errors_payload}
    error_text = " ".join(str(value) for value in errors.values()).lower()

    if http_status in (401, 403):
        return CIRCUIT_AUTH_ERROR, f"HTTP {http_status}: authentication failed", quota
    if http_status == 429:
        return CIRCUIT_RATE_LIMITED, "HTTP 429: rate limited", quota
    if "request limit" in error_text or "quota" in error_text:
        return CIRCUIT_QUOTA_EXHAUSTED, error_text or "daily request limit reached", quota
    if http_status >= 500:
        return CIRCUIT_PROVIDER_ERROR, f"HTTP {http_status}: provider error", quota
    return None, None, quota


def schedule_fixture_policy(target_date, now_local_date) -> tuple[int, str]:
    """Cache TTL + priority for a league/date schedule fetch by date distance."""
    delta_days = (target_date - now_local_date).days
    if delta_days <= 0:
        return DEFAULT_TTL_SCHEDULE_TODAY, PRIORITY_NORMAL
    if delta_days == 1:
        return DEFAULT_TTL_SCHEDULE_NEXT_DAY, PRIORITY_LOW
    return DEFAULT_TTL_SCHEDULE_FAR, PRIORITY_LOW


def odds_policy_for_kickoff(kickoff_utc: str | None, *, now_utc: datetime | None = None) -> tuple[int, str]:
    """Cache TTL + priority for a per-fixture odds fetch by time to kickoff."""
    if not kickoff_utc:
        return DEFAULT_TTL_ODDS_FAR, PRIORITY_LOW
    try:
        kickoff = parse_iso_datetime(kickoff_utc)
    except ValueError:
        return DEFAULT_TTL_ODDS_FAR, PRIORITY_LOW
    reference = now_utc or datetime.now(timezone.utc)
    delta = kickoff - reference
    if delta <= timedelta(minutes=15):
        return 180, PRIORITY_ESSENTIAL
    if delta <= timedelta(minutes=60):
        return 420, PRIORITY_ESSENTIAL
    if delta <= timedelta(hours=6):
        return 1200, PRIORITY_NORMAL
    return DEFAULT_TTL_ODDS_FAR, PRIORITY_LOW


@dataclass(frozen=True)
class BudgetDecision:
    allow: bool
    action: str
    reason: str | None = None
    cached_payload: dict[str, Any] | None = None
    quota_state: str = QUOTA_UNKNOWN
    request_key: str | None = None
    provider: str | None = None


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS provider_request_cache (
        request_key TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        normalized_params_key TEXT NOT NULL,
        response_json TEXT NOT NULL,
        provider_timestamp TEXT,
        created_at TEXT NOT NULL,
        last_success_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        next_eligible_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_quota_state (
        provider TEXT PRIMARY KEY,
        state_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_circuit_state (
        provider TEXT PRIMARY KEY,
        current_state TEXT NOT NULL,
        failure_count INTEGER NOT NULL DEFAULT 0,
        blocked_until TEXT,
        last_error TEXT,
        last_error_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_budget_metrics (
        provider TEXT NOT NULL,
        metric TEXT NOT NULL,
        value INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (provider, metric)
    )
    """,
)


class ProviderRequestBudget:
    """Enforces the paid-provider gate, persistent cache, quota governor, and breaker."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        allow_paid_provider_calls: bool | None = None,
        daily_budget: int | None = None,
        quota_reset_hour_utc: int = 0,
    ) -> None:
        self._db_path = Path(db_path)
        if allow_paid_provider_calls is None:
            allow_paid_provider_calls = _env_bool(ENV_ALLOW_PAID_PROVIDER_CALLS, False)
        self._allow = bool(allow_paid_provider_calls)
        if daily_budget is None:
            daily_budget = _env_int(ENV_API_FOOTBALL_DAILY_BUDGET, None)
        self._daily_budget = daily_budget
        if quota_reset_hour_utc is None:
            quota_reset_hour_utc = _env_int(ENV_QUOTA_RESET_HOUR_UTC, 0) or 0
        self._quota_reset_hour_utc = int(quota_reset_hour_utc)
        self._lock = threading.Lock()
        self._inflight: dict[str, dict[str, Any]] = {}
        self._ensure_schema()

    # ------------------------------------------------------------------ state
    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)

    def allow_paid_provider_calls(self) -> bool:
        return self._allow

    def daily_budget(self) -> int | None:
        return self._daily_budget

    def quota_reset_hour_utc(self) -> int:
        return self._quota_reset_hour_utc

    # ----------------------------------------------------------------- metrics
    def _bump(self, metric: str, *, delta: int = 1) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_budget_metrics (provider, metric, value)
                VALUES (?, ?, ?)
                ON CONFLICT(provider, metric) DO UPDATE SET value = value + excluded.value
                """,
                (PROVIDER_API_FOOTBALL, metric, delta),
            )

    def metrics(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT metric, value FROM provider_budget_metrics WHERE provider = ?",
                (PROVIDER_API_FOOTBALL,),
            ).fetchall()
        return {str(row["metric"]): int(row["value"]) for row in rows}

    # -------------------------------------------------------------- quota state
    def _quota_snapshot(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM provider_quota_state WHERE provider = ?",
                (PROVIDER_API_FOOTBALL,),
            ).fetchone()
        if row is None:
            return {}
        try:
            return json.loads(str(row["state_json"]))
        except (TypeError, ValueError):
            return {}

    def _persist_quota(self, snapshot: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_quota_state (provider, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (PROVIDER_API_FOOTBALL, json.dumps(snapshot, sort_keys=True), utc_now_iso()),
            )

    def _compute_quota_reset_at(self, now_utc: datetime) -> str:
        hour = max(0, min(23, self._quota_reset_hour_utc))
        reset_at = now_utc.replace(hour=hour, minute=0, second=0, microsecond=0)
        if reset_at <= now_utc:
            reset_at += timedelta(days=1)
        return reset_at.isoformat(timespec="seconds")

    def quota_snapshot(self) -> dict[str, Any]:
        """Return a read-only snapshot of persisted quota + governor state."""
        snapshot = dict(self._quota_snapshot())
        snapshot["state"] = self.quota_state()
        return snapshot

    def quota_state(self) -> str:
        snapshot = self._quota_snapshot()
        remaining = snapshot.get("daily_remaining")
        limit = snapshot.get("daily_limit")
        if remaining is None and limit is None and self._daily_budget:
            limit = self._daily_budget
            remaining = max(0, limit - int(snapshot.get("requests_used") or 0))
        if remaining is None:
            return QUOTA_UNKNOWN
        try:
            remaining = int(remaining)
            limit_value = int(limit) if limit else None
        except (TypeError, ValueError):
            return QUOTA_UNKNOWN
        if remaining <= 0:
            reset_at = snapshot.get("quota_reset_at")
            if reset_at:
                try:
                    if parse_iso_datetime(str(reset_at)) <= datetime.now(timezone.utc):
                        return QUOTA_UNKNOWN
                except ValueError:
                    pass
            return QUOTA_EXHAUSTED
        if not limit_value or limit_value <= 0:
            return QUOTA_UNKNOWN
        ratio = remaining / float(limit_value)
        if ratio < QUOTA_CRITICAL_THRESHOLD:
            return QUOTA_CRITICAL
        if ratio < QUOTA_LOW_THRESHOLD:
            return QUOTA_LOW
        if ratio < QUOTA_CONSERVE_THRESHOLD:
            return QUOTA_CONSERVE
        return QUOTA_HEALTHY

    def _priority_allowed(self, priority: str, quota_state: str) -> bool:
        if quota_state == QUOTA_EXHAUSTED:
            return False
        if quota_state in {QUOTA_UNKNOWN, QUOTA_CONSERVE}:
            return priority != PRIORITY_LOW
        if quota_state == QUOTA_LOW:
            return priority in {PRIORITY_ESSENTIAL, PRIORITY_NORMAL}
        if quota_state == QUOTA_CRITICAL:
            return priority == PRIORITY_ESSENTIAL
        return True  # HEALTHY

    def _record_quota_from_headers(self, headers: Mapping[str, Any] | None) -> None:
        parsed = parse_quota_headers(headers)
        if not any(
            parsed[key] is not None
            for key in ("daily_limit", "daily_remaining", "minute_limit", "minute_remaining")
        ):
            return
        now_utc = datetime.now(timezone.utc)
        snapshot = self._quota_snapshot()
        snapshot["provider"] = PROVIDER_API_FOOTBALL
        for key in ("daily_limit", "daily_remaining", "minute_limit", "minute_remaining"):
            if parsed[key] is not None:
                snapshot[key] = parsed[key]
        snapshot["captured_at"] = utc_now_iso()
        snapshot["quota_reset_at"] = self._compute_quota_reset_at(now_utc)
        snapshot["last_request_at"] = utc_now_iso()
        self._persist_quota(snapshot)

    # --------------------------------------------------------------- circuit
    def _circuit_row(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT current_state, failure_count, blocked_until, last_error, last_error_at FROM provider_circuit_state WHERE provider = ?",
                (PROVIDER_API_FOOTBALL,),
            ).fetchone()
        if row is None:
            return None
        return {
            "current_state": str(row["current_state"]),
            "failure_count": int(row["failure_count"]),
            "blocked_until": row["blocked_until"],
            "last_error": row["last_error"],
            "last_error_at": row["last_error_at"],
        }

    def _circuit_blocked(self, now_iso: str) -> str | None:
        row = self._circuit_row()
        if row is None or row["current_state"] == CIRCUIT_CLOSED:
            return None
        blocked_until = row["blocked_until"]
        if blocked_until:
            try:
                if parse_iso_datetime(blocked_until) > parse_iso_datetime(now_iso):
                    return str(row["current_state"])
            except ValueError:
                pass
        # Backoff expired: reopen the circuit.
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE provider_circuit_state
                SET current_state = ?, failure_count = 0, blocked_until = NULL,
                    last_error = NULL, updated_at = ?
                WHERE provider = ?
                """,
                (CIRCUIT_CLOSED, now_iso, PROVIDER_API_FOOTBALL),
            )
        return None

    def _record_failure(self, state: str, message: str | None) -> None:
        now_utc = datetime.now(timezone.utc)
        now_iso = utc_now_iso()
        row = self._circuit_row()
        failure_count = (row or {}).get("failure_count", 0)
        blocked_until: datetime | None = None
        if state == CIRCUIT_RATE_LIMITED:
            failure_count = int(failure_count) + 1
            backoff = min(
                RATE_LIMIT_BACKOFF_CAP_SECONDS,
                RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** (failure_count - 1)),
            )
            blocked_until = now_utc + timedelta(seconds=backoff)
        elif state == CIRCUIT_AUTH_ERROR:
            failure_count = int(failure_count) + 1
            blocked_until = now_utc + timedelta(seconds=AUTH_ERROR_BACKOFF_SECONDS)
        elif state == CIRCUIT_PROVIDER_ERROR:
            failure_count = int(failure_count) + 1
            backoff = min(
                PROVIDER_ERROR_BACKOFF_CAP_SECONDS,
                PROVIDER_ERROR_BACKOFF_BASE_SECONDS * (2 ** (failure_count - 1)),
            )
            blocked_until = now_utc + timedelta(seconds=backoff)
        elif state == CIRCUIT_QUOTA_EXHAUSTED:
            snapshot = self._quota_snapshot()
            reset_at = snapshot.get("quota_reset_at")
            if reset_at:
                try:
                    blocked_until = parse_iso_datetime(str(reset_at))
                except ValueError:
                    blocked_until = None
            if blocked_until is None:
                blocked_until = now_utc + timedelta(seconds=QUOTA_EXHAUSTED_FALLBACK_BLOCK_SECONDS)
        else:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_circuit_state (provider, current_state, failure_count, blocked_until, last_error, last_error_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    current_state = excluded.current_state,
                    failure_count = excluded.failure_count,
                    blocked_until = excluded.blocked_until,
                    last_error = excluded.last_error,
                    last_error_at = excluded.last_error_at,
                    updated_at = excluded.updated_at
                """,
                (
                    PROVIDER_API_FOOTBALL,
                    state,
                    int(failure_count),
                    None if blocked_until is None else blocked_until.isoformat(timespec="seconds"),
                    message,
                    now_iso,
                    now_iso,
                ),
            )

    def circuit_snapshot(self) -> dict[str, Any]:
        row = self._circuit_row()
        return {
            "state": "CLOSED" if row is None else row["current_state"],
            "failure_count": 0 if row is None else row["failure_count"],
            "blocked_until": None if row is None else row["blocked_until"],
            "last_error": None if row is None else row["last_error"],
            "last_error_at": None if row is None else row["last_error_at"],
        }

    # ------------------------------------------------------------------ cache
    def _cache_get(self, key: str, ttl_seconds: int, now_iso: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response_json, expires_at FROM provider_request_cache WHERE request_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        expires_at = row["expires_at"]
        if expires_at and expires_at <= now_iso:
            return None
        try:
            return json.loads(str(row["response_json"]))
        except (TypeError, ValueError):
            return None

    def _cache_set(
        self,
        key: str,
        payload: dict[str, Any],
        *,
        endpoint: str,
        params: Mapping[str, Any] | None,
        provider_timestamp: str | None,
        ttl_seconds: int,
    ) -> None:
        now_iso = utc_now_iso()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(1, int(ttl_seconds)))
        ).isoformat(timespec="seconds")
        normalized_params_key = json.dumps(
            {"endpoint": endpoint, "params": {str(k): v for k, v in sorted((params or {}).items())}},
            sort_keys=True,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_request_cache (
                    request_key, provider, endpoint, normalized_params_key,
                    response_json, provider_timestamp, created_at, last_success_at,
                    expires_at, next_eligible_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_key) DO UPDATE SET
                    response_json = excluded.response_json,
                    provider_timestamp = excluded.provider_timestamp,
                    last_success_at = excluded.last_success_at,
                    expires_at = excluded.expires_at,
                    next_eligible_at = excluded.next_eligible_at
                """,
                (
                    key,
                    PROVIDER_API_FOOTBALL,
                    endpoint,
                    normalized_params_key,
                    json.dumps(payload, sort_keys=True),
                    provider_timestamp,
                    now_iso,
                    now_iso,
                    expires_at,
                    expires_at,
                ),
            )

    def cache_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM provider_request_cache").fetchone()[0]
        return {"entries": int(total)}

    # ------------------------------------------------------------- in-flight
    def _inflight_begin(self, key: str) -> bool:
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight[key] = {"event": threading.Event(), "result": None}
            return True

    def _inflight_wait(self, key: str) -> None:
        with self._lock:
            entry = self._inflight.get(key)
        if entry is None:
            return
        entry["event"].wait(INFLIGHT_WAIT_TIMEOUT_SECONDS)

    def _inflight_finish(self, key: str) -> None:
        with self._lock:
            entry = self._inflight.pop(key, None)
        if entry is not None:
            entry["event"].set()

    # ------------------------------------------------------------------ issue
    def issue(
        self,
        *,
        provider: str,
        endpoint: str,
        params: Mapping[str, Any] | None,
        ttl_seconds: int,
        priority: str,
    ) -> BudgetDecision:
        key = request_key(provider, endpoint, params)
        now_iso = utc_now_iso()
        if not self._allow:
            self._bump("requests_blocked_gate")
            return BudgetDecision(
                allow=False,
                action=ACTION_BLOCKED_GATE,
                reason="PAID_PROVIDER_CALLS_DISABLED: set SPORTS_AI_ALLOW_PAID_PROVIDER_CALLS=1 to enable paid provider polling",
                quota_state=self.quota_state(),
                request_key=key,
                provider=provider,
            )

        circuit_state = self._circuit_blocked(now_iso)
        if circuit_state is not None:
            self._bump("requests_blocked_circuit")
            return BudgetDecision(
                allow=False,
                action=ACTION_BLOCKED_CIRCUIT,
                reason=f"provider circuit {circuit_state} open",
                quota_state=self.quota_state(),
                request_key=key,
                provider=provider,
            )

        quota_state = self.quota_state()
        if not self._priority_allowed(priority, quota_state):
            self._bump("requests_blocked_governor")
            return BudgetDecision(
                allow=False,
                action=ACTION_BLOCKED_QUOTA,
                reason=f"quota state {quota_state} suppresses priority {priority}",
                quota_state=quota_state,
                request_key=key,
                provider=provider,
            )

        cached = self._cache_get(key, ttl_seconds, now_iso)
        if cached is not None:
            self._bump("requests_cached")
            return BudgetDecision(
                allow=False,
                action=ACTION_CACHE_HIT,
                reason="persistent cache hit",
                cached_payload=cached,
                quota_state=quota_state,
                request_key=key,
                provider=provider,
            )

        if self._inflight_begin(key):
            self._bump("requests_inflight_owner")
            return BudgetDecision(
                allow=True,
                action=ACTION_ALLOW,
                quota_state=quota_state,
                request_key=key,
                provider=provider,
            )

        # Another application path is already fetching this exact key.
        self._bump("requests_inflight_waiter")
        self._inflight_wait(key)
        cached = self._cache_get(key, ttl_seconds, utc_now_iso())
        if cached is not None:
            self._bump("requests_cached")
            return BudgetDecision(
                allow=False,
                action=ACTION_CACHE_HIT,
                reason="persistent cache hit after coalesced wait",
                cached_payload=cached,
                quota_state=self.quota_state(),
                request_key=key,
                provider=provider,
            )
        self._bump("requests_blocked_coalesced")
        return BudgetDecision(
            allow=False,
            action=ACTION_BLOCKED_COALESCED,
            reason="coalesced request produced no cached response",
            quota_state=self.quota_state(),
            request_key=key,
            provider=provider,
        )

    def finish_success(
        self,
        *,
        provider: str,
        endpoint: str,
        params: Mapping[str, Any] | None,
        payload: dict[str, Any],
        provider_timestamp: str | None,
        ttl_seconds: int,
        response_headers: Mapping[str, Any] | None,
    ) -> None:
        key = request_key(provider, endpoint, params)
        self._cache_set(
            key,
            payload,
            endpoint=endpoint,
            params=params,
            provider_timestamp=provider_timestamp,
            ttl_seconds=ttl_seconds,
        )
        self._record_quota_from_headers(response_headers)
        self._record_request_result(success=True)
        self._bump("http_requests")
        self._inflight_finish(key)

    def finish_failure(
        self,
        *,
        provider: str,
        endpoint: str,
        params: Mapping[str, Any] | None,
        state: str | None,
        message: str | None,
        response_headers: Mapping[str, Any] | None = None,
    ) -> None:
        key = request_key(provider, endpoint, params)
        if response_headers:
            self._record_quota_from_headers(response_headers)
        if state is not None:
            self._record_failure(state, message)
        self._record_request_result(success=False)
        self._bump("http_requests")
        self._inflight_finish(key)

    def _record_request_result(self, *, success: bool) -> None:
        snapshot = self._quota_snapshot()
        snapshot["last_request_at"] = utc_now_iso()
        if success:
            snapshot["last_success_at"] = utc_now_iso()
        snapshot["requests_used"] = int(snapshot.get("requests_used") or 0) + 1
        if success:
            snapshot["requests_success"] = int(snapshot.get("requests_success") or 0) + 1
        self._persist_quota(snapshot)

    # ----------------------------------------------------------- observability
    def snapshot(self) -> dict[str, Any]:
        metrics = self.metrics()
        quota = self.quota_snapshot()
        circuit = self.circuit_snapshot()
        cache = self.cache_stats()
        return {
            "paid_provider_calls_allowed": "YES" if self._allow else "NO",
            "provider": PROVIDER_API_FOOTBALL,
            "gate": {
                "env_var": ENV_ALLOW_PAID_PROVIDER_CALLS,
                "allowed": self._allow,
                "default": False,
            },
            "quota": {
                "state": quota.get("state"),
                "daily_limit": quota.get("daily_limit"),
                "daily_remaining": quota.get("daily_remaining"),
                "minute_limit": quota.get("minute_limit"),
                "minute_remaining": quota.get("minute_remaining"),
                "captured_at": quota.get("captured_at"),
                "quota_reset_at": quota.get("quota_reset_at"),
                "last_request_at": quota.get("last_request_at"),
                "last_success_at": quota.get("last_success_at"),
                "requests_used": quota.get("requests_used"),
                "configured_daily_budget": self._daily_budget,
                "quota_reset_hour_utc": self._quota_reset_hour_utc,
            },
            "circuit": circuit,
            "cache": cache,
            "metrics": {
                "provider_http_requests": int(metrics.get("http_requests") or 0),
                "requests_cached": int(metrics.get("requests_cached") or 0),
                "requests_blocked_gate": int(metrics.get("requests_blocked_gate") or 0),
                "requests_blocked_governor": int(metrics.get("requests_blocked_governor") or 0),
                "requests_blocked_circuit": int(metrics.get("requests_blocked_circuit") or 0),
                "requests_blocked_coalesced": int(metrics.get("requests_blocked_coalesced") or 0),
                "requests_inflight_owner": int(metrics.get("requests_inflight_owner") or 0),
                "requests_inflight_waiter": int(metrics.get("requests_inflight_waiter") or 0),
            },
        }
