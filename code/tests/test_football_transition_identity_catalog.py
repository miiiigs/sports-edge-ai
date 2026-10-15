from __future__ import annotations

import pytest

from sports_ai.dashboard.models import normalize_text_key
from sports_ai.dashboard.snapshot_service import CurrentFootballModelSnapshot


def _snapshot(canonical: dict[str, tuple[str, ...]]) -> CurrentFootballModelSnapshot:
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
        built_at="2026-10-15T00:00:00+00:00",
        model_as_of="2026-10-12T23:59:59+00:00",
        model_version="test",
        gap_engine=None,
        supported_competitions=tuple(canonical),
        competition_names={},
        team_catalog=team_catalog,
        alias_catalog={},
        conversion_coefficients={},
        corner_models={},
        rho_by_competition={},
        xi_by_competition={},
        team_model_history_index={},
        route_a_artifact=None,
        manifest_paths={},
        diagnostics={},
    )


@pytest.mark.parametrize(
    ("competition_id", "provider_name", "catalog_name"),
    [
        # Production regression (since 2026-09-13): the transition baseline names
        # "Lincoln City" / "MK Dons" while football-data research uses these names.
        ("ENG2", "Lincoln", "Lincoln"),
        ("ENG2", "Lincoln City", "Lincoln"),
        ("ENG3", "Milton Keynes Dons", "Milton Keynes Dons"),
        ("ENG3", "MK Dons", "Milton Keynes Dons"),
    ],
)
def test_transition_identity_resolves_to_research_catalog_spelling(
    competition_id: str, provider_name: str, catalog_name: str
) -> None:
    snapshot = _snapshot({"ENG2": ("Lincoln", "Millwall"), "ENG3": ("Milton Keynes Dons", "Luton")})

    resolved = snapshot.resolve_team(
        provider="api-football",
        competition_id=competition_id,
        provider_team_id=None,
        provider_team_name=provider_name,
    )

    assert resolved.resolution_kind == "api_football_transition_alias"
    assert resolved.canonical_team_name == catalog_name
    assert resolved.canonical_team_id == f"{competition_id}:{normalize_text_key(catalog_name)}"
    assert resolved.model_history_available is True


def test_transition_identity_keeps_baseline_name_when_it_matches_catalog() -> None:
    snapshot = _snapshot({"ENG1": ("Coventry", "Arsenal")})

    resolved = snapshot.resolve_team(
        provider="api-football",
        competition_id="ENG1",
        provider_team_id=None,
        provider_team_name="Coventry City",
    )

    assert resolved.canonical_team_name == "Coventry"
    assert resolved.model_history_available is True


def test_transition_identity_without_any_catalog_name_still_reports_missing_history() -> None:
    snapshot = _snapshot({"ENG3": ("Luton",)})

    resolved = snapshot.resolve_team(
        provider="api-football",
        competition_id="ENG3",
        provider_team_id=None,
        provider_team_name="Bromley",
    )

    assert resolved.resolution_kind == "api_football_transition_alias"
    assert resolved.canonical_team_name == "Bromley"
    assert resolved.model_history_available is False


def test_transition_identity_lookup_stays_competition_scoped() -> None:
    # "Lincoln" exists only in the ENG3 catalog; the ENG2 transition must not borrow it.
    snapshot = _snapshot({"ENG2": ("Millwall",), "ENG3": ("Lincoln",)})

    resolved = snapshot.resolve_team(
        provider="api-football",
        competition_id="ENG2",
        provider_team_id=None,
        provider_team_name="Lincoln",
    )

    assert resolved.model_history_available is False
