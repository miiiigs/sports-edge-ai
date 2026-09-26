"""Focused tests for leakage-safe Day 12 rolling xG and live wiring."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from sports_ai.dashboard.current_market import build_fixture_market_evaluation
import sports_ai.dashboard.services as services_module
from sports_ai.dashboard.services import DashboardConfig, DashboardServices
from sports_ai.dashboard.day12_rolling_xg import (
    DEFAULT_MIN_WINDOW_MATCHES,
    SOURCE_IDENTIFIER,
    build_fixture_xg_rows,
    build_team_rolling_xg_row,
    build_xg_inspection,
    missing_xg_inspection,
)
from sports_ai.experiments.football_day12_safeguards import (
    DAY12_BLOCKED_MISSING_XG_DATA,
    DAY12_REJECTED_BY_XG_VETO,
    DAY12_REVIEW_REQUIRED,
    XG_APPLICABLE_COMPETITIONS,
)
from sports_ai.market_data.providers import NormalizedMarketSnapshot, NotConfiguredMarketProvider

FIXTURE_ID = "football-data.org:560546"
MODEL_AS_OF = "2026-05-24T23:59:59+00:00"
KICKOFF = "2026-08-23T14:00:00+00:00"

COEFFS = {
    "home": {"beta_shots": 0.02, "beta_shots_on_target": 0.08},
    "away": {"beta_shots": 0.015, "beta_shots_on_target": 0.075},
}


def _seed_events(db_path, rows, competition_id="ENG1"):
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE IF EXISTS football_events")
    conn.execute(
        """
        CREATE TABLE football_events (
            event_id TEXT, match_date TEXT, kickoff_time TEXT, home_team TEXT, away_team TEXT,
            home_shots REAL, away_shots REAL, home_shots_on_target REAL, away_shots_on_target REAL,
            home_goals REAL, away_goals REAL, competition_id TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO football_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(*row, competition_id) for row in rows],
    )
    conn.commit()
    conn.close()


def _event(event_id, match_date, home, away, h_shots, a_shots, h_sot, a_sot):
    return (event_id, match_date, "15:00", home, away, h_shots, a_shots, h_sot, a_sot, 1, 1)


def _rolling_row(fixture_id, role, recent, matches=5, window_end="2026-05-24T20:00:00+00:00"):
    return {
        "fixture_id": fixture_id,
        "team_role": role,
        "recent_xg_for_per_match": recent,
        "matches_in_window": matches,
        "as_of": window_end,
        "window_start_at": "2026-04-01T20:00:00+00:00",
        "window_end_at": window_end,
        "source": SOURCE_IDENTIFIER,
        "competition_id": "ENG1",
    }


def _snapshot(fixture_id, bookmaker, selection, price, reference, provider_market_id):
    return NormalizedMarketSnapshot(
        provider="the-odds-api",
        fixture_id=fixture_id,
        provider_fixture_id="odds_event_1",
        captured_at="2026-08-23T09:00:00+00:00",
        source_timestamp="2026-08-23T09:00:00+00:00",
        market="1x2",
        selection=selection,
        line=None,
        decimal_odds=price,
        bookmaker=bookmaker,
        ingestion_version="the_odds_api_current_market_provider_v1",
        provider_market_id=provider_market_id,
        is_pre_match=True,
        home_team_name="Home FC",
        away_team_name="Away FC",
        competition_id="ENG1",
        competition_name="Premier League",
        kickoff_utc=KICKOFF,
        freshness_state="FRESH",
        reference_designation="reference" if reference else "available",
        reference_rank=0 if reference else None,
    )


def _snapshots(fixture_id):
    return [
        _snapshot(fixture_id, "pinnacle", "HOME", 2.30, True, "pin-home"),
        _snapshot(fixture_id, "pinnacle", "DRAW", 3.40, True, "pin-draw"),
        _snapshot(fixture_id, "pinnacle", "AWAY", 3.20, True, "pin-away"),
        _snapshot(fixture_id, "bet365", "HOME", 3.20, False, "b365-home"),
        _snapshot(fixture_id, "bet365", "DRAW", 3.70, False, "b365-draw"),
        _snapshot(fixture_id, "bet365", "AWAY", 3.60, False, "b365-away"),
    ]


