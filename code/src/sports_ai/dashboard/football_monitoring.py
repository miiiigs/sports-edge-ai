"""Read-only Football monitoring contract for the unified Sports Edge dashboard.

One aggregate JSON document (``football_monitoring_v1``) answering:

* **operational** - is the system running? scheduler heartbeats, prediction
  coverage of upcoming fixtures, READY fixtures without a pick, settlement lag,
  odds freshness, and derived alerts;
* **model_performance** - which forward Model Picks were right or wrong:
  stake-weighted win share with Wilson CIs by market, competition and
  confidence band, calibration (mean q vs realised), model-version lineage,
  high-confidence misses; retrospective and replay rows are counted separately
  and never enter the forward headline;
* **qualified_betting** - Value Bets only (frozen value pipeline rows), with
  bookmaker provenance, CLV, ROI, drawdown and staking-policy limits. Legacy
  paper positions and plain Model Picks are never counted as qualified bets;
* **high_confidence_shadow** - the isolated shadow lane's own ``performance()``.

Every DB is opened ``mode=ro``. No provider requests, no writes. Rates are
``None`` (never 0 or a guess) when the denominator is zero, and every cohort
carries ``insufficient_sample`` against an explicit minimum. Nothing here makes a
profitability claim.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..staking import STAKING_RULE_VERSION
from ..storage.football_model_pick_ledger import (
    ISSUANCE_TIMING_FORWARD,
    RECORD_ORIGIN_CURRENT_SYSTEM_REPLAY,
    RECORD_ORIGIN_LIVE_ISSUED,
    issuance_timing,
)
from . import high_confidence_shadow
from .model_pick_history import normalize_state, win_probability_ex_push

SCHEMA_VERSION = "football_monitoring_v1"

# Minimum settled (decisive stake units) before a rate is considered informative.
MIN_SAMPLE_FORWARD = 100
MIN_SAMPLE_COHORT = 30
MIN_SAMPLE_VALUE_BETS = 100

CONFIDENCE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("q<0.50", 0.0, 0.50),
    ("0.50-0.55", 0.50, 0.55),
    ("0.55-0.60", 0.55, 0.60),
    ("0.60-0.70", 0.60, 0.70),
    ("q>=0.70", 0.70, 1.01),
)

HIGH_CONFIDENCE_MISS_Q = 0.60
UPCOMING_WINDOW_HOURS = 72
SETTLEMENT_LAG_HOURS = 3
STALE_ODDS_MINUTES = 180
SCHEDULER_STALE_HOURS = 24

# Heartbeat keys surfaced from the runtime status payload, per scheduler.
SCHEDULERS = (
    "market_collection",
    "result_sync",
    "model_pick_settlement",
    "model_pick_issuance",
    "prediction_refresh",
    "value_evaluation",
    "high_confidence_shadow",
)
_HEARTBEAT_SUFFIXES = ("enabled", "scheduler_enabled", "last_cycle_at", "last_success_at", "last_error", "cycles_completed")

_STAKE = {"FULL_WIN": (1.0, 0.0), "HALF_WIN": (0.5, 0.0), "PUSH": (0.0, 0.0), "HALF_LOSS": (0.0, 0.5), "FULL_LOSS": (0.0, 1.0)}
_NOT_UPCOMING = {"FINISHED", "CANCELLED", "POSTPONED", "ABANDONED", "LIVE", "HALFTIME", "IN_PLAY", "PAUSED"}


# ------------------------------------------------------------------ helpers
def _parse(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _connect_ro(path: str | Path | None) -> sqlite3.Connection | None:
    if not path or not Path(path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection | None, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError:
        return []


def wilson_interval(successes: float, n: float, z: float = 1.96) -> list[float | None]:
    if n <= 0:
        return [None, None]
    p = successes / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def _q(row: Mapping[str, Any]) -> float | None:
    return win_probability_ex_push(row.get("model_fair_price"))


def accuracy_cohort(rows: Iterable[Mapping[str, Any]], *, min_sample: int = MIN_SAMPLE_COHORT) -> dict[str, Any]:
    """Stake-weighted, push-excluded accuracy with Wilson CI and calibration."""
    rows = list(rows)
    won = lost = 0.0
    q_weighted = 0.0
    states: dict[str, int] = dict.fromkeys(_STAKE, 0)
    open_count = 0
    for row in rows:
        state = normalize_state(row.get("trade_state"))
        if state not in _STAKE:
            open_count += 1
            continue
        states[state] += 1
        w, l = _STAKE[state]
        won += w
        lost += l
        q = _q(row)
        if q is not None and (w + l) > 0:
            q_weighted += q * (w + l)
    decisive = won + lost
    share = round(won / decisive, 4) if decisive else None
    mean_q = round(q_weighted / decisive, 4) if decisive else None
    return {
        "issued": len(rows),
        "settled": sum(states.values()),
        "open": open_count,
        "states": states,
        "decisive_stake_units": decisive,
        "win_share_stake_weighted": share,
        "ci95": wilson_interval(won, decisive),
        "mean_q": mean_q,
        "calibration_gap": round(share - mean_q, 4) if share is not None and mean_q is not None else None,
        "insufficient_sample": decisive < min_sample,
        "min_sample": min_sample,
    }


def _band(q: float | None) -> str | None:
    if q is None:
        return None
    for label, low, high in CONFIDENCE_BANDS:
        if low <= q < high:
            return label
    return None


# ------------------------------------------------------------------ sections
def _scheduler_heartbeats(status: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in SCHEDULERS:
        beat = {
            suffix: status.get(f"{name}_{suffix}")
            for suffix in _HEARTBEAT_SUFFIXES
            if f"{name}_{suffix}" in status
        }
        enabled = beat.pop("scheduler_enabled", None)
        # Exception text can carry paths or provider detail; expose presence only.
        beat["last_error_present"] = bool(beat.pop("last_error", None))
        if "enabled" not in beat:
            beat["enabled"] = enabled
        success = _parse(beat.get("last_success_at"))
        beat["hours_since_success"] = None if success is None else round((now - success).total_seconds() / 3600, 2)
        beat["stale"] = bool(beat.get("enabled")) and (success is None or now - success > timedelta(hours=SCHEDULER_STALE_HOURS))
        out[name] = beat
    return out


def _latest_predictions(dash: sqlite3.Connection | None) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in _rows(dash, "SELECT prediction_id, fixture_id, status, generated_at FROM dashboard_predictions"):
        fid = str(row["fixture_id"])
        if fid not in latest or str(row.get("generated_at") or "") > str(latest[fid].get("generated_at") or ""):
            latest[fid] = row
    return latest


def _operational(
    *,
    dash: sqlite3.Connection | None,
    paper: sqlite3.Connection | None,
    picks: Sequence[Mapping[str, Any]],
    status: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    fixtures = _rows(dash, "SELECT fixture_id, kickoff_utc, status FROM dashboard_fixtures")
    latest = _latest_predictions(dash)
    horizon = now + timedelta(hours=UPCOMING_WINDOW_HOURS)
    upcoming = [
        f for f in fixtures
        if str(f.get("status") or "").upper() not in _NOT_UPCOMING
        and (k := _parse(f.get("kickoff_utc"))) is not None and now < k <= horizon
    ]
    picked_fixtures = {str(p.get("fixture_id")) for p in picks}
    with_prediction = [f for f in upcoming if str(f["fixture_id"]) in latest]
    ready = [f for f in with_prediction if str(latest[str(f["fixture_id"])].get("status") or "").upper() == "READY"]
    ready_without_pick = [f for f in ready if str(f["fixture_id"]) not in picked_fixtures]
    not_ready_reasons: dict[str, int] = defaultdict(int)
    for f in with_prediction:
        st = str(latest[str(f["fixture_id"])].get("status") or "UNKNOWN").upper()
        if st != "READY":
            not_ready_reasons[st] += 1

    live = [p for p in picks if str(p.get("record_origin") or "") == RECORD_ORIGIN_LIVE_ISSUED]
    forward = [p for p in live if issuance_timing(p) == ISSUANCE_TIMING_FORWARD]
    latest_forward = max((_parse(p.get("created_at")) for p in forward), default=None, key=lambda d: d or datetime.min.replace(tzinfo=timezone.utc))
    lag_cutoff = now - timedelta(hours=SETTLEMENT_LAG_HOURS)
    backlog = [
        p for p in live
        if normalize_state(p.get("trade_state")) not in _STAKE
        and (k := _parse(p.get("kickoff_utc"))) is not None and k < lag_cutoff
    ]
    oldest_backlog = min((_parse(p.get("kickoff_utc")) for p in backlog), default=None)

    odds_rows = _rows(
        paper,
        "SELECT provider, market, MAX(captured_at) AS latest_captured_at, "
        "SUM(CASE WHEN captured_at >= ? THEN 1 ELSE 0 END) AS snapshots_24h "
        "FROM market_snapshots GROUP BY provider, market",
        (_iso(now - timedelta(hours=24)),),
    )
    odds = []
    for row in odds_rows:
        captured = _parse(row.get("latest_captured_at"))
        age = None if captured is None else round((now - captured).total_seconds() / 60, 1)
        odds.append({
            "provider": row.get("provider"),
            "market": row.get("market"),
            "latest_captured_at": _iso(captured),
            "age_minutes": age,
            "snapshots_24h": int(row.get("snapshots_24h") or 0),
            "stale": age is None or age > STALE_ODDS_MINUTES,
        })

    heartbeats = _scheduler_heartbeats(status, now)
    issuance = heartbeats.get("model_pick_issuance", {})
    alerts: list[dict[str, Any]] = []
    for name, beat in heartbeats.items():
        if beat.get("stale"):
            alerts.append({"code": "SCHEDULER_STALE", "scheduler": name, "hours_since_success": beat.get("hours_since_success")})
        if beat.get("last_error_present"):
            alerts.append({"code": "SCHEDULER_LAST_CYCLE_ERROR", "scheduler": name})
    if issuance.get("enabled") and ready_without_pick:
        alerts.append({"code": "READY_FIXTURES_WITHOUT_PICK", "count": len(ready_without_pick)})
    if upcoming and not ready:
        alerts.append({"code": "NO_READY_PREDICTIONS_FOR_UPCOMING", "upcoming": len(upcoming)})
    if backlog:
        alerts.append({"code": "SETTLEMENT_LAG", "count": len(backlog), "oldest_kickoff_utc": _iso(oldest_backlog)})
    if odds and all(o["stale"] for o in odds):
        alerts.append({"code": "ALL_ODDS_STALE", "threshold_minutes": STALE_ODDS_MINUTES})
    fixture_provider_state = str(status.get("fixture_provider_state") or "").upper()
    if fixture_provider_state and fixture_provider_state != "CONNECTED":
        alerts.append({"code": "FIXTURE_PROVIDER_NOT_CONNECTED", "state": fixture_provider_state})
    quota_state = str(status.get("provider_budget_quota_state") or "").upper()
    if quota_state and quota_state not in {"HEALTHY", "UNKNOWN"}:
        alerts.append({"code": "PROVIDER_QUOTA_NOT_HEALTHY", "state": quota_state})

    return {
        "schedulers": heartbeats,
        "latest_forward_model_pick_issued_at": _iso(latest_forward),
        "latest_prediction_generated_at": max((str(p.get("generated_at")) for p in latest.values() if p.get("generated_at")), default=None),
        "prediction_coverage": {
            "window_hours": UPCOMING_WINDOW_HOURS,
            "upcoming_fixtures": len(upcoming),
            "with_prediction": len(with_prediction),
            "latest_ready": len(ready),
            "ready_without_model_pick": len(ready_without_pick),
            "latest_not_ready_by_status": dict(not_ready_reasons),
            "ready_rate": round(len(ready) / len(upcoming), 4) if upcoming else None,
        },
        "settlement_backlog": {
            "lag_threshold_hours": SETTLEMENT_LAG_HOURS,
            "open_past_kickoff": len(backlog),
            "oldest_kickoff_utc": _iso(oldest_backlog),
            "max_lag_hours": None if oldest_backlog is None else round((now - oldest_backlog).total_seconds() / 3600, 1),
        },
        "providers": {
            "fixture_provider_state": status.get("fixture_provider_state"),
            "provider_budget_quota_state": status.get("provider_budget_quota_state"),
            "market_sync_state": status.get("market_sync_state"),
        },
        "last_issuance_cycle": {
            "written": status.get("model_pick_issuance_written_last_cycle"),
            "canonical_duplicates": status.get("model_pick_issuance_canonical_duplicates_last_cycle"),
            "conflicts": status.get("model_pick_issuance_conflicts_last_cycle"),
        },
        "odds_freshness": sorted(odds, key=lambda o: (str(o["provider"]), str(o["market"]))),
        "alerts": alerts,
    }


def _model_performance(picks: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    live = [p for p in picks if str(p.get("record_origin") or "") == RECORD_ORIGIN_LIVE_ISSUED]
    forward = [p for p in live if issuance_timing(p) == ISSUANCE_TIMING_FORWARD]
    replay = [p for p in picks if str(p.get("record_origin") or "") == RECORD_ORIGIN_CURRENT_SYSTEM_REPLAY]

    def group(key) -> dict[str, Any]:
        buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for p in forward:
            k = key(p)
            if k is not None:
                buckets[str(k)].append(p)
        return {k: accuracy_cohort(v) for k, v in sorted(buckets.items())}

    misses = sorted(
        (
            p for p in forward
            if normalize_state(p.get("trade_state")) == "FULL_LOSS" and (_q(p) or 0) >= HIGH_CONFIDENCE_MISS_Q
        ),
        key=lambda p: -(_q(p) or 0),
    )[:25]
    return {
        "headline_cohort": "LIVE_ISSUED_FORWARD",
        "headline_definition": "record_origin LIVE_ISSUED and created_at strictly before kickoff",
        "forward": accuracy_cohort(forward, min_sample=MIN_SAMPLE_FORWARD),
        "forward_by_market": group(lambda p: p.get("market")),
        "forward_by_competition": group(lambda p: p.get("competition_id")),
        "forward_by_confidence_band": group(lambda p: _band(_q(p))),
        "forward_by_model_version": group(lambda p: p.get("model_version")),
        "high_confidence_misses": [
            {
                "model_pick_id": p.get("model_pick_id"),
                "fixture_id": p.get("fixture_id"),
                "competition_id": p.get("competition_id"),
                "kickoff_utc": p.get("kickoff_utc"),
                "market": p.get("market"),
                "line": p.get("line"),
                "selection": p.get("selection"),
                "q": round(_q(p) or 0, 4),
                "model_version": p.get("model_version"),
            }
            for p in misses
        ],
        "excluded_from_headline": {
            "live_issued_not_forward": len(live) - len(forward),
            "current_system_replay": len(replay),
        },
        # Replay is a separate, retrospective cohort; never pooled with forward.
        "historical_replay": accuracy_cohort(replay) if replay else None,
        "market_benchmark": {
            "forward_available": False,
            "reason": "Forward closing prices are not yet linked to Model Picks; see docs/FOOTBALL_MODEL_ACCURACY_AUDIT.md for the holdout model-vs-market benchmark.",
        },
    }


def _qualified_betting(paper: sqlite3.Connection | None, *, value_bet_issuance_enabled: bool) -> dict[str, Any]:
    from ..storage import football_paper_trading as paper_store
    from .paper_market_evidence import partition_counted_paper_rows

    base = {
        "definition": "Frozen value-pipeline Value Bets only (value_pipeline_version set). Model Picks and legacy paper positions are not qualified bets.",
        "value_bet_issuance_enabled": value_bet_issuance_enabled,
        "staking_policy": {"rule": STAKING_RULE_VERSION, "kelly_fraction": 0.25, "max_stake_fraction": 0.02},
        "profitability_claim": "NONE",
    }
    if paper is None:
        return {**base, "available": False}
    try:
        rows = paper_store.fetch_paper_trade_rows(paper, initialize_schema=False)
    except sqlite3.Error:
        return {**base, "available": False}
    value_rows = [r for r in rows if r.get("value_pipeline_version")]
    counted, quarantined = partition_counted_paper_rows(value_rows, set())
    perf = paper_store.paper_trading_performance(counted)
    decisive = sum(1 for r in counted if r.get("trade_state") in {"WON", "LOST", "HALF_WIN", "HALF_LOSS"})
    stakes = [float(r["suggested_stake_fraction"]) for r in counted if r.get("suggested_stake_fraction") is not None]
    bookmakers: dict[str, int] = defaultdict(int)
    for r in counted:
        bookmakers[f"{r.get('provider') or 'unknown'}/{r.get('bookmaker') or 'unknown'}"] += 1
    return {
        **base,
        "available": True,
        "value_bets": len(counted),
        "quarantined_value_bets": len(quarantined),
        "legacy_paper_positions_excluded": len(rows) - len(value_rows),
        "settled": perf.get("settled_positions"),
        "open": perf.get("open_positions"),
        "units_net": perf.get("net_units"),
        "roi": perf.get("roi"),
        "average_clv": perf.get("average_clv"),
        "clv_state": perf.get("clv_state"),
        "max_drawdown": perf.get("max_drawdown"),
        "by_bookmaker": dict(sorted(bookmakers.items())),
        "max_suggested_stake_fraction": max(stakes) if stakes else None,
        "stake_cap_breaches": sum(1 for s in stakes if s > 0.02 + 1e-12),
        "open_exposure_stake_fraction": round(
            sum(float(r.get("suggested_stake_fraction") or 0) for r in counted if r.get("trade_state") == "PENDING"), 6
        ),
        "insufficient_sample": decisive < MIN_SAMPLE_VALUE_BETS,
        "min_sample": MIN_SAMPLE_VALUE_BETS,
    }


def _shadow(shadow_db_path: str | Path | None) -> dict[str, Any]:
    conn = _connect_ro(shadow_db_path)
    if conn is None:
        return {"available": False, "enabled": high_confidence_shadow.enabled(), "policy_id": high_confidence_shadow.POLICY_ID}
    with closing(conn):
        try:
            perf = high_confidence_shadow.performance(conn)
        except sqlite3.OperationalError:
            return {"available": False, "enabled": high_confidence_shadow.enabled(), "policy_id": high_confidence_shadow.POLICY_ID}
    return {"available": True, "enabled": high_confidence_shadow.enabled(), **perf}


# ------------------------------------------------------------------ entrypoint
def build_football_monitoring_payload(
    *,
    dashboard_db_path: str | Path,
    paper_db_path: str | Path | None,
    model_pick_db_path: str | Path | None,
    shadow_db_path: str | Path | None,
    runtime_status: Mapping[str, Any] | None = None,
    value_bet_issuance_enabled: bool = False,
    deployed_commit: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the read-only monitoring document. Opens every DB ``mode=ro``."""
    now = now or datetime.now(timezone.utc)
    status = dict(runtime_status or {})
    dash = _connect_ro(dashboard_db_path)
    paper = _connect_ro(paper_db_path)
    ledger = _connect_ro(model_pick_db_path)
    try:
        picks = _rows(ledger, "SELECT * FROM football_model_pick_entries")
        return {
            "schema_version": SCHEMA_VERSION,
            "sport": "football",
            "generated_at": _iso(now),
            "read_only": True,
            "deployed_commit": deployed_commit,
            "sources": {
                "dashboard_db": dash is not None,
                "paper_db": paper is not None,
                "model_pick_db": ledger is not None,
            },
            "operational": _operational(dash=dash, paper=paper, picks=picks, status=status, now=now),
            "model_performance": _model_performance(picks),
            "qualified_betting": _qualified_betting(paper, value_bet_issuance_enabled=value_bet_issuance_enabled),
            "high_confidence_shadow": _shadow(shadow_db_path),
        }
    finally:
        for conn in (dash, paper, ledger):
            if conn is not None:
                conn.close()


__all__ = [
    "CONFIDENCE_BANDS",
    "SCHEMA_VERSION",
    "accuracy_cohort",
    "build_football_monitoring_payload",
    "wilson_interval",
]
