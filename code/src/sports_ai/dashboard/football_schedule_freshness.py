"""Schedule-aware model-state freshness evidence for the Football product.

The default production gate (``calendar`` mode) declares a competition STALE
when its latest complete research match is older than a fixed number of
calendar days.  That proxy is blind to the competition's own schedule:

* an international break can trip it although no match has been missed;
* a CSV publication lag can leave completed matches out of the model while
  the calendar age is still inside the limit.

``schedule`` mode replaces the proxy with the requirement it approximates:
the model must contain every completed league match that kicked off before
the prediction.  A competition is FRESH only when the fixture provider knows
of no completed (or unverifiable past-kickoff) league fixture after the latest
complete research match, the same-day count on the cutoff date is covered,
and an absolute age ceiling still holds.  Any doubt fails closed.

The evaluator is pure: callers pass fixtures and research facts in.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from ..storage.football_canonical_fixture import canonical_team_name
from .models import (
    FIXTURE_STATUS_CANCELLED,
    FIXTURE_STATUS_FINISHED,
    FIXTURE_STATUS_POSTPONED,
    parse_iso_datetime,
)

FRESHNESS_MODE_ENV_VAR = "SPORTS_AI_FOOTBALL_MODEL_FRESHNESS_MODE"
FRESHNESS_MODE_CALENDAR = "calendar"
FRESHNESS_MODE_SCHEDULE = "schedule"
FRESHNESS_MODES = frozenset({FRESHNESS_MODE_CALENDAR, FRESHNESS_MODE_SCHEDULE})
SCHEDULE_MAX_AGE_ENV_VAR = "SPORTS_AI_FOOTBALL_SCHEDULE_FRESHNESS_MAX_AGE_DAYS"
DEFAULT_SCHEDULE_MAX_AGE_DAYS = 42
# A fixture that kicked off less than this long ago may legitimately still be
# in play; it is not yet a "completed match" the model could contain.
IN_PLAY_GRACE = timedelta(hours=3)
SCHEDULE_FRESHNESS_VERSION = "football_schedule_freshness_v1"
# football-data.co.uk Date/Time columns are UK local clock values.
_RESEARCH_TZ = ZoneInfo("Europe/London")
_NOT_PLAYED_STATUSES = frozenset({FIXTURE_STATUS_POSTPONED, FIXTURE_STATUS_CANCELLED})



def _research_local_date(kickoff_utc: Any) -> date | None:
    try:
        kickoff = parse_iso_datetime(str(kickoff_utc))
    except Exception:
        return None
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    return kickoff.astimezone(_RESEARCH_TZ).date()


def evaluate_schedule_freshness(
    *,
    competition_id: str,
    latest_complete_match_date: str,
    research_complete_matches_on_latest_date: int,
    fixtures: Iterable[Mapping[str, Any]],
    now: datetime,
    max_age_days: int = DEFAULT_SCHEDULE_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Return schedule completeness evidence and a FRESH/STALE verdict."""
    cutoff = date.fromisoformat(str(latest_complete_match_date)[:10])
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    age_days = max(0, (now_utc.astimezone(timezone.utc).date() - cutoff).days)

    competition_fixture_count = 0
    missed: set[tuple[str, str, str]] = set()
    unverified: set[tuple[str, str, str]] = set()
    finished_on_cutoff: set[tuple[str, str]] = set()
    for fixture in fixtures:
        if str(fixture.get("competition_id") or "").upper() != str(competition_id).upper():
            continue
        competition_fixture_count += 1
        local_day = _research_local_date(fixture.get("kickoff_utc"))
        if local_day is None or local_day < cutoff:
            continue
        status = str(fixture.get("status") or "").upper()
        if status in _NOT_PLAYED_STATUSES:
            continue
        kickoff = parse_iso_datetime(str(fixture.get("kickoff_utc")))
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        if kickoff >= now_utc:
            continue
        # Providers (and the legacy/current provider pair) can report the same
        # match twice; identity is competition + canonical teams + local day.
        pair = (
            canonical_team_name(fixture.get("home_team_name")),
            canonical_team_name(fixture.get("away_team_name")),
        )
        key = (local_day.isoformat(), *pair)
        if status == FIXTURE_STATUS_FINISHED:
            if local_day == cutoff:
                finished_on_cutoff.add(pair)
            else:
                missed.add(key)
            continue
        if now_utc - kickoff < IN_PLAY_GRACE:
            # Kicked off recently (pre-match status lag or genuinely in play):
            # not yet a completed match the model could contain.
            continue
        # Past kickoff + grace, not final, not postponed/cancelled: completion
        # cannot be ruled out, so the model cannot be proven to contain it.
        unverified.add(key)
    cutoff_day_gap = max(0, len(finished_on_cutoff) - int(research_complete_matches_on_latest_date))

    reasons: list[str] = []
    if competition_fixture_count == 0:
        reasons.append("NO_PROVIDER_SCHEDULE_EVIDENCE")
    if missed:
        reasons.append("COMPLETED_MATCH_MISSING_FROM_RESEARCH")
    if cutoff_day_gap:
        reasons.append("CUTOFF_DAY_MATCH_MISSING_FROM_RESEARCH")
    if unverified:
        reasons.append("PAST_FIXTURE_COMPLETION_UNVERIFIED")
    if age_days > int(max_age_days):
        reasons.append("SCHEDULE_MAX_AGE_EXCEEDED")
    return {
        "schedule_freshness_version": SCHEDULE_FRESHNESS_VERSION,
        "state": "STALE" if reasons else "FRESH",
        "reasons": reasons,
        "age_days": age_days,
        "schedule_max_age_days": int(max_age_days),
        "provider_fixtures_considered": competition_fixture_count,
        "completed_matches_missing": len(missed) + cutoff_day_gap,
        "unverified_past_fixtures": len(unverified),
        "first_missing_match_date": min((row[0] for row in missed), default=None),
    }