def _market_row(fixture_id, selection, win, loss, fair, competition_id="ENG1", competition_name="Premier League"):
    return {
        "as_of": MODEL_AS_OF,
        "competition_id": competition_id,
        "competition_name": competition_name,
        "division_code": "E0",
        "effective_loss_stake_probability": loss,
        "effective_win_stake_probability": win,
        "event_id": fixture_id,
        "fair_decimal_price": fair,
        "fixture_id": fixture_id,
        "kickoff_at": KICKOFF,
        "kickoff_time": "14:00",
        "line": None,
        "market_family": "1x2",
        "match_date": "2026-08-23",
        "model_versions": {"conversion_model_version": "football_day8_goal_conversion_v1"},
        "output_schema_version": "football_day10_market_price_record_v1",
        "pricing_model_version": "football_day10_market_pricing_v1",
        "season_code": "2026",
        "selection": selection,
        "settlement_engine_version": "football_day10_settlement_engine_v1",
        "settlement_pricing_policy_id": "zero_ev_with_partial_and_push_settlement_v1",
        "settlement_probabilities": {
            "full_win_probability": win,
            "full_loss_probability": loss,
            "push_probability": 0.0,
            "half_win_probability": 0.0,
            "half_loss_probability": 0.0,
        },
        "source_corner_object_id": None,
        "source_scoreline_object_id": f"scoreline_{fixture_id}",
    }


def _prediction(fixture_id, competition_id="ENG1", competition_name="Premier League"):
    return {
        "prediction_status": "READY",
        "fixture_id": fixture_id,
        "competition_id": competition_id,
        "competition_name": competition_name,
        "home_team_name": "Home FC",
        "away_team_name": "Away FC",
        "kickoff_utc": KICKOFF,
        "model_as_of": MODEL_AS_OF,
        "model_snapshot_id": "snapshot_1",
        "expected_goals_home": 1.6,
        "expected_goals_away": 1.2,
        "expected_total_goals": 2.8,
        "market_rows": [
            _market_row(fixture_id, "HOME", 0.35, 0.65, 2.60, competition_id, competition_name),
            _market_row(fixture_id, "DRAW", 0.30, 0.70, 3.40, competition_id, competition_name),
            _market_row(fixture_id, "AWAY", 0.35, 0.65, 2.90, competition_id, competition_name),
        ],
    }


def _fixture_context(fixture_id, competition_id="ENG1", competition_name="Premier League"):
    return {
        "fixture_id": fixture_id,
        "competition_id": competition_id,
        "competition_name": competition_name,
        "division_code": "E0",
        "season": "2026",
        "home_team_name": "Home FC",
        "away_team_name": "Away FC",
        "kickoff_utc": KICKOFF,
    }


def _home_candidate(evaluation):
    for row in evaluation.get("decision_rows") or []:
        if row["selection"] == "HOME":
            return row
    return None


# ---------------------------------------------------------------------------
# Generator tests
# ---------------------------------------------------------------------------


