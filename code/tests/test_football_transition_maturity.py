from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from sports_ai.dashboard.football_context_veto import (
    CONTEXT_VETO,
    REASON_NEWLY_PROMOTED_RELEGATED,
    evaluate_football_context_veto,
)
from sports_ai.dashboard.football_transition_maturity import (
    TRANSITION_BASELINE_VERSION,
    identify_transition_team,
    transition_maturity_for_fixture,
)
from sports_ai.dashboard.live_production_services import (
    LiveProductionDashboardServices,
    _enforce_transition_maturity_from_shadow,
)
from sports_ai.dashboard.team_aliases import resolve_provider_team_alias
from sports_ai.locking import canonical_hash


def _research_db(path: Path, *, santander_matches: int) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE football_events (
            competition_id TEXT,
            season_code TEXT,
            home_team TEXT,
            away_team TEXT,
            home_goals INTEGER,
            away_goals INTEGER
        )
        """
    )
    for index in range(santander_matches):
        if index % 2 == 0:
            home, away = "Santander", f"Opponent {index}"
        else:
            home, away = f"Opponent {index}", "Santander"
        conn.execute(
            "INSERT INTO football_events VALUES ('ESP1', '2627', ?, ?, 1, 1)",
            (home, away),
        )
    conn.commit()
    conn.close()
    return path


def test_transition_baseline_matches_provider_name_variants():
    deportivo = identify_transition_team(competition_id="ESP1", team_name="Deportivo La Coruña")
    racing = identify_transition_team(competition_id="ESP1", team_name="Racing Santander")
    hull = identify_transition_team(competition_id="ENG1", team_name="Hull City")

    assert deportivo is not None and deportivo.canonical_name == "La Coruna"
    assert racing is not None and racing.canonical_name == "Santander"
    assert hull is not None and hull.canonical_name == "Hull"


def test_api_football_transition_alias_is_deterministic():
    resolved = resolve_provider_team_alias(
        provider="api-football",
        competition_id="ESP1",
        provider_team_id="999",
        provider_team_name="Racing Santander",
    )

    assert resolved == {
        "canonical_team_name": "Santander",
        "resolution_kind": "api_football_transition_alias",
        "diagnostic_category": "E",
        "reason": "versioned 2026/27 transition baseline provides deterministic club identity",
    }


def test_transition_maturity_counts_current_competition_matches(tmp_path: Path):
    db_path = _research_db(tmp_path / "research.sqlite", santander_matches=4)

    status = transition_maturity_for_fixture(
        research_db_path=db_path,
        competition_id="ESP1",
        home_team_name="Barcelona",
        away_team_name="Racing Santander",
    )

    assert status is not None
    assert status["baseline_version"] == TRANSITION_BASELINE_VERSION
    assert status["season_matches_played"] == 4
    assert status["under_maturity_threshold"] is True
    assert status["teams"] == [
        {
            "side": "away",
            "canonical_name": "Santander",
            "movement": "promoted",
            "matches_played": 4,
        }
    ]


def test_transition_maturity_releases_after_eight_matches(tmp_path: Path):
    db_path = _research_db(tmp_path / "research.sqlite", santander_matches=8)

    status = transition_maturity_for_fixture(
        research_db_path=db_path,
        competition_id="ESP1",
        home_team_name="Barcelona",
        away_team_name="Racing Santander",
    )

    assert status is not None
    assert status["season_matches_played"] == 8
    assert status["under_maturity_threshold"] is False


def test_context_veto_keeps_transition_baseline_evidence():
    result = evaluate_football_context_veto(
        {
            "promoted_or_relegated": True,
            "season_matches_played": 4,
            "transition_baseline_version": TRANSITION_BASELINE_VERSION,
            "transition_minimum_required_matches": 8,
            "transition_teams": [
                {
                    "side": "away",
                    "canonical_name": "Santander",
                    "movement": "promoted",
                    "matches_played": 4,
                }
            ],
        }
    )

    assert result["status"] == CONTEXT_VETO
    assert REASON_NEWLY_PROMOTED_RELEGATED in result["reason_codes"]
    assert result["diagnostics"]["transition_baseline_version"] == TRANSITION_BASELINE_VERSION
    assert result["diagnostics"]["transition_minimum_required_matches"] == 8
    assert result["diagnostics"]["transition_teams"][0]["canonical_name"] == "Santander"


def test_transition_veto_is_enforced_even_while_other_context_is_shadowed():
    row = {
        "context_shadow_status": "VETO",
        "context_shadow_reason_codes": [REASON_NEWLY_PROMOTED_RELEGATED],
        "context_shadow_diagnostics": {
            "season_matches_played": 4,
            "transition_baseline_version": TRANSITION_BASELINE_VERSION,
        },
        "context_diagnostics": {},
        "paper_entry_eligible": True,
        "current_decision_state": "BET",
    }

    changed = _enforce_transition_maturity_from_shadow(row)

    assert changed is True
    assert row["paper_entry_eligible"] is False
    assert row["context_status"] == "VETO"
    assert row["context_reason_codes"] == [REASON_NEWLY_PROMOTED_RELEGATED]
    assert row["context_enforced"] is True
    assert row["context_enforcement_scope"] == "transition_maturity"
    assert row["current_decision_state"] == "NO_BET_CONTEXT_VETO"


def test_non_transition_shadow_veto_remains_shadow_only():
    row = {
        "context_shadow_status": "VETO",
        "context_shadow_reason_codes": ["ADVERSE_ODDS_MOVE_OVER_15_PERCENT"],
        "paper_entry_eligible": True,
    }

    changed = _enforce_transition_maturity_from_shadow(row)

    assert changed is False
    assert row["paper_entry_eligible"] is True
    assert "context_status" not in row


def test_transition_baseline_versions_live_prediction_identity():
    fixture = SimpleNamespace(
        fixture_id="api-football:example",
        kickoff_utc="2026-09-16T19:30:00+00:00",
    )
    snapshot = SimpleNamespace(snapshot_id="snapshot-current")

    version_key = LiveProductionDashboardServices._prediction_version_key(fixture, snapshot)
    old_key = canonical_hash(
        {
            "fixture_id": fixture.fixture_id,
            "model_snapshot_id": snapshot.snapshot_id,
            "kickoff_utc": fixture.kickoff_utc,
        }
    )

    assert version_key == canonical_hash(
        {
            "fixture_id": fixture.fixture_id,
            "model_snapshot_id": snapshot.snapshot_id,
            "kickoff_utc": fixture.kickoff_utc,
            "live_identity_policy_version": TRANSITION_BASELINE_VERSION,
        }
    )
    assert version_key != old_key
