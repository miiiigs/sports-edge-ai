"""Focused tests for the research-event identity bridge (pure + manifest)."""

from __future__ import annotations

import sqlite3

import sports_ai.dashboard.services  # noqa: F401
from sports_ai.dashboard import football_research_identity as ident
from sports_ai.dashboard import football_stale_result_reconciliation as recon
from sports_ai.storage.football_canonical_fixture import (
    DEFAULT_KICKOFF_TOLERANCE_SECONDS,
    canonical_team_name,
)


def test_global_canonical_tolerance_unchanged():
    assert DEFAULT_KICKOFF_TOLERANCE_SECONDS == 0


def test_bst_local_to_utc():
    assert ident.local_kickoff_to_utc("2026-08-22", "12:30") == "2026-08-22T11:30:00+00:00"


def test_gmt_local_to_utc():
    assert ident.local_kickoff_to_utc("2026-01-15", "15:00") == "2026-01-15T15:00:00+00:00"


def test_dst_boundary_uses_zoneinfo_not_fixed_offset():
    # UK DST ends 2026-10-25; 10-24 is BST (+1), 10-26 is GMT (+0).
    assert ident.local_kickoff_to_utc("2026-10-24", "15:00") == "2026-10-24T14:00:00+00:00"
    assert ident.local_kickoff_to_utc("2026-10-26", "15:00") == "2026-10-26T15:00:00+00:00"


def test_ipswich_alias_is_deterministic():
    dashboard = ident.canonical_dashboard_team_name(
        provider="football-data.org", competition_id="ENG1", provider_team_id="349", provider_team_name="Ipswich Town"
    )
    research = ident.canonical_research_team_name("Ipswich")
    assert dashboard == research == canonical_team_name("Ipswich")


def test_nottingham_provider_name_variant_via_approved_alias():
    dashboard = ident.canonical_dashboard_team_name(
        provider="football-data.org", competition_id="ENG1", provider_team_id="351", provider_team_name="Nottingham"
    )
    research = ident.canonical_research_team_name("Nott'm Forest")
    assert dashboard == research


def _dash_fixture(**over):
    base = {
        "fixture_id": "football-data.org:999",
        "competition_id": "ENG1",
        "home_team_id": "349",
        "home_team_name": "Ipswich Town",
        "away_team_id": "71",
        "away_team_name": "Sunderland",
        "kickoff_utc": "2026-08-22T11:30:00+00:00",
    }
    base.update(over)
    return base


def _research_event(**over):
    base = {
        "event_id": "evt_1",
        "competition_id": "ENG1",
        "match_date": "2026-08-22",
        "kickoff_time": "12:30",
        "home_team": "Ipswich",
        "away_team": "Sunderland",
        "home_goals": 2,
        "away_goals": 1,
        "result_code": "H",
    }
    base.update(over)
    return base


def _resolve(dash, events):
    index = ident.index_research_events(events)
    return ident.resolve_identity(ident.dashboard_fixture_identity(dash), index)


def test_same_teams_normalized_exact_kickoff_matches_once():
    state, matches = _resolve(_dash_fixture(), [_research_event()])
    assert state == ident.IDENTITY_MATCHED
    assert len(matches) == 1


def test_wrong_kickoff_does_not_match():
    state, _ = _resolve(_dash_fixture(), [_research_event(kickoff_time="15:00")])
    assert state == ident.IDENTITY_UNMATCHED


def test_wrong_teams_do_not_match():
    state, _ = _resolve(_dash_fixture(), [_research_event(away_team="Middlesbrough")])
    assert state == ident.IDENTITY_UNMATCHED


def test_score_is_not_used_for_identity_ambiguous_fails_closed():
    # two events with the SAME identity (same teams/kickoff) but different scores
    events = [_research_event(home_goals=2, away_goals=1), _research_event(event_id="evt_2", home_goals=0, away_goals=0)]
    state, matches = _resolve(_dash_fixture(), events)
    assert state == ident.IDENTITY_AMBIGUOUS
    assert len(matches) == 2


def test_research_final_requires_valid_full_time_result_code():
    assert ident.research_final_result(_research_event(result_code="H"))["status"] == ident.RESEARCH_FINAL
    assert ident.research_final_result(_research_event(result_code=""))["status"] == ident.RESEARCH_FINAL_NOT_FINAL
    assert ident.research_final_result(_research_event(result_code="HH"))["status"] == ident.RESEARCH_FINAL_NOT_FINAL
    assert ident.research_final_result(_research_event(home_goals=None, result_code="H"))["status"] == ident.RESEARCH_FINAL_MISSING