def test_historical_matches_only_and_as_of_cutoff(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed_events(
        db,
        [
            _event("e1", "2026-04-05", "Home FC", "Other", 10, 8, 4, 3),
            _event("e2", "2026-04-19", "Home FC", "Other", 9, 7, 3, 2),
            _event("e3", "2026-05-03", "Home FC", "Other", 12, 6, 5, 2),
            _event("e4", "2026-05-10", "Home FC", "Other", 8, 9, 2, 4),
            _event("e5", "2026-05-17", "Home FC", "Other", 11, 5, 4, 1),
            # future (after decision_as_of cutoff) must be excluded
            _event("e6", "2026-08-24", "Home FC", "Other", 20, 5, 10, 1),
        ],
    )
    row = build_team_rolling_xg_row(
        db_path=db,
        competition_id="ENG1",
        team="Home FC",
        team_role="HOME",
        coefficients=COEFFS,
        decision_as_of="2026-08-23T09:00:00+00:00",
    )
    assert row is not None
    assert row["matches_in_window"] == 5
    assert row["window_end_at"] == "2026-05-17T14:00:00+00:00"
    assert row["window_start_at"] == "2026-04-05T14:00:00+00:00"


def test_home_and_away_historical_stats_mapped_correctly(tmp_path):
    db = tmp_path / "events.sqlite"
    # Home FC played once at home and once away.
    _seed_events(
        db,
        [
            _event("e1", "2026-04-05", "Home FC", "Other", 10, 8, 4, 3),
            _event("e2", "2026-04-19", "Other", "Home FC", 8, 6, 3, 2),
        ],
    )
    home_row = build_team_rolling_xg_row(
        db_path=db, competition_id="ENG1", team="Home FC", team_role="HOME",
        coefficients=COEFFS, decision_as_of="2026-08-23T09:00:00+00:00",
    )
    # home match: 0.02*10 + 0.08*4 = 0.52 ; away match: 0.015*6 + 0.075*2 = 0.24 ; mean = 0.38
    assert home_row["recent_xg_for_per_match"] == pytest.approx(0.38)
    assert home_row["matches_in_window"] == 2


def test_coefficients_reused_frozen(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed_events(db, [_event("e1", "2026-04-05", "Home FC", "Other", 10, 8, 4, 3)])
    row = build_team_rolling_xg_row(
        db_path=db, competition_id="ENG1", team="Home FC", team_role="HOME",
        coefficients=COEFFS, decision_as_of="2026-08-23T09:00:00+00:00",
    )
    assert row["recent_xg_for_per_match"] == pytest.approx(0.02 * 10 + 0.08 * 4)


def test_rolling_deterministic(tmp_path):
    db = tmp_path / "events.sqlite"
    rows = [_event(f"e{i}", f"2026-0{4 + i // 4}-0{(i % 4) + 1}", "Home FC", "Other", 10 + i, 8, 4, 3) for i in range(6)]
    _seed_events(db, rows)
    a = build_team_rolling_xg_row(
        db_path=db, competition_id="ENG1", team="Home FC", team_role="HOME",
        coefficients=COEFFS, decision_as_of="2026-08-23T09:00:00+00:00",
    )
    b = build_team_rolling_xg_row(
        db_path=db, competition_id="ENG1", team="Home FC", team_role="HOME",
        coefficients=COEFFS, decision_as_of="2026-08-23T09:00:00+00:00",
    )
    assert a == b


def test_minimum_5_match_window(tmp_path):
    db = tmp_path / "events.sqlite"
    many = [_event(f"e{i}", f"2026-0{4 + i // 4}-0{(i % 4) + 1}", "Home FC", "Other", 10, 8, 4, 3) for i in range(7)]
    _seed_events(db, many)
    row = build_team_rolling_xg_row(
        db_path=db, competition_id="ENG1", team="Home FC", team_role="HOME",
        coefficients=COEFFS, decision_as_of="2026-08-23T09:00:00+00:00",
    )
    assert row["matches_in_window"] == DEFAULT_MIN_WINDOW_MATCHES == 5


def test_missing_history_returns_none_and_blocks(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed_events(db, [_event("e1", "2026-04-05", "Other A", "Other B", 10, 8, 4, 3)])
    row = build_team_rolling_xg_row(
        db_path=db, competition_id="ENG1", team="Unknown FC", team_role="HOME",
        coefficients=COEFFS, decision_as_of="2026-08-23T09:00:00+00:00",
    )
    assert row is None
    rows, inspection = build_fixture_xg_rows(
        db_path=db, competition_id="ENG1", fixture_id=FIXTURE_ID,
        home_team="Unknown FC", away_team="Unknown Away",
        decision_as_of="2026-08-23T09:00:00+00:00", coefficients=COEFFS,
    )
    assert inspection["exists"] is False
    assert (FIXTURE_ID, "HOME") not in rows
    assert (FIXTURE_ID, "AWAY") not in rows


def test_future_match_excluded_even_if_before_fixture_kickoff(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed_events(
        db,
        [
            _event("e1", "2026-04-05", "Home FC", "Other", 10, 8, 4, 3),
            # Match kicks off after decision_as_of (fetch time) -> must be excluded.
            _event("e2", "2026-08-23T14:00:00+00:00".split("T")[0], "Home FC", "Other", 10, 8, 4, 3),
        ],
    )
    row = build_team_rolling_xg_row(
        db_path=db, competition_id="ENG1", team="Home FC", team_role="HOME",
        coefficients=COEFFS, decision_as_of="2026-08-23T09:00:00+00:00",
    )
    assert row["matches_in_window"] == 1
    assert row["window_end_at"] == "2026-04-05T14:00:00+00:00"


def test_missing_research_db_blocks(tmp_path):
    # A missing/corrupt research DB must fail closed (block), not raise.
    missing_db = tmp_path / "does_not_exist.sqlite"
    rows, inspection = build_fixture_xg_rows(
        db_path=missing_db,
        competition_id="ENG1",
        fixture_id=FIXTURE_ID,
        home_team="Home FC",
        away_team="Away FC",
        decision_as_of="2026-08-23T09:00:00+00:00",
        coefficients=COEFFS,
    )
    assert inspection["exists"] is False
    assert len(rows) == 0


def test_partial_coefficients_fail_closed(tmp_path):
    db = tmp_path / "events.sqlite"
    _seed_events(db, [_event("e1", "2026-04-05", "Home FC", "Away FC", 10, 8, 4, 3)])
    partial = {"home": {"beta_shots": 0.02, "beta_shots_on_target": 0.08}}  # away missing
    rows, inspection = build_fixture_xg_rows(
        db_path=db,
        competition_id="ENG1",
        fixture_id=FIXTURE_ID,
        home_team="Home FC",
        away_team="Away FC",
        decision_as_of="2026-08-23T09:00:00+00:00",
        coefficients=partial,
    )
    assert len(rows) == 0
    assert inspection["exists"] is False


def test_home_only_profile_blocks_live():
    # Day 12 requires BOTH HOME and AWAY rows; HOME-only must block.
    rows = {(FIXTURE_ID, "HOME"): _rolling_row(FIXTURE_ID, "HOME", 1.5)}
    inspection = build_xg_inspection(rows)
    evaluation = build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID),
        prediction=_prediction(FIXTURE_ID),
        snapshots=_snapshots(FIXTURE_ID),
        current_xg_connected=True,
        xg_profile_provider=lambda: (rows, inspection),
    )
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == DAY12_BLOCKED_MISSING_XG_DATA
    assert row["paper_entry_eligible"] is False


def test_evaluation_failure_fails_closed():
    # A malformed expected-goals value must fail closed to BLOCKED, never crash the sync.
    prediction = _prediction(FIXTURE_ID)
    prediction["expected_home_goals"] = "not-a-number"
    rows = {
        (FIXTURE_ID, "HOME"): _rolling_row(FIXTURE_ID, "HOME", 1.5),
        (FIXTURE_ID, "AWAY"): _rolling_row(FIXTURE_ID, "AWAY", 1.1),
    }
    inspection = build_xg_inspection(rows)
    evaluation = build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID),
        prediction=prediction,
        snapshots=_snapshots(FIXTURE_ID),
        current_xg_connected=True,
        xg_profile_provider=lambda: (rows, inspection),
    )
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == DAY12_BLOCKED_MISSING_XG_DATA
    assert row["paper_entry_eligible"] is False


