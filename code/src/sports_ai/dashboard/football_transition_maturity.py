"""Versioned 2026/27 promotion/relegation maturity evidence for Football.

The controlling Football policy vetoes clubs that changed division until they
have eight completed matches in the *current* competition.  This module keeps
that fact separate from provider naming and from Route B model pricing.

Only deterministic league-membership transitions are listed here.  Match counts
come from the production research database, so the threshold advances naturally
without a manual date switch or probability adjustment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage import football_research_store
from .models import normalize_text_key

TRANSITION_BASELINE_VERSION = "football_transition_maturity_2627_v1"
TRANSITION_SEASON_CODE = "2627"
TRANSITION_MIN_MATCHES = 8


@dataclass(frozen=True)
class TransitionTeam:
    canonical_name: str
    movement: str
    aliases: tuple[str, ...]

    @property
    def normalized_aliases(self) -> frozenset[str]:
        return frozenset(normalize_text_key(value) for value in (self.canonical_name, *self.aliases))


# Sources are the official/competition-confirmed 2026/27 membership changes.
# The aliases intentionally include the naming styles used by API-Football and
# football-data.co.uk so identity matching never depends on fuzzy inference.
_TRANSITION_TEAMS: dict[str, tuple[TransitionTeam, ...]] = {
    "ENG1": (
        TransitionTeam("Coventry", "promoted", ("Coventry City",)),
        TransitionTeam("Ipswich", "promoted", ("Ipswich Town",)),
        TransitionTeam("Hull", "promoted", ("Hull City",)),
    ),
    "ENG2": (
        TransitionTeam("Wolves", "relegated", ("Wolverhampton", "Wolverhampton Wanderers")),
        TransitionTeam("Burnley", "relegated", ()),
        TransitionTeam("West Ham", "relegated", ("West Ham United",)),
        TransitionTeam("Lincoln City", "promoted", ("Lincoln",)),
        TransitionTeam("Cardiff", "promoted", ("Cardiff City",)),
        TransitionTeam("Bolton", "promoted", ("Bolton Wanderers",)),
    ),
    "ENG3": (
        TransitionTeam("Oxford", "relegated", ("Oxford United",)),
        TransitionTeam("Leicester", "relegated", ("Leicester City",)),
        TransitionTeam("Sheffield Weds", "relegated", ("Sheffield Wednesday",)),
        TransitionTeam("Bromley", "promoted", ()),
        TransitionTeam("MK Dons", "promoted", ("Milton Keynes Dons",)),
        TransitionTeam("Cambridge", "promoted", ("Cambridge United",)),
        TransitionTeam("Notts County", "promoted", ("Notts Co",)),
    ),
    "ENG4": (
        TransitionTeam("Exeter", "relegated", ("Exeter City",)),
        TransitionTeam("Port Vale", "relegated", ()),
        TransitionTeam("Rotherham", "relegated", ("Rotherham United",)),
        TransitionTeam("Northampton", "relegated", ("Northampton Town",)),
        TransitionTeam("York", "promoted", ("York City",)),
        TransitionTeam("Rochdale", "promoted", ()),
    ),
    "ESP1": (
        TransitionTeam(
            "Santander",
            "promoted",
            ("Racing Santander", "Real Racing Club", "Racing de Santander"),
        ),
        TransitionTeam(
            "La Coruna",
            "promoted",
            ("Deportivo La Coruna", "Deportivo La Coruña", "RC Deportivo", "Deportivo"),
        ),
        TransitionTeam("Malaga", "promoted", ("Malaga CF", "Málaga", "Málaga CF")),
    ),
}


def transition_teams_for_competition(competition_id: str) -> tuple[TransitionTeam, ...]:
    return _TRANSITION_TEAMS.get(str(competition_id), ())


def identify_transition_team(*, competition_id: str, team_name: str) -> TransitionTeam | None:
    normalized = normalize_text_key(team_name)
    if not normalized:
        return None
    for team in transition_teams_for_competition(competition_id):
        if normalized in team.normalized_aliases:
            return team
    return None


def _completed_match_counts(
    *,
    research_db_path: Path,
    competition_id: str,
    season_code: str,
) -> dict[str, int]:
    if not Path(research_db_path).exists():
        return {}
    with football_research_store.connect(Path(research_db_path)) as conn:
        rows = conn.execute(
            """
            SELECT home_team, away_team
            FROM football_events
            WHERE competition_id = ?
              AND season_code = ?
              AND home_goals IS NOT NULL
              AND away_goals IS NOT NULL
            """,
            (str(competition_id), str(season_code)),
        ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        for raw_name in (row["home_team"], row["away_team"]):
            normalized = normalize_text_key(str(raw_name or ""))
            if normalized:
                counts[normalized] = counts.get(normalized, 0) + 1
    return counts


def transition_maturity_for_fixture(
    *,
    research_db_path: Path,
    competition_id: str,
    home_team_name: str,
    away_team_name: str,
    season_code: str = TRANSITION_SEASON_CODE,
) -> dict[str, Any] | None:
    """Return deterministic transition evidence for a fixture when applicable."""
    identified: list[tuple[str, TransitionTeam]] = []
    for side, name in (("home", home_team_name), ("away", away_team_name)):
        team = identify_transition_team(competition_id=competition_id, team_name=name)
        if team is not None:
            identified.append((side, team))
    if not identified:
        return None

    match_counts = _completed_match_counts(
        research_db_path=Path(research_db_path),
        competition_id=competition_id,
        season_code=season_code,
    )
    teams: list[dict[str, Any]] = []
    for side, team in identified:
        # A provider/source may change between approved name variants during a
        # season.  Sum all approved aliases so that a harmless naming change does
        # not reset maturity back to zero.
        matches_played = sum(match_counts.get(alias, 0) for alias in team.normalized_aliases)
        teams.append(
            {
                "side": side,
                "canonical_name": team.canonical_name,
                "movement": team.movement,
                "matches_played": int(matches_played),
            }
        )

    minimum_matches = min(int(row["matches_played"]) for row in teams)
    return {
        "baseline_version": TRANSITION_BASELINE_VERSION,
        "season_code": str(season_code),
        "competition_id": str(competition_id),
        "promoted_or_relegated": True,
        "season_matches_played": minimum_matches,
        "minimum_required_matches": TRANSITION_MIN_MATCHES,
        "under_maturity_threshold": minimum_matches < TRANSITION_MIN_MATCHES,
        "teams": teams,
    }