def test_research_result_code_must_agree_with_score():
    result = ident.research_final_result(_research_event(home_goals=0, away_goals=0, result_code="H"))
    assert result["status"] == ident.RESEARCH_RESULT_CONFLICT
    assert result["implied_result_code"] == "D"


def test_validate_score_match_conflict_missing():
    event = _research_event()
    assert ident.validate_score((2, 1), event) == ident.SCORE_MATCH
    assert ident.validate_score((0, 0), event) == ident.SCORE_CONFLICT
    assert ident.validate_score((None, None), event) == ident.SCORE_MATCH  # dashboard absent
    assert ident.validate_score((2, 1), _research_event(home_goals=None)) == ident.SCORE_MISSING


def _manifest_dbs():
    dash = sqlite3.connect(":memory:")
    dash.row_factory = sqlite3.Row
    dash.execute(
        "CREATE TABLE dashboard_fixtures (fixture_id TEXT PRIMARY KEY, competition_id TEXT, competition_name TEXT, "
        "home_team_id TEXT, home_team_name TEXT, away_team_id TEXT, away_team_name TEXT, kickoff_utc TEXT, "
        "status TEXT, home_score INTEGER, away_score INTEGER)"
    )
    dash.execute(
        "INSERT INTO dashboard_fixtures VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("football-data.org:999", "ENG1", "Premier League", "349", "Ipswich Town", "71", "Sunderland",
         "2026-08-22T11:30:00+00:00", "LIVE", 2, 1),
    )
    led = sqlite3.connect(":memory:")
    led.row_factory = sqlite3.Row
    led.execute(
        "CREATE TABLE football_model_pick_entries (model_pick_id TEXT PRIMARY KEY, fixture_id TEXT, market TEXT, "
        "line REAL, selection TEXT, kickoff_utc TEXT, trade_state TEXT)"
    )
    led.execute(
        "INSERT INTO football_model_pick_entries VALUES (?,?,?,?,?,?,?)",
        ("mp1", "football-data.org:999", "asian_handicap", 0.0, "HOME", "2026-08-22T11:30:00+00:00", None),
    )
    return dash, led


def test_manifest_marks_live_stale_with_matching_final_evidence_eligible():
    dash, led = _manifest_dbs()
    manifest = recon.build_manifest(dash, led, [_research_event()])
    assert manifest["summary"] == {
        "cohort": 1,
        "matched": 1,
        "ambiguous": 0,
        "unmatched": 0,
        "final_results_proven": 1,
        "research_result_conflicts": 0,
        "score_agreeing": 1,
        "score_conflicts": 0,
        "eligible_status_repairs": 1,
    }
    assert manifest["rows"][0]["repair_eligible"] == "YES"
    assert manifest["rows"][0]["classification"] == "IDENTITY_MATCHED"
    dash.close()
    led.close()


def test_manifest_result_code_conflict_not_eligible():
    dash, led = _manifest_dbs()
    manifest = recon.build_manifest(dash, led, [_research_event(home_goals=0, away_goals=0, result_code="H")])
    assert manifest["summary"]["research_result_conflicts"] == 1
    assert manifest["summary"]["eligible_status_repairs"] == 0
    assert manifest["rows"][0]["classification"] == "RESEARCH_RESULT_CONFLICT"
    assert manifest["rows"][0]["repair_eligible"] == "NO"
    dash.close()
    led.close()


def test_manifest_score_conflict_is_not_eligible():
    dash, led = _manifest_dbs()
    manifest = recon.build_manifest(dash, led, [_research_event(home_goals=0, away_goals=0, result_code="D")])
    assert manifest["summary"]["score_conflicts"] == 1
    assert manifest["summary"]["eligible_status_repairs"] == 0
    assert manifest["rows"][0]["classification"] == "DASHBOARD_SCORE_CONFLICT"
    assert manifest["rows"][0]["repair_eligible"] == "REPAIR_BLOCKED"
    dash.close()
    led.close()


def test_module_is_evidence_only_no_network():
    import pathlib

    for module in (ident, recon):
        src = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("import httpx", "import requests", "five_dollar", "the_odds_api"):
            assert forbidden not in src