def test_not_applicable_xg_status_is_internally_consistent():
    from sports_ai.dashboard.current_market import _day12_state_from_xg_rule

    assert _day12_state_from_xg_rule({"status": "XG_PASS"}) == "PASSED"
    assert _day12_state_from_xg_rule({"status": "XG_MISSING"}) == DAY12_BLOCKED_MISSING_XG_DATA
    assert _day12_state_from_xg_rule({"status": "XG_VETO"}) == DAY12_REJECTED_BY_XG_VETO
    assert _day12_state_from_xg_rule({"status": "XG_REVIEW_REQUIRED"}) == DAY12_REVIEW_REQUIRED
    assert _day12_state_from_xg_rule({"status": "XG_NOT_APPLICABLE"}) == "NOT_APPLICABLE"


# ---------------------------------------------------------------------------
# Live wiring tests
# ---------------------------------------------------------------------------


def test_valid_xg_allows_passed_and_paper_eligible(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    rows = {
        (FIXTURE_ID, "HOME"): _rolling_row(FIXTURE_ID, "HOME", 1.5),
        (FIXTURE_ID, "AWAY"): _rolling_row(FIXTURE_ID, "AWAY", 1.1),
    }
    inspection = build_xg_inspection(rows)
    evaluation = build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID),
        prediction=_prediction(FIXTURE_ID),
        snapshots=_snapshots(FIXTURE_ID),
        current_xg_connected=True,
        xg_profile_provider=lambda: (rows, inspection),
        market_freshness_evaluated_at="2026-08-23T09:00:00+00:00",
    )
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == "PASSED"
    assert row["paper_entry_eligible"] is True


