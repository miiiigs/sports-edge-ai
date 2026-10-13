from __future__ import annotations

from sports_ai.dashboard.models import normalize_text_key
from sports_ai.dashboard.snapshot_service import CurrentFootballModelSnapshot, _load_alias_catalog


def _snapshot(alias_catalog):
    canonical = {
        "ENG1": ("Nott'm Forest", "Crystal Palace"),
        "ENG2": ("Stoke", "Middlesbrough"),
        "ENG3": ("Stockport", "Peterboro", "Mansfield"),
        "ENG4": ("Bristol Rvs", "Swindon", "Accrington", "Salford"),
    }
    team_catalog = {
        competition_id: {
            normalize_text_key(name): {
                "canonical_team_id": f"{competition_id}:{normalize_text_key(name)}",
                "canonical_team_name": name,
            }
            for name in names
        }
        for competition_id, names in canonical.items()
    }
    return CurrentFootballModelSnapshot(
        snapshot_id="test",
        built_at="2026-09-26T00:00:00+00:00",
        model_as_of="2026-09-20T23:59:59+00:00",
        model_version="test",
        gap_engine=None,
        supported_competitions=("ENG1", "ENG2", "ENG3", "ENG4"),
        competition_names={},
        team_catalog=team_catalog,
        alias_catalog=alias_catalog,
        conversion_coefficients={},
        corner_models={},
        rho_by_competition={},
        xi_by_competition={},
        team_model_history_index={},
        route_a_artifact=None,
        manifest_paths={},
        diagnostics={},
    )


def test_api_football_current_team_aliases_resolve_to_model_catalog(tmp_path) -> None:
    alias_catalog = _load_alias_catalog(tmp_path)
    snapshot = _snapshot(alias_catalog)

    expected = {
        ("ENG3", "Stockport County"): "Stockport",
        ("ENG3", "Peterborough"): "Peterboro",
        ("ENG3", "Peterborough United"): "Peterboro",
        ("ENG4", "Bristol Rovers"): "Bristol Rvs",
        ("ENG4", "Swindon Town"): "Swindon",
        ("ENG4", "Accrington ST"): "Accrington",
        ("ENG4", "Accrington Stanley"): "Accrington",
        ("ENG4", "Salford City"): "Salford",
    }

    for (competition_id, provider_name), canonical_name in expected.items():
        resolved = snapshot.resolve_team(
            provider="api-football",
            competition_id=competition_id,
            provider_team_id=None,
            provider_team_name=provider_name,
        )
        assert resolved.is_resolved
        assert resolved.canonical_team_name == canonical_name
        assert resolved.model_history_available is True


def test_current_team_aliases_remain_competition_scoped(tmp_path) -> None:
    alias_catalog = _load_alias_catalog(tmp_path)
    snapshot = _snapshot(alias_catalog)

    unresolved = snapshot.resolve_team(
        provider="api-football",
        competition_id="ENG3",
        provider_team_id=None,
        provider_team_name="Swindon Town",
    )

    assert not unresolved.is_resolved


def test_api_football_unresolved_production_teams_resolve_to_research_names(tmp_path) -> None:
    # Production 2026-10-12 dashboard_team_mappings rows (provider api-football)
    # that were unresolved, mapped to the football-data.co.uk research names.
    alias_catalog = _load_alias_catalog(tmp_path)
    snapshot = _snapshot(alias_catalog)
    expected = {
        ("ENG1", "65", "Nottingham Forest"): "Nott'm Forest",
        ("ENG2", "75", "Stoke City"): "Stoke",
        ("ENG3", "1374", "Mansfield Town"): "Mansfield",
    }
    for (competition_id, team_id, provider_name), canonical_name in expected.items():
        resolved = snapshot.resolve_team(
            provider="api-football",
            competition_id=competition_id,
            provider_team_id=team_id,
            provider_team_name=provider_name,
        )
        assert resolved.is_resolved, provider_name
        assert resolved.resolution_kind == "exact_normalized_name"
        assert resolved.canonical_team_name == canonical_name
        assert resolved.model_history_available is True


def test_new_aliases_do_not_leak_across_competitions_or_similar_clubs(tmp_path) -> None:
    alias_catalog = _load_alias_catalog(tmp_path)
    snapshot = _snapshot(alias_catalog)
    for competition_id, provider_name in (
        ("ENG2", "Nottingham Forest"),
        ("ENG1", "Stoke City"),
        ("ENG4", "Mansfield Town"),
        ("ENG1", "Notts County"),
    ):
        resolved = snapshot.resolve_team(
            provider="api-football",
            competition_id=competition_id,
            provider_team_id=None,
            provider_team_name=provider_name,
        )
        assert not resolved.is_resolved, (competition_id, provider_name)
