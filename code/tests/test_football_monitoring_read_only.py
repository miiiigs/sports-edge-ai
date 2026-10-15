"""GET/HEAD /api/monitoring/football must never mutate operational state.

The monitoring document used to take its runtime status from
``get_health_payload()``, whose provider-budget helpers persist the quota-day
transition and reopen a QUOTA_EXHAUSTED circuit. These tests pin the
read-only path against the real production web composition and prove the
governor still performs those transitions on its own path.
"""

from __future__ import annotations

import gc
import importlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_ai.dashboard.app import create_app
from sports_ai.dashboard.client_api_token import CLIENT_API_TOKEN_ENV_VAR
from sports_ai.dashboard.football_monitoring import to_unified_card
from sports_ai.dashboard.low_memory_app import LowMemoryWebDashboardServices
from sports_ai.dashboard.owner_access import owner_access_policy
from sports_ai.storage import football_model_pick_ledger as ledger
from sports_ai.dashboard.provider_budget import (
    CIRCUIT_CLOSED,
    CIRCUIT_PROVIDER_ERROR,
    CIRCUIT_QUOTA_EXHAUSTED,
    CIRCUIT_UNKNOWN,
    PROVIDER_API_FOOTBALL,
    QUOTA_EXHAUSTED,
    QUOTA_HEALTHY,
    QUOTA_UNKNOWN,
    ProviderRequestBudget,
)

TOKEN = "monitoring-read-only-regression-token-0123456789"
OWNER_PASSWORD = "owner-password-for-tests-only"
PATH = "/api/monitoring/football"
app_module = importlib.import_module("sports_ai.dashboard.app")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_quota(db: Path, state: dict | None) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM provider_quota_state")
        if state is not None:
            conn.execute(
                "INSERT INTO provider_quota_state (provider, state_json, updated_at) VALUES (?, ?, ?)",
                (PROVIDER_API_FOOTBALL, json.dumps(state, sort_keys=True), "2026-10-14T00:00:00+00:00"),
            )


def _seed_circuit(db: Path, state: str | None, *, blocked_until: str | None = None) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM provider_circuit_state")
        if state is not None:
            conn.execute(
                "INSERT INTO provider_circuit_state (provider, current_state, failure_count, blocked_until,"
                " last_error, last_error_at, updated_at) VALUES (?, ?, 3, ?, 'provider said no', ?, ?)",
                (PROVIDER_API_FOOTBALL, state, blocked_until, _iso(_now()), _iso(_now())),
            )


def _healthy() -> dict:
    return {
        "daily_limit": 7500,
        "daily_remaining": 6000,
        "quota_reset_at": _iso(_now() + timedelta(hours=6)),
        "operational_used": 400,
        "operational_cap": 4500,
        "operational_reset_at": _iso(_now() + timedelta(hours=6)),
    }


def _at_boundary() -> dict:
    # Reset instant has just been reached: the governor would clear the
    # previous day's allowance and zero the operational counter.
    state = _healthy()
    state["quota_reset_at"] = _iso(_now())
    state["operational_reset_at"] = _iso(_now())
    return state


def _exhausted(*, reset_passed: bool = False) -> dict:
    state = _healthy()
    state["daily_remaining"] = 0
    state["operational_used"] = 4500
    if reset_passed:
        state["quota_reset_at"] = _iso(_now() - timedelta(minutes=5))
        state["operational_reset_at"] = _iso(_now() - timedelta(minutes=5))
    return state


def _logical_dump(directory: Path) -> dict[str, list[str]]:
    """Every row of every SQLite DB under ``directory``, read mode=ro."""
    out: dict[str, list[str]] = {}
    for path in sorted(directory.glob("*.sqlite")):
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            out[path.name] = list(conn.iterdump())
        finally:
            conn.close()
    return out