def test_missing_xg_blocks():
    evaluation = build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID),
        prediction=_prediction(FIXTURE_ID),
        snapshots=_snapshots(FIXTURE_ID),
        current_xg_connected=True,
        xg_profile_provider=lambda: ({}, missing_xg_inspection()),
    )
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == DAY12_BLOCKED_MISSING_XG_DATA
    assert row["paper_entry_eligible"] is False


def test_connected_alone_does_not_permit_paper_entry():
    evaluation = build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID),
        prediction=_prediction(FIXTURE_ID),
        snapshots=_snapshots(FIXTURE_ID),
        current_xg_connected=True,
        xg_profile_provider=None,
    )
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == DAY12_BLOCKED_MISSING_XG_DATA
    assert row["paper_entry_eligible"] is False


def test_xg_disabled_behavior_unchanged():
    evaluation = build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID),
        prediction=_prediction(FIXTURE_ID),
        snapshots=_snapshots(FIXTURE_ID),
        current_xg_connected=False,
    )
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == DAY12_BLOCKED_MISSING_XG_DATA
    assert row["paper_entry_eligible"] is False


def test_non_applicable_competition_unchanged(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    evaluation = build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID, competition_id="ENG3", competition_name="League One"),
        prediction=_prediction(FIXTURE_ID, competition_id="ENG3", competition_name="League One"),
        snapshots=_snapshots(FIXTURE_ID),
        current_xg_connected=False,
        market_freshness_evaluated_at="2026-08-23T09:00:00+00:00",
    )
    row = _home_candidate(evaluation)
    assert "ENG3" not in XG_APPLICABLE_COMPETITIONS
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == "PASSED"
    assert row["paper_entry_eligible"] is True


def test_xg_gap_veto():
    rows = {
        (FIXTURE_ID, "HOME"): _rolling_row(FIXTURE_ID, "HOME", 3.0),
        (FIXTURE_ID, "AWAY"): _rolling_row(FIXTURE_ID, "AWAY", 1.1),
    }
    inspection = build_xg_inspection(rows)
    evaluation = build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID),
        prediction=_prediction(FIXTURE_ID),
        snapshots=_snapshots(FIXTURE_ID),
        current_xg_connected=True,
        xg_profile_provider=lambda: (rows, inspection),
    )
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == DAY12_REJECTED_BY_XG_VETO
    assert row["paper_entry_eligible"] is False


def test_future_xg_timestamp_rejected():
    rows = {
        (FIXTURE_ID, "HOME"): _rolling_row(FIXTURE_ID, "HOME", 1.5, window_end="2026-08-25T20:00:00+00:00"),
        (FIXTURE_ID, "AWAY"): _rolling_row(FIXTURE_ID, "AWAY", 1.1, window_end="2026-08-25T20:00:00+00:00"),
    }
    inspection = build_xg_inspection(rows)
    evaluation = build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID),
        prediction=_prediction(FIXTURE_ID),
        snapshots=_snapshots(FIXTURE_ID),
        current_xg_connected=True,
        xg_profile_provider=lambda: (rows, inspection),
    )
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == DAY12_REVIEW_REQUIRED
    assert row["paper_entry_eligible"] is False


