"""Focused tests for the forward-only live market evidence contract.

No live provider calls are made. These tests exercise AH/corner normalization,
two-sided pairing, freshness classification, pre-match-only eligibility, fixture
identity resolution, append-only chronology and read-only coverage diagnostics.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sports_ai.dashboard.services  # noqa: F401  (pre-import breaks the market_data/dashboard cycle)
from sports_ai.diagnostics.football_live_market_evidence import run_diagnostics
from sports_ai.market_data import market_evidence as me
from sports_ai.market_data.five_dollar_football import FiveDollarRateGovernor
from sports_ai.market_data.five_dollar_shadow import ShadowBudget
from sports_ai.storage import football_canonical_fixture as cf
from sports_ai.storage import football_paper_trading as pt

KICKOFF = "2026-10-07T15:00:00+00:00"
NOW = "2026-10-07T12:00:00+00:00"
FRESH_SOURCE = "2026-10-07T11:30:00+00:00"
STALE_SOURCE = "2026-10-06T00:00:00+00:00"


def _ago(minutes: int) -> str:
    base = datetime.fromisoformat(NOW).astimezone(timezone.utc)
    return (base - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _quote(
    *,
    market: str,
    selection: str,
    line: float,
    odds: float = 1.9,
    captured_at: str = NOW,
    source_timestamp: str | None = FRESH_SOURCE,
    kickoff_utc: str = KICKOFF,
    fixture_id: str = "canon_fixture_1",
    bookmaker: str = "Bet 365",
    provider: str = "api-football",
) -> me.MarketEvidenceQuote:
    return me.MarketEvidenceQuote(
        canonical_fixture_id=fixture_id,
        provider=provider,
        provider_fixture_id="12345",
        bookmaker=bookmaker,
        market=market,
        selection=selection,
        line=line,
        decimal_odds=odds,
        captured_at=captured_at,
        source_timestamp=source_timestamp,
        kickoff_utc=kickoff_utc,
    )


# --------------------------------------------------------------------------- AH signs
def test_ah_sign_normalization_named_team_convention():
    # HOME -0.5 stays -0.5; AWAY -0.5 becomes +0.5 (home receives 0.5).
    assert (
        me.normalize_asian_handicap_line(
            selection="HOME", raw_line=-0.5, convention=me.AsianHandicapConvention.NAMED_TEAM_HANDICAP
        )
        == -0.5
    )
    assert (
        me.normalize_asian_handicap_line(
            selection="AWAY", raw_line=-0.5, convention=me.AsianHandicapConvention.NAMED_TEAM_HANDICAP
        )
        == 0.5
    )
    assert (
        me.normalize_asian_handicap_line(
            selection="HOME", raw_line=0.5, convention=me.AsianHandicapConvention.NAMED_TEAM_HANDICAP
        )
        == 0.5
    )
    assert (
        me.normalize_asian_handicap_line(
            selection="AWAY", raw_line=0.5, convention=me.AsianHandicapConvention.NAMED_TEAM_HANDICAP
        )
        == -0.5
    )


def test_ah_sign_normalization_home_handicap_convention_is_never_flipped():
    for selection in ("HOME", "AWAY"):
        assert (
            me.normalize_asian_handicap_line(
                selection=selection, raw_line=-0.25, convention=me.AsianHandicapConvention.HOME_HANDICAP
            )
            == -0.25
        )


def test_ah_pairing_pairs_opposite_sides_at_same_physical_line():
    home = _quote(market="asian_handicap", selection="HOME", line=0.5)
    away = _quote(market="asian_handicap", selection="AWAY", line=0.5)
    pairs = me.pair_asian_handicap_quotes([home, away])
    assert len(pairs) == 1
    assert pairs[0].line == 0.5
    assert pairs[0].primary.selection == "HOME"
    assert pairs[0].secondary.selection == "AWAY"


def test_ah_pairing_rejects_unpaired_side():
    with pytest.raises(me.UnpairedMarketEvidenceError):
        me.pair_asian_handicap_quotes([_quote(market="asian_handicap", selection="HOME", line=-0.5)])


def test_ah_pairing_rejects_duplicate_side():
    duplicate = [
        _quote(market="asian_handicap", selection="HOME", line=-0.5),
        _quote(market="asian_handicap", selection="HOME", line=-0.5, odds=1.95),
    ]
    with pytest.raises(me.UnpairedMarketEvidenceError):
        me.pair_asian_handicap_quotes(duplicate)


# --------------------------------------------------------------------------- corners
def test_corner_pairing_pairs_over_under_at_same_line():
    over = _quote(market="corner_total", selection="OVER", line=9.5)
    under = _quote(market="corner_total", selection="UNDER", line=9.5, odds=1.95)
    pairs = me.pair_corner_total_quotes([over, under])
    assert len(pairs) == 1
    assert pairs[0].line == 9.5


def test_corner_pairing_rejects_unpaired_and_missing_line():
    with pytest.raises(me.UnpairedMarketEvidenceError):
        me.pair_corner_total_quotes([_quote(market="corner_total", selection="OVER", line=9.5)])
    with pytest.raises(me.MalformedMarketRowError):
        _quote(market="corner_total", selection="OVER", line=0.0)


@pytest.mark.parametrize("bad_line", [0.0, -9.5, 9.3, float("nan")])
def test_corner_rejects_malformed_line(bad_line):
    with pytest.raises(me.MalformedMarketRowError):
        me.normalize_corner_total_line(raw_line=bad_line)


def test_non_match_total_corner_market_rejected():
    with pytest.raises(me.MalformedMarketRowError):
        me.classify_provider_market_name("Home Corners Over Under")
    with pytest.raises(me.MalformedMarketRowError):
        me.classify_provider_market_name("Corners 1st Half Over/Under")
    assert me.classify_provider_market_name("Corners Over Under") == "corner_total"
    assert me.classify_provider_market_name("Asian Handicap") == "asian_handicap"


# --------------------------------------------------------------------------- freshness
def test_fresh_quote_is_fresh_prematch():
    result = me.classify_quote_state(
        captured_at=NOW,
        source_timestamp=FRESH_SOURCE,
        kickoff_utc=KICKOFF,
        now_utc=NOW,
        actionable_max_age_minutes=240,
        collector_health_max_age_minutes=360,
    )
    assert result.state == me.QUOTE_STATE_FRESH_PREMATCH
    assert result.age_minutes == pytest.approx(30.0)
    assert result.actionable_max_age_minutes == 240


def test_stale_quote_is_stale_prematch():
    result = me.classify_quote_state(
        captured_at=STALE_SOURCE,
        source_timestamp=STALE_SOURCE,
        kickoff_utc=KICKOFF,
        now_utc=NOW,
        actionable_max_age_minutes=240,
    )
    assert result.state == me.QUOTE_STATE_STALE_PREMATCH


@pytest.mark.parametrize(
    ("age_minutes", "expected"),
    [
        (239, me.QUOTE_STATE_FRESH_PREMATCH),
        (240, me.QUOTE_STATE_FRESH_PREMATCH),
        (241, me.QUOTE_STATE_STALE_PREMATCH),
    ],
)
def test_api_football_actionable_threshold_boundary(monkeypatch, age_minutes, expected):
    monkeypatch.delenv("SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES", raising=False)
    monkeypatch.delenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", raising=False)
    threshold = me.resolve_actionable_max_age_minutes("api-football")
    assert threshold == 240
    result = me.classify_quote_state(
        captured_at=_ago(age_minutes),
        source_timestamp=_ago(age_minutes),
        kickoff_utc=KICKOFF,
        now_utc=NOW,
        actionable_max_age_minutes=threshold,
    )
    assert result.state == expected


def test_300m_quote_stale_for_value_but_not_collector_cycle_missed():
    quote = _quote(
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        captured_at=_ago(300),
        source_timestamp=_ago(300),
    )
    state = me.classify_quote_state(
        captured_at=quote.captured_at,
        source_timestamp=quote.source_timestamp,
        kickoff_utc=KICKOFF,
        now_utc=NOW,
        actionable_max_age_minutes=240,
        collector_health_max_age_minutes=360,
    )
    assert state.state == me.QUOTE_STATE_STALE_PREMATCH
    assert state.collector_health_state in {me.COLLECTOR_CURRENT_CYCLE, me.COLLECTOR_CYCLE_DUE}
    assert not me.pre_match_value_eligibility(quote, now_utc=NOW).eligible


def test_over_360m_quote_stale_and_collector_cycle_missed():
    result = me.classify_quote_state(
        captured_at=_ago(361),
        source_timestamp=_ago(361),
        kickoff_utc=KICKOFF,
        now_utc=NOW,
        actionable_max_age_minutes=240,
        collector_health_max_age_minutes=360,
    )
    assert result.state == me.QUOTE_STATE_STALE_PREMATCH
    assert result.collector_health_state == me.COLLECTOR_CYCLE_MISSED


def test_in_play_quote_rejected_for_pre_match_value():
    quote = _quote(
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        captured_at="2026-10-07T15:30:00+00:00",
        source_timestamp="2026-10-07T15:30:00+00:00",
    )
    eligibility = me.pre_match_value_eligibility(quote, now_utc="2026-10-07T15:31:00+00:00")
    assert not eligibility.eligible
    assert eligibility.quote_state == me.QUOTE_STATE_IN_PLAY


def test_captured_after_kickoff_rejected_even_with_fresh_source():
    quote = _quote(
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        captured_at="2026-10-07T15:10:00+00:00",
        source_timestamp="2026-10-07T14:55:00+00:00",
    )
    eligibility = me.pre_match_value_eligibility(quote, now_utc="2026-10-07T15:11:00+00:00")
    assert not eligibility.eligible


def test_fresh_prematch_quote_is_eligible():
    quote = _quote(market="asian_handicap", selection="HOME", line=-0.5)
    eligibility = me.pre_match_value_eligibility(quote, now_utc=NOW)
    assert eligibility.eligible


def test_source_timestamp_authoritative_over_capture():
    quote = _quote(
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        captured_at=NOW,  # capture looks fresh
        source_timestamp=_ago(300),  # authoritative source is stale
    )
    state = me.classify_quote_state(
        captured_at=quote.captured_at,
        source_timestamp=quote.source_timestamp,
        kickoff_utc=KICKOFF,
        now_utc=NOW,
        actionable_max_age_minutes=240,
    )
    assert state.state == me.QUOTE_STATE_STALE_PREMATCH
    assert state.age_minutes == pytest.approx(300.0)


def test_restart_replay_of_old_source_is_not_fresh():
    # A cached provider payload re-observed with a new captured_at, but an old
    # source timestamp, must never be classified fresh.
    replay = _quote(
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        captured_at=NOW,
        source_timestamp="2026-10-01T00:00:00+00:00",
    )
    state = me.classify_quote_state(
        captured_at=replay.captured_at,
        source_timestamp=replay.source_timestamp,
        kickoff_utc=replay.kickoff_utc,
        now_utc=NOW,
        actionable_max_age_minutes=240,
    )
    assert state.state == me.QUOTE_STATE_STALE_PREMATCH
    assert not me.pre_match_value_eligibility(replay, now_utc=NOW).eligible


def test_future_source_timestamp_is_invalid():
    result = me.classify_quote_state(
        captured_at=NOW,
        source_timestamp="2026-10-08T00:00:00+00:00",
        kickoff_utc=KICKOFF,
        now_utc=NOW,
        actionable_max_age_minutes=240,
    )
    assert result.state == me.QUOTE_STATE_INVALID


# ------------------------------------------- provider-specific actionable freshness
def test_provider_specific_configured_threshold_respected(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES", "120")
    assert me.resolve_actionable_max_age_minutes("api-football") == 120
    quote = _quote(market="asian_handicap", selection="HOME", line=-0.5, source_timestamp=_ago(180), captured_at=_ago(180))
    eligibility = me.pre_match_value_eligibility(quote, now_utc=NOW)
    assert not eligibility.eligible
    assert eligibility.actionable_max_age_minutes == 120


@pytest.mark.parametrize("bad", ["0", "-5", "abc"])
def test_invalid_actionable_threshold_fails_closed(monkeypatch, bad):
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES", bad)
    assert me.resolve_actionable_max_age_minutes("api-football") is None
    quote = _quote(market="asian_handicap", selection="HOME", line=-0.5, source_timestamp=_ago(5), captured_at=_ago(5))
    eligibility = me.pre_match_value_eligibility(quote, now_utc=NOW)
    assert not eligibility.eligible
    assert eligibility.quote_state == me.QUOTE_STATE_INVALID


def test_unknown_provider_does_not_receive_permissive_threshold(monkeypatch):
    monkeypatch.delenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", raising=False)
    assert me.resolve_actionable_max_age_minutes("the-odds-api") is None
    assert me.resolve_actionable_max_age_minutes("the-odds-api") != 360
    quote = _quote(market="asian_handicap", selection="HOME", line=-0.5, provider="the-odds-api")
    eligibility = me.pre_match_value_eligibility(quote, now_utc=NOW)
    assert not eligibility.eligible
    assert eligibility.quote_state == me.QUOTE_STATE_INVALID


def test_collector_health_threshold_is_separate_from_decision(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_FOOTBALL_COLLECTOR_HEALTH_MAX_AGE_MINUTES", "120")
    assert me.resolve_collector_health_max_age_minutes() == 120
    monkeypatch.delenv("SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES", raising=False)
    assert me.resolve_actionable_max_age_minutes("api-football") == 240


def test_legacy_evidence_env_var_only_controls_collector_health(monkeypatch):
    monkeypatch.delenv("SPORTS_AI_FOOTBALL_COLLECTOR_HEALTH_MAX_AGE_MINUTES", raising=False)
    monkeypatch.setenv("SPORTS_AI_FOOTBALL_EVIDENCE_MAX_AGE_MINUTES", "999")
    monkeypatch.delenv("SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES", raising=False)
    assert me.resolve_collector_health_max_age_minutes() == 999
    # Even a huge legacy "evidence" value cannot loosen the 240m decision gate.
    assert me.resolve_actionable_max_age_minutes("api-football") == 240


def test_invalid_collector_health_env_falls_back_to_cadence_default(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_FOOTBALL_COLLECTOR_HEALTH_MAX_AGE_MINUTES", "0")
    assert me.resolve_collector_health_max_age_minutes() == 360


# --------------------------------------------------------------------------- identity
def _identity_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cf.ensure_schema(conn)
    return conn


def test_fixture_identity_resolved_via_alias_registry():
    conn = _identity_db()
    canonical, _ = cf.resolve_or_create_canonical_fixture(
        conn,
        provider="api-football",
        provider_fixture_id="12345",
        competition_id="ENG1",
        home_team_name="Home FC",
        away_team_name="Away FC",
        kickoff_utc=KICKOFF,
    )
    resolution = me.resolve_fixture_identity(
        conn,
        provider="api-football",
        provider_fixture_id="12345",
        initialize_schema=False,
    )
    assert resolution.resolved
    assert resolution.canonical_fixture_id == canonical


def test_fixture_identity_mismatch_blocked():
    conn = _identity_db()
    cf.resolve_or_create_canonical_fixture(
        conn,
        provider="api-football",
        provider_fixture_id="12345",
        competition_id="ENG1",
        home_team_name="Home FC",
        away_team_name="Away FC",
        kickoff_utc=KICKOFF,
    )
    resolution = me.resolve_fixture_identity(
        conn,
        provider="api-football",
        provider_fixture_id="12345",
        expected_canonical_fixture_id="a-different-fixture",
        initialize_schema=False,
    )
    assert resolution.state == me.MARKET_IDENTITY_UNRESOLVED
    assert resolution.canonical_fixture_id is None


def test_fixture_identity_safe_degrade_when_registry_missing():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row  # no canonical schema created
    resolution = me.resolve_fixture_identity(
        conn,
        provider="api-football",
        provider_fixture_id="999",
        initialize_schema=False,
    )
    assert resolution.state == me.MARKET_IDENTITY_UNRESOLVED


# --------------------------------------------------------------------------- chronology
def _stored(**overrides) -> pt.StoredMarketSnapshot:
    base = {
        "fixture_id": "f1",
        "provider": "api-football",
        "bookmaker": "Pinnacle",
        "market": "asian_handicap",
        "selection": "HOME",
        "odds": 1.9,
        "ingestion_version": "api_football_current_market_provider_v1",
        "line": -0.5,
        "provider_timestamp": "2026-10-01T10:00:00+00:00",
        "captured_at": "2026-10-01T10:01:00+00:00",
        "kickoff_utc": KICKOFF,
    }
    base.update(overrides)
    return pt.StoredMarketSnapshot(**base)


def test_duplicate_observation_keeps_first_and_advances_last():
    conn = pt.connect(":memory:")
    pt.ensure_schema(conn)
    first_id = pt.record_market_snapshot(conn, _stored())
    second_id = pt.record_market_snapshot(
        conn, dataclasses.replace(_stored(), captured_at="2026-10-01T10:31:00+00:00")
    )
    assert first_id == second_id
    rows = pt.fetch_market_snapshots(conn, fixture_id="f1")
    assert len(rows) == 1
    row = rows[0]
    assert row["captured_at"] == "2026-10-01T10:01:00+00:00"
    assert row["first_observed_at"] == "2026-10-01T10:01:00+00:00"
    assert row["last_observed_at"] == "2026-10-01T10:31:00+00:00"
    assert row["provider_timestamp"] == "2026-10-01T10:00:00+00:00"


def test_last_observed_never_goes_backwards():
    conn = pt.connect(":memory:")
    pt.ensure_schema(conn)
    pt.record_market_snapshot(conn, _stored(captured_at="2026-10-01T11:00:00+00:00"))
    pt.record_market_snapshot(conn, dataclasses.replace(_stored(), captured_at="2026-10-01T10:00:00+00:00"))
    rows = pt.fetch_market_snapshots(conn, fixture_id="f1")
    assert rows[0]["last_observed_at"] == "2026-10-01T11:00:00+00:00"


# --------------------------------------------------------------------------- coverage
def _coverage_db() -> sqlite3.Connection:
    conn = pt.connect(":memory:")
    pt.ensure_schema(conn)
    return conn


def test_coverage_report_accuracy():
    conn = _coverage_db()
    pt.record_market_snapshot(
        conn,
        _stored(fixture_id="fA", selection="HOME", line=-0.5, captured_at=NOW, provider_timestamp=FRESH_SOURCE),
    )
    pt.record_market_snapshot(
        conn,
        _stored(fixture_id="fA", selection="AWAY", line=-0.5, odds=1.95, captured_at=NOW, provider_timestamp=FRESH_SOURCE),
    )
    pt.record_market_snapshot(
        conn,
        _stored(
            fixture_id="fB",
            market="corner_total",
            selection="OVER",
            line=9.5,
            captured_at=STALE_SOURCE,
            provider_timestamp=STALE_SOURCE,
        ),
    )
    pt.record_market_snapshot(
        conn,
        _stored(
            fixture_id="fB",
            market="corner_total",
            selection="UNDER",
            line=9.5,
            odds=1.95,
            captured_at=STALE_SOURCE,
            provider_timestamp=STALE_SOURCE,
        ),
    )
    fixtures = [
        {"fixture_id": "fA", "competition_id": "ENG1", "kickoff_utc": KICKOFF},
        {"fixture_id": "fB", "competition_id": "ESP1", "kickoff_utc": KICKOFF},
    ]
    report = me.build_market_evidence_coverage(
        conn=conn, fixtures=fixtures, now_utc=NOW, actionable_max_age_minutes=240
    )
    summary = report.summary()
    assert summary["fixtures"] == 2
    assert summary["asian_handicap"]["available"] == 1
    assert summary["corner_total"]["available"] == 0
    assert summary["corner_total"]["stale"] == 1
    assert summary["asian_handicap"]["missing"] == 1  # fB has no AH
    assert summary["corner_total"]["missing"] == 1  # fA has no corners
    assert summary["both_markets_coverage_pct"] == 0.0


def test_coverage_flags_identity_unresolved():
    conn = _coverage_db()
    report = me.build_market_evidence_coverage(
        conn=conn,
        fixtures=[{"fixture_id": "", "competition_id": "ENG1", "kickoff_utc": KICKOFF}],
        now_utc=NOW,
        actionable_max_age_minutes=240,
    )
    row = report.fixtures[0]
    assert row.asian_handicap_state == me.EVIDENCE_IDENTITY_UNRESOLVED
    assert row.corner_total_state == me.EVIDENCE_IDENTITY_UNRESOLVED


def test_coverage_reports_in_play_only_as_stale_not_available():
    conn = _coverage_db()
    pt.record_market_snapshot(
        conn,
        _stored(
            fixture_id="fLive",
            selection="HOME",
            line=-0.5,
            is_pre_match=False,
            captured_at="2026-10-07T16:00:00+00:00",
            provider_timestamp="2026-10-07T16:00:00+00:00",
        ),
    )
    report = me.build_market_evidence_coverage(
        conn=conn,
        fixtures=[{"fixture_id": "fLive", "competition_id": "ENG1", "kickoff_utc": KICKOFF}],
        now_utc="2026-10-07T16:05:00+00:00",
        actionable_max_age_minutes=240,
    )
    assert report.fixtures[0].asian_handicap_state == me.EVIDENCE_STALE


# --------------------------------------------------------------------------- budgets
def test_shadow_budget_guard_blocks_over_budget():
    budget = ShadowBudget(max_per_cycle=2, max_per_hour=3, clock=lambda: 0.0)
    budget.start_cycle()
    assert budget.can_spend(2)
    budget.record(2)
    assert not budget.can_spend(1)


def test_provider_rate_governor_guard_blocks_and_counts():
    governor = FiveDollarRateGovernor(max_per_hour=1, max_per_minute=1, clock=lambda: 0.0)
    assert governor.try_acquire()
    assert not governor.try_acquire()
    snapshot = governor.snapshot()
    assert snapshot["requests_allowed"] == 1
    assert snapshot["blocked_by_hour_cap"] == 1


# --------------------------------------------------------------------------- no mutation
def test_evidence_layer_does_not_mutate_paper_or_model_state():
    conn = _coverage_db()
    # A counted paper position and a candidate observation must be untouched.
    conn.execute(
        """
        INSERT INTO paper_trade_entries (
            paper_trade_id, prediction_id, fixture_id, model_snapshot_id, model_version, market,
            selection, model_fair_price, available_market_price, decision, decision_timestamp,
            kickoff, safeguard_state, entry_eligibility, entry_signature, created_at
        ) VALUES ('p1','pred1','fA','snap1','m1','asian_handicap','HOME',2.0,2.05,'PAPER_CANDIDATE',
                  '2026-10-07T11:00:00+00:00','2026-10-07T15:00:00+00:00','PASS','ELIGIBLE','sig1',
                  '2026-10-07T11:00:00+00:00')
        """
    )
    conn.commit()
    before_trades = conn.execute("SELECT COUNT(*) FROM paper_trade_entries").fetchone()[0]
    before_snapshots = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]

    me.build_market_evidence_coverage(
        conn=conn,
        fixtures=[{"fixture_id": "fA", "competition_id": "ENG1", "kickoff_utc": KICKOFF}],
        now_utc=NOW,
        actionable_max_age_minutes=240,
    )
    audit = me.audit_provider_request_accounting(conn)
    assert audit["historical_endpoint_used"] is False
    assert conn.execute("SELECT COUNT(*) FROM paper_trade_entries").fetchone()[0] == before_trades
    assert conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0] == before_snapshots


def test_collector_health_default_is_one_collection_cadence():
    assert me.resolve_collector_health_max_age_minutes() == 360


def test_documented_provider_limits_are_unchanged():
    limits = me.documented_provider_limits()
    assert limits["five_dollar_internal_cap_per_hour"] == 50
    assert limits["five_dollar_internal_cap_per_minute"] == 15
    assert limits["shadow_max_provider_calls_per_cycle"] == 5
    assert limits["shadow_max_provider_calls_per_hour"] == 20
    assert limits["limits_changed_by_evidence_lane"] is False
    assert limits["historical_api_calls"] is False


def test_run_diagnostics_is_read_only_and_consumes_zero_provider_requests(tmp_path: Path):
    dashboard_path = tmp_path / "dashboard.sqlite"
    dash = sqlite3.connect(dashboard_path)
    dash.row_factory = sqlite3.Row
    dash.execute(
        """
        CREATE TABLE dashboard_fixtures (
            fixture_id TEXT PRIMARY KEY, provider TEXT, provider_fixture_id TEXT,
            competition_id TEXT, competition_name TEXT, division_code TEXT,
            home_team_name TEXT, away_team_name TEXT, kickoff_utc TEXT, status TEXT
        )
        """
    )
    dash.execute(
        "INSERT INTO dashboard_fixtures VALUES ('fA','api-football','1','ENG1','Premier League','E0','Home FC','Away FC',?, 'PRE_MATCH')",
        (KICKOFF,),
    )
    dash.commit()
    dash.close()

    paper_path = tmp_path / "paper.sqlite"
    paper = pt.connect(paper_path)
    pt.ensure_schema(paper)
    pt.record_market_snapshot(
        paper,
        _stored(fixture_id="fA", selection="HOME", line=-0.5, captured_at=NOW, provider_timestamp=FRESH_SOURCE),
    )
    pt.record_market_snapshot(
        paper,
        _stored(fixture_id="fA", selection="AWAY", line=-0.5, odds=1.95, captured_at=NOW, provider_timestamp=FRESH_SOURCE),
    )
    paper.commit()
    before = paper.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    paper.close()

    report = run_diagnostics(
        dashboard_db_path=dashboard_path,
        paper_db_path=paper_path,
        now_utc=NOW,
    )
    assert report["read_only"] is True
    assert report["provider_requests_consumed"] == 0
    assert report["coverage"]["summary"]["asian_handicap"]["available"] == 1
    assert report["actionable_freshness_minutes_by_provider"]["api-football"] == 240
    assert report["collector_health_max_age_minutes"] == 360

    paper_check = pt.connect(paper_path)
    after = paper_check.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    paper_check.close()
    assert after == before


def test_evidence_modules_make_no_provider_or_ledger_calls():
    from sports_ai.diagnostics import football_live_market_evidence as diagnostics

    for module in (me, diagnostics):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "import httpx",
            "import requests",
            "import urllib",
            "from urllib",
            "football_model_pick_ledger",
            "record_paper_trade_entry",
            "record_candidate_observation",
        ):
            assert forbidden not in source