def _files(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir() if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal"))}


@pytest.fixture
def prod(tmp_path, monkeypatch):
    """Production web composition (owner_access over LowMemoryWebDashboardServices)."""
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTS_AI_RUNTIME_ROLE", "web")
    monkeypatch.setenv("SPORTS_AI_ALLOW_PAID_PROVIDER_CALLS", "0")
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_OPERATIONAL_DAILY_CAP", "4500")
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "1")
    monkeypatch.setenv("SPORTS_AI_AUTH_COOKIE_SECURE", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_DB_PATH", str(tmp_path / "sports_edge_auth.sqlite"))
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_SIGNUP", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_GUEST", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_USERNAME", "owner")
    monkeypatch.setenv("SPORTS_AI_AUTH_PASSWORD", OWNER_PASSWORD)
    monkeypatch.setenv(CLIENT_API_TOKEN_ENV_VAR, TOKEN)
    # Every request rebuilds the document so repeated reads are real reads.
    monkeypatch.setattr(app_module, "MONITORING_CACHE_SECONDS", 0.0)

    services = LowMemoryWebDashboardServices()
    # An empty (initialized) Model Pick ledger, so card health is computed.
    pick_conn = ledger.connect(services.config.model_pick_db_path)
    ledger.ensure_schema(pick_conn)
    pick_conn.commit()
    pick_conn.close()
    app = create_app(services=services)
    app.middleware("http")(owner_access_policy)

    # Any persisting/provider path reached from a monitoring read fails loudly.
    calls: list[str] = []

    def forbid(name):
        def _raise(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"monitoring read reached {name}")
        return _raise

    with TestClient(app, follow_redirects=False) as client:
        budget = services._provider_budget
        db = Path(services.config.dashboard_db_path)

        def arm():
            for name in ("_persist_quota", "_reopen_quota_exhausted_circuit", "_bump", "issue", "_record_failure"):
                monkeypatch.setattr(budget, name, forbid(name))

        yield {"client": client, "services": services, "budget": budget, "db": db, "dir": tmp_path,
               "arm": arm, "calls": calls}


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _read(prod, *, method: str = "GET"):
    response = prod["client"].request(method, PATH, headers=_bearer())
    assert response.status_code == 200, response.text
    return response


def _assert_no_mutation(prod, before_dump, before_files):
    assert prod["calls"] == []
    assert _logical_dump(prod["dir"]) == before_dump
    assert _files(prod["dir"]) == before_files


@pytest.mark.parametrize(
    "scenario,quota,circuit,expected_quota,expected_circuit",
    [
        ("normal", _healthy, None, QUOTA_HEALTHY, CIRCUIT_CLOSED),
        ("reset_boundary", _at_boundary, None, QUOTA_UNKNOWN, CIRCUIT_CLOSED),
        ("exhausted", _exhausted, CIRCUIT_QUOTA_EXHAUSTED, QUOTA_EXHAUSTED, CIRCUIT_QUOTA_EXHAUSTED),
        ("exhausted_reset_passed", lambda: _exhausted(reset_passed=True), CIRCUIT_QUOTA_EXHAUSTED,
         QUOTA_UNKNOWN, CIRCUIT_QUOTA_EXHAUSTED),
        ("circuit_open", _healthy, CIRCUIT_PROVIDER_ERROR, QUOTA_HEALTHY, CIRCUIT_PROVIDER_ERROR),
        ("absent_metadata", lambda: None, None, QUOTA_UNKNOWN, CIRCUIT_CLOSED),
    ],
)
def test_monitoring_get_and_head_never_mutate(prod, scenario, quota, circuit, expected_quota, expected_circuit):
    _seed_quota(prod["db"], quota())
    blocked = _iso(_now() + timedelta(hours=1)) if circuit else None
    _seed_circuit(prod["db"], circuit, blocked_until=blocked)
    before_dump, before_files = _logical_dump(prod["dir"]), _files(prod["dir"])
    prod["arm"]()

    payload = _read(prod).json()
    head = _read(prod, method="HEAD")
    assert head.content == b""
    for _ in range(3):
        _read(prod)

    _assert_no_mutation(prod, before_dump, before_files)
    providers = payload["operational"]["providers"]
    assert providers["provider_budget_quota_state"] == expected_quota, scenario
    assert providers["provider_budget_circuit_state"] == expected_circuit, scenario
    codes = {a["code"] for a in payload["operational"]["alerts"]}
    card = to_unified_card(payload)
    if expected_circuit != CIRCUIT_CLOSED:
        assert "PROVIDER_CIRCUIT_OPEN" in codes
        assert card["health"] == "RED"
    if expected_quota == QUOTA_UNKNOWN:
        assert "PROVIDER_STATE_UNKNOWN" in codes
    # Unknown or unavailable provider state is never GREEN.
    assert card["health"] != "GREEN"


def test_concurrent_monitoring_reads_never_mutate(prod):
    _seed_quota(prod["db"], _at_boundary())
    _seed_circuit(prod["db"], CIRCUIT_QUOTA_EXHAUSTED, blocked_until=_iso(_now() + timedelta(hours=1)))
    before_dump, before_files = _logical_dump(prod["dir"]), _files(prod["dir"])
    prod["arm"]()
    statuses: list[int] = []
    lock = threading.Lock()

    def worker():
        for _ in range(3):
            code = prod["client"].get(PATH, headers=_bearer()).status_code
            with lock:
                statuses.append(code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses == [200] * 18
    _assert_no_mutation(prod, before_dump, before_files)


def test_governor_path_still_performs_quota_day_transition(prod):
    """The scheduler/provider path keeps its legitimate reset + circuit reopen."""
    _seed_quota(prod["db"], _exhausted(reset_passed=True))
    _seed_circuit(prod["db"], CIRCUIT_QUOTA_EXHAUSTED, blocked_until=_iso(_now() + timedelta(hours=1)))
    budget = prod["budget"]

    view = budget.read_only_view()
    assert view["transition_pending"] is True
    assert view["circuit_state"] == CIRCUIT_QUOTA_EXHAUSTED

    assert budget.quota_state() == QUOTA_UNKNOWN
    assert budget.circuit_state() == CIRCUIT_CLOSED
    with sqlite3.connect(prod["db"]) as conn:
        persisted = json.loads(conn.execute("SELECT state_json FROM provider_quota_state").fetchone()[0])
    assert "daily_remaining" not in persisted
    assert persisted["operational_used"] == 0
    assert budget.read_only_view()["transition_pending"] is False


def test_health_route_still_runs_the_transition(prod):
    """/health is unchanged: it is the documented owner of the read-time reset."""
    _seed_quota(prod["db"], _exhausted(reset_passed=True))
    _seed_circuit(prod["db"], CIRCUIT_QUOTA_EXHAUSTED, blocked_until=_iso(_now() + timedelta(hours=1)))
    prod["services"].get_health_payload()
    assert prod["budget"].circuit_state() == CIRCUIT_CLOSED


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-token-of-sufficient-length-0123456789"}, {"Authorization": TOKEN}],
)
def test_unauthorized_monitoring_is_denied_without_reads(prod, headers):
    before_dump = _logical_dump(prod["dir"])
    prod["arm"]()
    for method in ("GET", "HEAD"):
        assert prod["client"].request(method, PATH, headers=headers).status_code in {401, 403}
    assert prod["calls"] == []
    assert _logical_dump(prod["dir"]) == before_dump


def test_post_is_rejected_with_and_without_token(prod):
    before_dump = _logical_dump(prod["dir"])
    prod["arm"]()
    assert prod["client"].post(PATH).status_code in {401, 403, 405}
    assert prod["client"].post(PATH, headers=_bearer()).status_code in {401, 403, 405}
    assert prod["calls"] == []
    assert _logical_dump(prod["dir"]) == before_dump


def test_response_carries_no_sensitive_values(prod):
    _seed_quota(prod["db"], _exhausted())
    _seed_circuit(prod["db"], CIRCUIT_PROVIDER_ERROR, blocked_until=_iso(_now() + timedelta(hours=1)))
    body = _read(prod).text
    for needle in (TOKEN, OWNER_PASSWORD, str(prod["dir"]), str(prod["dir"]).replace("\\", "/"),
                   "provider said no", "Traceback", "sqlite", "Bearer"):
        assert needle not in body, needle


def test_read_only_view_creates_nothing_when_db_absent(tmp_path):
    db = tmp_path / "budget.sqlite"
    budget = ProviderRequestBudget(db, allow_paid_provider_calls=False, operational_daily_cap=4500)
    gc.collect()  # release the schema connection's file handle (Windows)
    db.unlink()
    view = budget.read_only_view()
    assert view == {
        "quota_state": QUOTA_UNKNOWN,
        "quota_alert": "NONE",
        "quota_metadata_present": False,
        "transition_pending": False,
        "circuit_state": CIRCUIT_UNKNOWN,
    }
    assert not db.exists()
    assert list(tmp_path.iterdir()) == []


def test_read_only_view_matches_governor_state_without_writing(tmp_path):
    db = tmp_path / "budget.sqlite"
    budget = ProviderRequestBudget(db, allow_paid_provider_calls=False, operational_daily_cap=4500)
    for seed, expected in ((_healthy(), QUOTA_HEALTHY), (_exhausted(), QUOTA_EXHAUSTED), (_at_boundary(), QUOTA_UNKNOWN)):
        _seed_quota(db, seed)
        with sqlite3.connect(db) as conn:
            before = conn.execute("SELECT state_json, updated_at FROM provider_quota_state").fetchall()
        assert budget.read_only_view()["quota_state"] == expected
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT state_json, updated_at FROM provider_quota_state").fetchall() == before
        # The governor agrees (and is allowed to persist).
        assert budget.quota_state() == expected