def test_window_too_small_blocks():
    rows = {
        (FIXTURE_ID, "HOME"): _rolling_row(FIXTURE_ID, "HOME", 1.5, matches=2),
        (FIXTURE_ID, "AWAY"): _rolling_row(FIXTURE_ID, "AWAY", 1.1, matches=2),
    }
    inspection = build_xg_inspection(rows, min_window_matches=5)
    evaluation = build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID),
        prediction=_prediction(FIXTURE_ID),
        snapshots=_snapshots(FIXTURE_ID),
        current_xg_connected=True,
        xg_profile_provider=lambda: (rows, inspection),
    )
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == DAY12_BLOCKED_MISSING_XG_DATA
    assert row["paper_entry_eligible"] is False


# ---------------------------------------------------------------------------
# Current-slate readiness audit
# ---------------------------------------------------------------------------


class _AuditFixtureProvider:
    def provider_name(self):
        return "audit-fixture-provider"

    def configured(self):
        return False


def _audit_services(tmp_path):
    research_db = tmp_path / "research.sqlite"
    research_db.touch()
    config = DashboardConfig(
        repo_root=tmp_path,
        research_db_path=research_db,
        dashboard_db_path=tmp_path / "dashboard.sqlite",
        paper_trading_db_path=tmp_path / "paper.sqlite",
        performance_output_dir=tmp_path / "performance",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        enable_scheduler=False,
        current_odds_connected=False,
        current_xg_connected=False,
    )
    fixture_provider = _AuditFixtureProvider()
    services = DashboardServices(
        config=config,
        fixture_provider=fixture_provider,
        market_provider=NotConfiguredMarketProvider(provider_label="audit-market"),
        statistics_provider=fixture_provider,
    )
    services.snapshot = SimpleNamespace(
        conversion_coefficients={"ENG1": COEFFS, "ENG2": COEFFS},
    )
    return services


def _audit_fixture(fixture_id, competition_id):
    return {
        "fixture_id": fixture_id,
        "competition_id": competition_id,
        "home_team_name": "Home FC",
        "away_team_name": "Away FC",
        "kickoff_utc": "2099-09-26T14:00:00+00:00",
        "status": "PRE_MATCH",
        "prediction": {
            "prediction_status": "READY",
            "team_resolution": {
                "home": {"canonical_team_name": "Home FC"},
                "away": {"canonical_team_name": "Away FC"},
            },
        },
    }


def test_current_xg_readiness_audit_reports_ready_and_missing_without_enabling_xg(tmp_path, monkeypatch):
    services = _audit_services(tmp_path)

    def fake_build_fixture_xg_rows(**kwargs):
        fixture_id = kwargs["fixture_id"]
        if fixture_id == "ready":
            rows = {
                (fixture_id, "HOME"): {"matches_in_window": 5},
                (fixture_id, "AWAY"): {"matches_in_window": 5},
            }
            return rows, {"sufficient_for_day12_rule": True, "reason": None}
        return {
            (fixture_id, "HOME"): {"matches_in_window": 4},
        }, {"sufficient_for_day12_rule": False, "reason": "XG_DATA_NOT_SUPPLIED"}

    monkeypatch.setattr(services_module, "build_fixture_xg_rows", fake_build_fixture_xg_rows)
    result = services._audit_current_xg_readiness(
        [_audit_fixture("ready", "ENG1"), _audit_fixture("missing", "ENG2")]
    )

    assert services.config.current_xg_connected is False
    assert result["state"] == "PARTIAL"
    assert result["fixtures_considered"] == 2
    assert result["fixtures_ready"] == 1
    assert result["fixtures_missing"] == 1
    assert result["competition_counts"]["ENG1"] == {"considered": 1, "ready": 1, "missing": 0}
    assert result["competition_counts"]["ENG2"] == {"considered": 1, "ready": 0, "missing": 1}
    assert result["missing_examples"][0]["fixture_id"] == "missing"
    assert result["missing_examples"][0]["home_matches"] == 4
    assert result["missing_examples"][0]["away_matches"] == 0


def test_current_xg_readiness_audit_is_explicit_when_slate_has_no_applicable_fixtures(tmp_path):
    services = _audit_services(tmp_path)
    result = services._audit_current_xg_readiness([_audit_fixture("league-one", "ENG3")])

    assert result["state"] == "NO_APPLICABLE_FIXTURES"
    assert result["fixtures_considered"] == 0
    assert result["fixtures_ready"] == 0
    assert result["fixtures_missing"] == 0
