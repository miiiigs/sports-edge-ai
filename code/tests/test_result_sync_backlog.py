"""Regression tests: bounded result-sync backlog recovery, status batching, cap,
settlement safety and the read-only duplicate-identity diagnostic.

No live provider calls are made.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sports_ai.dashboard import model_pick_settlement as mps
from sports_ai.dashboard import result_sync as rs
from sports_ai.dashboard import storage
from sports_ai.dashboard.fixture_provider import (
    ApiFootballFixtureProvider,
    FixtureProviderConfig,
    FixtureProviderError,
)
from sports_ai.dashboard.services import DashboardServices
from sports_ai.diagnostics import football_model_pick_backlog as diag
from sports_ai.market_data.providers import NotConfiguredMarketProvider
from sports_ai.storage import football_model_pick_ledger as ledger

from test_result_sync import (  # type: ignore[import-not-found]
    FakeFixtureProvider,
    FakeStatsProvider,
    _config,
    _normalized,
    _rows_for,
    _seed_fixture,
    _seed_pick,
)

NOW = datetime(2026, 10, 12, 13, 20, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _ago(**kw) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(**kw))


# --------------------------------------------------------------------------- knobs
def test_new_knobs_default_to_historical_behaviour():
    assert rs.resolve_result_sync_backlog_lookback_days(None) == 0
    assert rs.resolve_result_sync_backlog_lookback_days(0) == 0
    assert rs.resolve_result_sync_backlog_lookback_days(45) == 45
    assert rs.resolve_result_sync_backlog_lookback_days(10_000) == rs.HARD_MAX_RESULT_SYNC_BACKLOG_LOOKBACK_DAYS
    assert rs.resolve_result_sync_status_batch_size(None) == 1
    assert rs.resolve_result_sync_status_batch_size(0) == 1
    assert rs.resolve_result_sync_status_batch_size(50) == rs.API_FOOTBALL_MAX_IDS_PER_REQUEST


def test_backlog_window_bounds():
    k = lambda **kw: _iso(NOW - timedelta(**kw))  # noqa: E731
    assert not rs.within_backlog_lookback(kickoff_utc=k(days=10), now_utc=NOW, backlog_days=0)
    assert not rs.within_backlog_lookback(kickoff_utc=k(hours=1), now_utc=NOW, backlog_days=45)  # grace
    assert not rs.within_backlog_lookback(kickoff_utc=k(hours=48), now_utc=NOW, backlog_days=45)  # recent window
    assert rs.within_backlog_lookback(kickoff_utc=k(days=10), now_utc=NOW, backlog_days=45)
    assert rs.within_backlog_lookback(kickoff_utc=k(days=44), now_utc=NOW, backlog_days=45)
    assert not rs.within_backlog_lookback(kickoff_utc=k(days=46), now_utc=NOW, backlog_days=45)
    assert not rs.within_backlog_lookback(kickoff_utc=_iso(NOW + timedelta(hours=2)), now_utc=NOW, backlog_days=45)


def test_pollable_ids_only_api_football_numeric():
    assert rs.is_pollable_api_football_fixture_id("api-football:1557417")
    assert not rs.is_pollable_api_football_fixture_id("football-data.org:560554")
    assert not rs.is_pollable_api_football_fixture_id("api-football:generated:abc")
    assert not rs.is_pollable_api_football_fixture_id("dcb515c4e1ef88f66e5ec9c48032c7a56d292b14")


# --------------------------------------------------------------------------- prioritisation
def _cand(fid, status, kickoff, *, backlog=False, attempt=None):
    return rs.ResultFixtureCandidate(
        fixture_id=fid, provider_fixture_id=fid.split(":")[-1], kickoff_utc=kickoff, status=status,
        home_score=None, away_score=None, has_live_issued_pick=True,
        last_result_sync_attempt_at=attempt, backlog=backlog,
    )


def test_recent_always_before_backlog_and_backlog_oldest_first():
    recent_pre = _cand("api-football:1", rs.STATE_PRE_MATCH, _iso(NOW - timedelta(hours=5)))
    old_live = _cand("api-football:2", rs.STATE_LIVE, _iso(NOW - timedelta(days=40)), backlog=True)
    old_pre = _cand("api-football:3", rs.STATE_PRE_MATCH, _iso(NOW - timedelta(days=30)), backlog=True)
    older_pre = _cand("api-football:4", rs.STATE_PRE_MATCH, _iso(NOW - timedelta(days=35)), backlog=True)
    attempted = _cand("api-football:5", rs.STATE_PRE_MATCH, _iso(NOW - timedelta(days=44)), backlog=True, attempt="2026-10-12T00:00:00+00:00")
    order = [c.fixture_id for c in rs.select_result_sync_fixtures([attempted, older_pre, old_pre, old_live, recent_pre], max_fixtures=10)]
    # recent first; then backlog stale-LIVE; then never-attempted oldest-first; re-attempts last.
    assert order == ["api-football:1", "api-football:2", "api-football:4", "api-football:3", "api-football:5"]


def test_chunk_batches_caps_size_count_and_drops_unpollable():
    cands = [_cand(f"api-football:{i}", rs.STATE_PRE_MATCH, _iso(NOW - timedelta(days=5)), backlog=True) for i in range(1, 50)]
    cands.insert(0, _cand("football-data.org:9", rs.STATE_PRE_MATCH, _iso(NOW - timedelta(days=5)), backlog=True))
    batches = rs.chunk_status_batches(cands, batch_size=99, max_batches=2)
    assert len(batches) == 2
    assert all(len(b) <= 20 for b in batches)
    assert all(rs.is_pollable_api_football_fixture_id(c.fixture_id) for b in batches for c in b)


# --------------------------------------------------------------------------- services
class BatchFakeProvider(FakeFixtureProvider):
    def __init__(self, fixtures_by_id):
        super().__init__(fixtures_by_id)
        self.batch_calls: list[list[str]] = []

    def get_fixtures_by_ids(self, fixture_ids):
        self.batch_calls.append(list(fixture_ids))
        return {fid: self._fixtures[fid] for fid in fixture_ids if fid in self._fixtures}


def _services(tmp_path, monkeypatch, fixtures, provider, stats=None, quota_state="HEALTHY", **cfg):
    config = replace(_config(tmp_path), **cfg)
    for f in fixtures:
        _seed_fixture(config, f)
    services = DashboardServices(config, fixture_provider=provider, market_provider=NotConfiguredMarketProvider(), statistics_provider=stats or FakeStatsProvider({}))
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: _rows_for(fixtures))
    monkeypatch.setattr(services._provider_budget, "quota_state", lambda: quota_state)
    return config, services


def test_default_config_never_polls_backlog(tmp_path, monkeypatch):
    fid = "api-football:9001"
    stale = _normalized(fid, status="PRE_MATCH", home_score=None, away_score=None, kickoff=_ago(days=10))
    provider = FakeFixtureProvider({fid: replace(stale, status="FINISHED", home_score=1, away_score=0)})
    config, services = _services(tmp_path, monkeypatch, [stale], provider)
    _seed_pick(config.model_pick_db_path, fid)
    result = services.run_current_result_sync_cycle()
    assert result["selected"] == [] and provider.calls == 0


def test_backlog_enabled_recovers_stale_pre_match_fixture(tmp_path, monkeypatch):
    fid = "api-football:9002"
    stale = _normalized(fid, status="PRE_MATCH", home_score=None, away_score=None, kickoff=_ago(days=10))
    finished = _normalized(fid, status="FINISHED", home_score=2, away_score=2, kickoff=stale.kickoff_utc, provider_payload={"fixture": {"status": {"short": "FT"}}})
    provider = FakeFixtureProvider({fid: finished})
    config, services = _services(tmp_path, monkeypatch, [stale], provider, result_sync_backlog_lookback_days=45)
    _seed_pick(config.model_pick_db_path, fid)
    result = services.run_current_result_sync_cycle()
    assert result["selected"] == [fid]
    assert result["backlog_selected"] == [fid]
    assert result["results_updated"] == 1
    with storage.connect(config.dashboard_db_path) as conn:
        row = storage.fetch_fixture(conn, fid, initialize_schema=False)
    assert (row["status"], row["home_score"], row["away_score"]) == ("FINISHED", 2, 2)


@pytest.mark.parametrize("quota_state", ["UNKNOWN", "CONSERVE", "LOW", "CRITICAL", "EXHAUSTED"])
def test_backlog_pauses_unless_quota_is_healthy(tmp_path, monkeypatch, quota_state):
    old_fid, recent_fid = "api-football:9010", "api-football:9011"
    stale = _normalized(old_fid, status="PRE_MATCH", home_score=None, away_score=None, kickoff=_ago(days=10))
    recent = _normalized(recent_fid, status="PRE_MATCH", home_score=None, away_score=None, kickoff=_ago(hours=3))
    provider = BatchFakeProvider({
        old_fid: replace(stale, status="FINISHED", home_score=1, away_score=0),
        recent_fid: replace(recent, status="FINISHED", home_score=0, away_score=0),
    })
    config, services = _services(
        tmp_path, monkeypatch, [stale, recent], provider, quota_state=quota_state,
        result_sync_backlog_lookback_days=45, result_sync_status_batch_size=20,
    )
    _seed_pick(config.model_pick_db_path, old_fid)
    _seed_pick(config.model_pick_db_path, recent_fid)
    result = services.run_current_result_sync_cycle()
    assert result["backlog_paused_reason"] == f"QUOTA_{quota_state}"
    assert result["backlog_selected"] == []
    assert old_fid not in [fid for call in provider.batch_calls for fid in call]
    if quota_state != "EXHAUSTED":
        assert result["selected"] == [recent_fid]  # the recent 72h window keeps running
    assert services._result_sync_runtime_state["result_sync_backlog_paused_reason"] == f"QUOTA_{quota_state}"


def test_backlog_not_paused_when_quota_healthy(tmp_path, monkeypatch):
    fid = "api-football:9012"
    stale = _normalized(fid, status="PRE_MATCH", home_score=None, away_score=None, kickoff=_ago(days=10))
    provider = BatchFakeProvider({fid: replace(stale, status="FINISHED", home_score=1, away_score=0)})
    config, services = _services(
        tmp_path, monkeypatch, [stale], provider, result_sync_backlog_lookback_days=45, result_sync_status_batch_size=20,
    )
    _seed_pick(config.model_pick_db_path, fid)
    result = services.run_current_result_sync_cycle()
    assert result["backlog_paused_reason"] is None
    assert result["backlog_selected"] == [fid]


def test_backlog_beyond_window_and_settled_picks_are_ignored(tmp_path, monkeypatch):
    too_old = _normalized("api-football:9003", status="PRE_MATCH", home_score=None, away_score=None, kickoff=_ago(days=50))
    settled = _normalized("api-football:9004", status="PRE_MATCH", home_score=None, away_score=None, kickoff=_ago(days=10))
    provider = FakeFixtureProvider({})
    config, services = _services(tmp_path, monkeypatch, [too_old, settled], provider, result_sync_backlog_lookback_days=45)
    _seed_pick(config.model_pick_db_path, too_old.fixture_id)
    conn = ledger.connect(config.model_pick_db_path)
    conn.execute("ALTER TABLE football_model_pick_entries ADD COLUMN trade_state TEXT")
    conn.execute("INSERT INTO football_model_pick_entries (fixture_id, market, record_origin, kickoff_utc, selection, trade_state) VALUES (?,?,?,?,?,?)", (settled.fixture_id, "asian_handicap", "LIVE_ISSUED", "", "HOME", "FULL_WIN"))
    conn.commit()
    conn.close()
    result = services.run_current_result_sync_cycle()
    assert result["selected"] == [] and provider.calls == 0


def test_provider_mismatch_never_consumes_a_slot(tmp_path, monkeypatch):
    fdo = _normalized("football-data.org:560554", status="LIVE", home_score=0, away_score=0, kickoff=_ago(hours=5))
    af = _normalized("api-football:9005", status="LIVE", home_score=0, away_score=0, kickoff=_ago(hours=6))
    provider = FakeFixtureProvider({af.fixture_id: replace(af, status="FINISHED", home_score=1, away_score=0)})
    config, services = _services(tmp_path, monkeypatch, [fdo, af], provider, result_sync_max_fixtures_per_cycle=1)
    _seed_pick(config.model_pick_db_path, fdo.fixture_id)
    _seed_pick(config.model_pick_db_path, af.fixture_id)
    result = services.run_current_result_sync_cycle()
    assert result["selected"] == [af.fixture_id]
    assert result["provider_mismatch"] == 1
    assert provider.called_ids == [af.fixture_id]


def test_batched_cycle_respects_request_cap(tmp_path, monkeypatch):
    fixtures, truth = [], {}
    for i in range(45):
        fid = f"api-football:{10_000 + i}"
        f = _normalized(fid, status="PRE_MATCH", home_score=None, away_score=None, kickoff=_ago(days=5 + (i % 30)))
        fixtures.append(f)
        truth[fid] = replace(f, status="FINISHED", home_score=1, away_score=1)
    # 5 finished fixtures with corner picks but no corner truth (corner track).
    corner_fixtures = [
        _normalized(f"api-football:{20_000 + i}", status="FINISHED", home_score=1, away_score=0, kickoff=_ago(days=6)) for i in range(5)
    ]
    provider = BatchFakeProvider(truth)
    stats = FakeStatsProvider({f.fixture_id: (5, 4) for f in corner_fixtures})
    config, services = _services(
        tmp_path, monkeypatch, fixtures + corner_fixtures, provider, stats,
        result_sync_backlog_lookback_days=45, result_sync_status_batch_size=20, result_sync_max_fixtures_per_cycle=2,
    )
    for f in fixtures:
        _seed_pick(config.model_pick_db_path, f.fixture_id, market="corner_total")
    for f in corner_fixtures:
        _seed_pick(config.model_pick_db_path, f.fixture_id, market="corner_total")
    result = services.run_current_result_sync_cycle()
    # cap=2 -> exactly 1 batched status request (<=20 ids) + 1 corner request; no
    # per-id status calls and no same-cycle corner bursts beyond the cap.
    assert len(provider.batch_calls) == 1 and len(provider.batch_calls[0]) == 20
    assert provider.calls == 0
    assert stats.calls == 1
    assert len(provider.batch_calls) + stats.calls <= 2
    assert result["results_updated"] == 20
    assert result["status_batches"] == 1


def test_batched_missing_ids_are_never_inferred(tmp_path, monkeypatch):
    a = _normalized("api-football:30001", status="LIVE", home_score=1, away_score=0, kickoff=_ago(days=4))
    b = _normalized("api-football:30002", status="LIVE", home_score=1, away_score=0, kickoff=_ago(days=4))
    provider = BatchFakeProvider({a.fixture_id: replace(a, status="FINISHED", home_score=2, away_score=0)})
    config, services = _services(tmp_path, monkeypatch, [a, b], provider, result_sync_backlog_lookback_days=45, result_sync_status_batch_size=20)
    _seed_pick(config.model_pick_db_path, a.fixture_id)
    _seed_pick(config.model_pick_db_path, b.fixture_id)
    result = services.run_current_result_sync_cycle()
    assert result["identity_unresolved"] == 1
    with storage.connect(config.dashboard_db_path) as conn:
        assert storage.fetch_fixture(conn, b.fixture_id, initialize_schema=False)["status"] == "LIVE"
        assert storage.fetch_fixture(conn, a.fixture_id, initialize_schema=False)["status"] == "FINISHED"


def test_batched_final_without_score_is_rejected(tmp_path, monkeypatch):
    a = _normalized("api-football:30003", status="LIVE", home_score=None, away_score=None, kickoff=_ago(days=4))
    provider = BatchFakeProvider({a.fixture_id: replace(a, status="FINISHED", home_score=None, away_score=None)})
    config, services = _services(tmp_path, monkeypatch, [a], provider, result_sync_backlog_lookback_days=45, result_sync_status_batch_size=20)
    _seed_pick(config.model_pick_db_path, a.fixture_id)
    result = services.run_current_result_sync_cycle()
    assert result["results_updated"] == 0
    with storage.connect(config.dashboard_db_path) as conn:
        assert storage.fetch_fixture(conn, a.fixture_id, initialize_schema=False)["status"] == "LIVE"


# --------------------------------------------------------------------------- provider batching
class _Budgetless(ApiFootballFixtureProvider):
    def __init__(self, rows):
        super().__init__(FixtureProviderConfig())
        self.params_seen = []
        self._rows = rows

    def _request_fixtures(self, *, params, priority_override=None):  # noqa: D401
        self.params_seen.append(dict(params))
        return list(self._rows)

    def _division_code_for_league_id(self, league_id):
        return "E0" if league_id == 39 else None


def _af_row(fid, short="FT", home=1, away=0, league=39):
    return {
        "fixture": {"id": fid, "date": "2026-09-01T14:00:00+00:00", "status": {"short": short, "elapsed": 90}},
        "league": {"id": league, "name": "Premier League", "season": 2026},
        "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"}},
        "goals": {"home": home, "away": away},
    }


def test_provider_ids_request_is_single_sorted_and_filters_unrequested_rows():
    provider = _Budgetless([_af_row(5), _af_row(3), _af_row(777)])
    out = provider.get_fixtures_by_ids(["api-football:5", "football-data.org:1", "api-football:3", "api-football:5"])
    assert provider.params_seen == [{"ids": "3-5"}]
    assert sorted(out) == ["api-football:3", "api-football:5"]
    assert provider._fixture_request_policy({"ids": "3-5"})[0] == 300


def test_provider_ids_request_rejects_more_than_20():
    provider = _Budgetless([])
    with pytest.raises(FixtureProviderError):
        provider.get_fixtures_by_ids([f"api-football:{i}" for i in range(1, 22)])
    assert provider.get_fixtures_by_ids(["football-data.org:1"]) == {}
    assert provider.params_seen == []


# --------------------------------------------------------------------------- settlement safety
def _pick(fid, market="asian_handicap", selection="HOME", line=-0.5):
    return {"model_pick_id": f"mp_{fid}_{market}", "fixture_id": fid, "market": market, "selection": selection, "line": line}


@pytest.mark.parametrize("status", ["POSTPONED", "CANCELLED", "ABANDONED", "PRE_MATCH", "LIVE", "HALFTIME"])
def test_non_final_never_settled_even_with_scores(status):
    fixtures = {"f": {"status": status, "home_score": 0, "away_score": 3, "kickoff_utc": "2026-09-01T14:00:00+00:00"}}
    plan = mps.build_settlement_plan(open_picks=[_pick("f"), _pick("f", "corner_total", "OVER", 9.5)], fixtures=fixtures, results={"f": {"home_corners": 1, "away_corners": 1, "total_corners": 2}}, now_utc=NOW)
    assert plan.actions == ()
    assert {r["reason"] for r in plan.ineligible} == {mps.REASON_NON_FINAL}


def test_finished_without_verified_score_or_corners_not_settled():
    fixtures = {
        "noscore": {"status": "FINISHED", "home_score": None, "away_score": 1, "kickoff_utc": "2026-09-01T14:00:00+00:00"},
        "ok": {"status": "FINISHED", "home_score": 2, "away_score": 0, "kickoff_utc": "2026-09-01T14:00:00+00:00"},
    }
    plan = mps.build_settlement_plan(
        open_picks=[_pick("noscore"), _pick("ok", "corner_total", "OVER", 9.5), _pick("ok")],
        fixtures=fixtures, results={}, now_utc=NOW,
    )
    assert [a.model_pick_id for a in plan.actions] == ["mp_ok_asian_handicap"]
    assert plan.ineligible_reason_counts() == {mps.REASON_FINAL_SCORE_MISSING: 1, mps.REASON_CORNER_TRUTH_MISSING: 1}


# --------------------------------------------------------------------------- duplicate diagnostic
def _make_ledger(path: Path, rows):
    conn = ledger.connect(path)
    ledger.ensure_schema(conn)
    for r in rows:
        conn.execute(
            "INSERT INTO football_model_pick_entries (model_pick_id, policy_id, policy_version, fixture_id, canonical_fixture_id, competition_id, kickoff_utc, market, line, selection, record_origin, trade_state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["model_pick_id"], "p", "1", r["fixture_id"], r.get("canonical_fixture_id"), r.get("competition_id", "ENG1"), r["kickoff_utc"], r["market"], r.get("line", -0.5), r.get("selection", "HOME"), "LIVE_ISSUED", r.get("trade_state"), "2026-10-06T00:00:00+00:00", "2026-10-06T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()


def _make_dashboard(path: Path, fixtures, results=()):
    with storage.connect(path) as conn:
        storage.initialize_database(conn)
        for f in fixtures:
            storage.upsert_fixtures(conn, [f], updated_at="2026-10-08T10:00:00+00:00", initialize_schema=False)
        for fid, (h, a) in results:
            storage.upsert_fixture_result(conn, fixture_id=fid, statistics_provider="api-football", statistics_provider_fixture_id=None, home_corners=h, away_corners=a, total_corners=h + a, source_timestamp=None, captured_at="2026-10-08T10:00:00+00:00", initialize_schema=False)


def _digest(*paths: Path) -> list[str]:
    return [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]


def test_duplicate_diagnostic_reports_pairs_and_is_read_only(tmp_path):
    ko = "2026-08-29T14:00:00+00:00"
    mp_db = tmp_path / "football_model_picks.sqlite"
    db = tmp_path / "football_dashboard.sqlite"
    _make_ledger(mp_db, [
        {"model_pick_id": "a1", "fixture_id": "api-football:1557377", "canonical_fixture_id": "canon1", "kickoff_utc": ko, "market": "asian_handicap", "trade_state": "FULL_WIN"},
        {"model_pick_id": "a2", "fixture_id": "football-data.org:560557", "canonical_fixture_id": "canon1", "kickoff_utc": ko, "market": "asian_handicap", "trade_state": None},
        {"model_pick_id": "c1", "fixture_id": "api-football:1557377", "canonical_fixture_id": "canon1", "kickoff_utc": ko, "market": "corner_total", "trade_state": "FULL_LOSS"},
        {"model_pick_id": "c2", "fixture_id": "football-data.org:560557", "canonical_fixture_id": "canon1", "kickoff_utc": ko, "market": "corner_total", "trade_state": "FULL_WIN"},
        {"model_pick_id": "u1", "fixture_id": "api-football:1", "canonical_fixture_id": "canon2", "kickoff_utc": ko, "market": "asian_handicap", "trade_state": None},
    ])
    _make_dashboard(db, [
        _normalized("api-football:1557377", status="FINISHED", home_score=2, away_score=1, kickoff=ko),
        _normalized("football-data.org:560557", status="LIVE", home_score=1, away_score=1, kickoff=ko),
    ], results=[("api-football:1557377", (5, 4)), ("football-data.org:560557", (6, 4))])
    before = _digest(mp_db, db)
    picks, fixtures, results = diag.load_snapshot(model_pick_db=mp_db, dashboard_db=db)
    report = diag.canonical_duplicate_report(picks, fixtures, results)
    assert report["read_only"] is True
    assert report["summary"]["duplicate_groups"] == 2
    groups = {g["market"]: g for g in report["groups"]}
    ah = groups["asian_handicap"]
    assert ah["providers"] == ["api-football", "football-data.org"]
    assert ah["settled_count"] == 1 and not ah["all_settled"]
    corners = groups["corner_total"]
    assert corners["settlement_states_disagree"] is True
    assert corners["results_disagree"] is True  # 9 vs 10 corners
    assert report["summary"]["partially_settled"] == 1 and report["summary"]["all_settled_disagreeing"] == 1
    # Running the CLI and classifier too must not change a single byte.
    assert diag.main(["--model-pick-db", str(mp_db), "--dashboard-db", str(db), "--rows"]) == 0
    assert _digest(mp_db, db) == before
    # The read-only connection refuses writes.
    conn = diag._readonly_connect(mp_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM football_model_pick_entries")
    conn.close()


def test_open_pick_classification(tmp_path):
    now = NOW
    picks = [
        {"model_pick_id": "1", "fixture_id": "api-football:1", "market": "corner_total", "record_origin": "LIVE_ISSUED", "kickoff_utc": "2026-09-19T14:00:00+00:00"},
        {"model_pick_id": "2", "fixture_id": "api-football:2", "market": "asian_handicap", "record_origin": "LIVE_ISSUED", "kickoff_utc": "2026-09-19T14:00:00+00:00"},
        {"model_pick_id": "3", "fixture_id": "football-data.org:3", "market": "asian_handicap", "record_origin": "LIVE_ISSUED", "kickoff_utc": "2026-08-29T14:00:00+00:00"},
        {"model_pick_id": "4", "fixture_id": "football-data.org:4", "market": "asian_handicap", "record_origin": "LIVE_ISSUED", "kickoff_utc": "2026-08-29T14:00:00+00:00"},
        {"model_pick_id": "5", "fixture_id": "api-football:5", "market": "asian_handicap", "record_origin": "LIVE_ISSUED", "kickoff_utc": "2026-10-03T14:00:00+00:00"},
        {"model_pick_id": "6", "fixture_id": "api-football:6", "market": "asian_handicap", "record_origin": "LIVE_ISSUED", "kickoff_utc": "2026-10-10T14:00:00+00:00", "trade_state": "FULL_WIN"},
        {"model_pick_id": "7", "fixture_id": "api-football:7", "market": "asian_handicap", "record_origin": "LIVE_ISSUED", "kickoff_utc": "2026-10-13T14:00:00+00:00"},
    ]
    fixtures = {
        "api-football:1": {"status": "FINISHED", "home_score": 1, "away_score": 0, "competition_id": "ENG3", "kickoff_utc": "2026-09-19T14:00:00+00:00"},
        "api-football:2": {"status": "PRE_MATCH", "competition_id": "ENG4", "kickoff_utc": "2026-09-19T14:00:00+00:00"},
        "football-data.org:3": {"status": "LIVE", "competition_id": "ENG2", "kickoff_utc": "2026-08-29T14:00:00+00:00"},
        "football-data.org:4": {"status": "PRE_MATCH", "competition_id": "SA", "kickoff_utc": "2026-08-29T14:00:00+00:00"},
        "api-football:5": {"status": "POSTPONED", "competition_id": "ENG3", "kickoff_utc": "2026-10-03T14:00:00+00:00"},
    }
    out = diag.classify_open_picks(picks, fixtures, {}, now_utc=now)
    assert out["examined"] == 5
    assert out["class_counts"] == {
        diag.CLASS_GENUINE_UNRESOLVED: 1,
        diag.CLASS_MISSING_CORNER_TRUTH: 1,
        diag.CLASS_PROVIDER_MISMATCH: 1,
        diag.CLASS_RESULT_SYNC_BACKLOG: 1,
        diag.CLASS_UNSUPPORTED_COMPETITION: 1,
    }
