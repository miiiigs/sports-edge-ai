"""Deterministic identity normalization for persisted research ``football_events``.

The research store (football-data.co.uk) stores kickoff as a NAIVE UK LOCAL clock
time. The dashboard stores kickoff as a UTC instant. Comparing them directly with
the canonical identity (which uses an exact UTC kickoff and a zero tolerance)
therefore fails for every UK fixture during BST and for vocabulary differences.

This module normalizes the SOURCE SEMANTICS before canonical identity is computed:

* local kickoff + ``Europe/London`` -> timezone-aware UTC (via ``zoneinfo``, so
  BST/GMT and DST transitions are handled correctly; never a fixed ``-1h``);
* team identity reuses the existing deterministic machinery
  (``dashboard.team_aliases.resolve_provider_team_alias`` and
  ``storage.football_canonical_fixture.canonical_team_name``) -- no second alias
  system, no fuzzy matching.

It is PURE: no DB access, no writes. The global canonical policy
(``DEFAULT_KICKOFF_TOLERANCE_SECONDS = 0``) is untouched.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..storage.football_canonical_fixture import (
    canonical_fixture_id_from_identity,
    canonical_team_name,
)
from .team_aliases import resolve_provider_team_alias

RESEARCH_EVENT_IDENTITY_VERSION = "football_research_event_identity_v1"
RESEARCH_SOURCE_TIMEZONE = "Europe/London"
RESEARCH_SOURCE_PROVIDER = "football_research_event"

IDENTITY_MATCHED = "MATCHED"
IDENTITY_UNMATCHED = "UNMATCHED"
IDENTITY_AMBIGUOUS = "AMBIGUOUS"

SCORE_MATCH = "MATCH"
SCORE_CONFLICT = "CONFLICT"
SCORE_MISSING = "MISSING"

# football-data.co.uk full-time result codes only (never half-time codes).
VALID_FULL_TIME_RESULT_CODES = frozenset({"H", "D", "A"})
RESEARCH_FINAL = "FINAL"
RESEARCH_FINAL_MISSING = "MISSING"
RESEARCH_FINAL_NOT_FINAL = "NOT_FINAL"
RESEARCH_RESULT_CONFLICT = "RESEARCH_RESULT_CONFLICT"


def score_implied_result_code(home_goals: Any, away_goals: Any) -> str | None:
    if home_goals is None or away_goals is None:
        return None
    home, away = int(home_goals), int(away_goals)
    if home > away:
        return "H"
    if home < away:
        return "A"
    return "D"


def research_final_result(event: Mapping[str, Any]) -> dict[str, Any]:
    """Authoritative FINAL proof for a research event.

    Requires full-time score AND a valid full-time result_code (H/D/A) that
    AGREES with the score. Score presence alone is never FINAL. Half-time codes
    are never consulted.
    """
    home_goals = event.get("home_goals")
    away_goals = event.get("away_goals")
    result_code = str(event.get("result_code") or "").strip().upper()
    if home_goals is None or away_goals is None:
        return {"status": RESEARCH_FINAL_MISSING, "reason": "SCORE_MISSING", "result_code": result_code or None}
    if result_code not in VALID_FULL_TIME_RESULT_CODES:
        return {
            "status": RESEARCH_FINAL_NOT_FINAL,
            "reason": "RESULT_CODE_NOT_FINAL",
            "result_code": result_code or None,
        }
    implied = score_implied_result_code(home_goals, away_goals)
    if implied != result_code:
        return {
            "status": RESEARCH_RESULT_CONFLICT,
            "reason": RESEARCH_RESULT_CONFLICT,
            "result_code": result_code,
            "implied_result_code": implied,
        }
    return {"status": RESEARCH_FINAL, "reason": None, "result_code": result_code}


def _zone(tz_name: str = RESEARCH_SOURCE_TIMEZONE) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(f"missing IANA tz database for {tz_name!r} (install tzdata)") from exc


def local_kickoff_to_utc(
    match_date: Any,
    kickoff_time: Any,
    *,
    tz_name: str = RESEARCH_SOURCE_TIMEZONE,
) -> str | None:
    """Naive UK-local date+time -> explicit UTC ISO instant (DST-correct).

    Returns ``None`` when the date is missing/invalid. Preserves the source's
    local wall clock, then attaches the IANA zone so zoneinfo applies the correct
    UTC offset for that calendar date (BST = +01:00, GMT = +00:00).
    """
    if not match_date:
        return None
    date_text = str(match_date).strip()[:10]
    time_text = str(kickoff_time or "").strip() or "00:00"
    if len(time_text.split(":")) == 2:
        time_text = f"{time_text}:00"
    try:
        naive = datetime.fromisoformat(f"{date_text}T{time_text}")
    except ValueError:
        return None
    aware = naive.replace(tzinfo=_zone(tz_name))
    return aware.astimezone(timezone.utc).isoformat(timespec="seconds")


def canonical_dashboard_team_name(
    *,
    provider: str,
    competition_id: str,
    provider_team_id: str | None,
    provider_team_name: str,
) -> str:
    """Canonical club key for a dashboard fixture team (approved aliases only)."""
    resolved = resolve_provider_team_alias(
        provider=provider,
        competition_id=competition_id,
        provider_team_id=provider_team_id,
        provider_team_name=provider_team_name,
    )
    name = resolved["canonical_team_name"] if resolved else provider_team_name
    return canonical_team_name(name)


def canonical_research_team_name(team_name: Any) -> str:
    return canonical_team_name(team_name)


def research_event_identity(event: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one persisted research event to canonical identity (pure)."""
    home = canonical_research_team_name(event.get("home_team"))
    away = canonical_research_team_name(event.get("away_team"))
    kickoff_utc = local_kickoff_to_utc(event.get("match_date"), event.get("kickoff_time"))
    competition_id = str(event.get("competition_id") or "").upper()
    canonical_id = None
    if kickoff_utc and home and away and competition_id:
        canonical_id = canonical_fixture_id_from_identity(
            competition_id=competition_id,
            home_team_name=home,
            away_team_name=away,
            kickoff_utc=kickoff_utc,
        )
    return {
        "research_event_id": event.get("event_id"),
        "competition_id": competition_id,
        "raw_home_team": event.get("home_team"),
        "raw_away_team": event.get("away_team"),
        "raw_kickoff": f"{event.get('match_date')} {event.get('kickoff_time') or ''}".strip(),
        "canonical_home_team": home,
        "canonical_away_team": away,
        "normalized_kickoff_utc": kickoff_utc,
        "canonical_fixture_id": canonical_id,
        "home_goals": event.get("home_goals"),
        "away_goals": event.get("away_goals"),
        "result_code": event.get("result_code"),
        "timezone_assumption": RESEARCH_SOURCE_TIMEZONE,
        "normalization_version": RESEARCH_EVENT_IDENTITY_VERSION,
    }


def dashboard_fixture_identity(fixture: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one dashboard fixture to canonical identity (pure)."""
    fixture_id = str(fixture.get("fixture_id") or "")
    provider = fixture_id.partition(":")[0]
    competition_id = str(fixture.get("competition_id") or "").upper()
    home = canonical_dashboard_team_name(
        provider=provider,
        competition_id=competition_id,
        provider_team_id=fixture.get("home_team_id"),
        provider_team_name=str(fixture.get("home_team_name") or ""),
    )
    away = canonical_dashboard_team_name(
        provider=provider,
        competition_id=competition_id,
        provider_team_id=fixture.get("away_team_id"),
        provider_team_name=str(fixture.get("away_team_name") or ""),
    )
    kickoff_utc = fixture.get("kickoff_utc")
    canonical_id = None
    if kickoff_utc and home and away and competition_id:
        canonical_id = canonical_fixture_id_from_identity(
            competition_id=competition_id,
            home_team_name=home,
            away_team_name=away,
            kickoff_utc=str(kickoff_utc),
        )
    return {
        "fixture_id": fixture_id,
        "provider": provider,
        "competition_id": competition_id,
        "raw_home_team": fixture.get("home_team_name"),
        "raw_away_team": fixture.get("away_team_name"),
        "canonical_home_team": home,
        "canonical_away_team": away,
        "kickoff_utc": kickoff_utc,
        "canonical_fixture_id": canonical_id,
    }


def index_research_events(events: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        identity = research_event_identity(event)
        if identity["canonical_fixture_id"]:
            index.setdefault(identity["canonical_fixture_id"], []).append(identity)
    return index


def resolve_identity(
    dashboard_identity: Mapping[str, Any],
    research_index: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[str, list[Mapping[str, Any]]]:
    """MATCHED (exactly one) / AMBIGUOUS (multiple) / UNMATCHED (zero)."""
    canonical_id = dashboard_identity.get("canonical_fixture_id")
    if not canonical_id:
        return IDENTITY_UNMATCHED, []
    candidates = list(research_index.get(str(canonical_id), []))
    if len(candidates) == 1:
        return IDENTITY_MATCHED, candidates
    if len(candidates) > 1:
        return IDENTITY_AMBIGUOUS, candidates
    return IDENTITY_UNMATCHED, []


def validate_score(dashboard_score: tuple[Any, Any], research_event: Mapping[str, Any]) -> str:
    """Validate score agreement AFTER identity is established. Never used for identity."""
    home_goals = research_event.get("home_goals")
    away_goals = research_event.get("away_goals")
    if home_goals is None or away_goals is None:
        return SCORE_MISSING
    dash_home, dash_away = dashboard_score
    if dash_home is None or dash_away is None:
        return SCORE_MATCH  # dashboard score absent; research supplies it
    if (int(dash_home), int(dash_away)) == (int(home_goals), int(away_goals)):
        return SCORE_MATCH
    return SCORE_CONFLICT
