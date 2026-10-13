from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from sports_ai.dashboard.football_schedule_freshness import (
    FRESHNESS_MODE_CALENDAR,
    FRESHNESS_MODE_ENV_VAR,
    FRESHNESS_MODE_SCHEDULE,
    evaluate_schedule_freshness,
)
from sports_ai.dashboard.production_services import (
    ProductionDashboardServices,
    _model_freshness_mode,
)
from sports_ai.storage import football_research_store

NOW = datetime(2026, 10, 12, 13, 0, tzinfo=timezone.utc)
CUTOFF = "2026-09-20"


def _fx(home: str, away: str, kickoff: str, status: str = "FINISHED", competition_id: str = "ENG1") -> dict:
    return {
        "competition_id": competition_id,
        "home_team_name": home,
        "away_team_name": away,
        "kickoff_utc": kickoff,
        "status": status,
    }


def _evaluate(fixtures, *, on_cutoff: int = 4, max_age_days: int = 42, cutoff: str = CUTOFF):
    return evaluate_schedule_freshness(
        competition_id="ENG1",
        latest_complete_match_date=cutoff,
        research_complete_matches_on_latest_date=on_cutoff,
        fixtures=fixtures,
        now=NOW,
        max_age_days=max_age_days,
    )


def _break_schedule() -> list[dict]:
    # Research contains the 4 matches of 2026-09-20; the league then pauses for
    # an international break and resumes after NOW.
    return [
        _fx("Arsenal", "Chelsea", "2026-09-20T13:00:00+00:00"),
        _fx("Everton", "Fulham", "2026-09-20T13:00:00+00:00"),
        _fx("Brentford", "Burnley", "2026-09-20T15:30:00+00:00"),
        _fx("Leeds", "Wolves", "2026-09-20T15:30:00+00:00"),
        _fx("Spurs", "Brighton", "2026-10-17T11:30:00+00:00", status="PRE_MATCH"),
    ]


def test_international_break_without_missed_matches_is_fresh_despite_calendar_age() -> None:
    result = _evaluate(_break_schedule())
    assert result["age_days"] == 22  # the calendar gate (21 days) would say STALE
    assert result["state"] == "FRESH"
    assert result["reasons"] == []
    assert result["completed_matches_missing"] == 0


def test_completed_match_missing_from_research_blocks() -> None:
    fixtures = _break_schedule() + [_fx("Coventry", "Newcastle", "2026-10-10T14:00:00+00:00")]
    result = _evaluate(fixtures)
    assert result["state"] == "STALE"
    assert "COMPLETED_MATCH_MISSING_FROM_RESEARCH" in result["reasons"]
    assert result["completed_matches_missing"] == 1
    assert result["first_missing_match_date"] == "2026-10-10"


def test_completed_match_missing_blocks_even_inside_calendar_limit() -> None:
    # Calendar age 2 days (calendar gate FRESH) but a finished match is absent.
    fixtures = [
        _fx("Arsenal", "Chelsea", "2026-10-10T14:00:00+00:00"),
        _fx("Everton", "Fulham", "2026-10-11T14:00:00+00:00"),
    ]
    result = _evaluate(fixtures, on_cutoff=1, cutoff="2026-10-10")
    assert result["age_days"] == 2
    assert result["state"] == "STALE"
    assert result["completed_matches_missing"] == 1


def test_cutoff_day_partial_ingest_blocks_and_duplicate_provider_rows_do_not() -> None:
    fixtures = _break_schedule() + [
        # Same match reported by a second provider: one identity, not a gap.
        _fx("Arsenal", "Chelsea", "2026-09-20T13:00:00Z"),
    ]
    assert _evaluate(fixtures)["state"] == "FRESH"
    partial = _evaluate(_break_schedule(), on_cutoff=3)
    assert partial["state"] == "STALE"
    assert partial["reasons"] == ["CUTOFF_DAY_MATCH_MISSING_FROM_RESEARCH"]


def test_unverified_past_fixture_fails_closed_but_in_play_and_postponed_do_not() -> None:
    stale_status = _break_schedule() + [_fx("Spurs", "Villa", "2026-10-11T14:00:00+00:00", status="PRE_MATCH")]
    result = _evaluate(stale_status)
    assert result["state"] == "STALE"
    assert result["reasons"] == ["PAST_FIXTURE_COMPLETION_UNVERIFIED"]

    benign = _break_schedule() + [
        _fx("Spurs", "Villa", (NOW - timedelta(hours=1)).isoformat(), status="LIVE"),
        _fx("Spurs", "Leeds", "2026-10-04T14:00:00+00:00", status="POSTPONED"),
        _fx("Spurs", "Hull", "2026-10-05T14:00:00+00:00", status="CANCELLED"),
        _fx("Other", "League", "2026-10-11T14:00:00+00:00", competition_id="ENG2"),
    ]
    assert _evaluate(benign)["state"] == "FRESH"


def test_no_schedule_evidence_and_age_ceiling_fail_closed() -> None:
    assert _evaluate([])["reasons"] == ["NO_PROVIDER_SCHEDULE_EVIDENCE"]
    capped = _evaluate(_break_schedule(), max_age_days=21)
    assert capped["state"] == "STALE"
    assert capped["reasons"] == ["SCHEDULE_MAX_AGE_EXCEEDED"]


def test_mode_defaults_to_calendar(monkeypatch) -> None:
    monkeypatch.delenv(FRESHNESS_MODE_ENV_VAR, raising=False)
    assert _model_freshness_mode() == FRESHNESS_MODE_CALENDAR
    monkeypatch.setenv(FRESHNESS_MODE_ENV_VAR, "bogus")
    assert _model_freshness_mode() == FRESHNESS_MODE_CALENDAR
    monkeypatch.setenv(FRESHNESS_MODE_ENV_VAR, "Schedule")
    assert _model_freshness_mode() == FRESHNESS_MODE_SCHEDULE


def _research_db(tmp_path, match_day: date):
    path = tmp_path / "research.sqlite"
    with football_research_store.connect(path) as conn:
        football_research_store.ensure_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO football_events (
                    event_id, competition_id, competition_name, season_code, division_code,
                    match_date, home_team, away_team, home_goals, away_goals,
                    home_shots, away_shots, home_shots_on_target, away_shots_on_target,
                    home_corners, away_corners
                ) VALUES ('e1', 'ENG1', 'Premier League', '2627', 'E0', ?, 'Arsenal', 'Chelsea',
                          1, 0, 10, 8, 4, 3, 5, 6)
                """,
                (match_day.isoformat(),),
            )
    return path


def _service(tmp_path, monkeypatch, *, mode: str, fixtures: list[dict], match_day: date):
    services = ProductionDashboardServices.__new__(ProductionDashboardServices)
    services.config = SimpleNamespace(
        research_db_path=_research_db(tmp_path, match_day),
        dashboard_db_path=tmp_path / "dash.sqlite",
    )
    services._max_model_state_age_days = 21
    services._model_freshness_mode = mode
    services._schedule_freshness_max_age_days = 42
    monkeypatch.setattr(ProductionDashboardServices, "_schedule_freshness_fixtures", lambda self: fixtures)
    services._refresh_competition_freshness()
    return services


def test_calendar_mode_freshness_payload_is_unchanged(tmp_path, monkeypatch) -> None:
    match_day = datetime.now(timezone.utc).date() - timedelta(days=22)
    services = _service(tmp_path, monkeypatch, mode=FRESHNESS_MODE_CALENDAR, fixtures=[], match_day=match_day)
    # Exact legacy keys: the dict feeds the stale-prediction version key.
    assert services._competition_model_freshness == {
        "ENG1": {
            "latest_complete_match_date": match_day.isoformat(),
            "age_days": 22,
            "max_age_days": 21,
            "state": "STALE",
        }
    }


def test_schedule_mode_service_fresh_over_break_and_blocks_missing_match(tmp_path, monkeypatch) -> None:
    today = datetime.now(timezone.utc)
    match_day = today.date() - timedelta(days=22)
    played = _fx("Arsenal", "Chelsea", f"{match_day.isoformat()}T13:00:00+00:00")
    upcoming = _fx("Spurs", "Brighton", (today + timedelta(days=3)).isoformat(), status="PRE_MATCH")
    services = _service(
        tmp_path, monkeypatch, mode=FRESHNESS_MODE_SCHEDULE, fixtures=[played, upcoming], match_day=match_day
    )
    row = services._competition_model_freshness["ENG1"]
    assert row["calendar_state"] == "STALE"
    assert row["state"] == "FRESH"
    assert services._competition_is_fresh("ENG1")

    missing = _fx("Everton", "Fulham", (today - timedelta(days=2)).isoformat())
    blocked = _service(
        tmp_path / "b", monkeypatch, mode=FRESHNESS_MODE_SCHEDULE, fixtures=[played, missing, upcoming], match_day=match_day
    )
    row = blocked._competition_model_freshness["ENG1"]
    assert row["state"] == "STALE"
    assert row["schedule"]["completed_matches_missing"] == 1
    assert not blocked._competition_is_fresh("ENG1")
    warning = blocked._stale_model_state_warning("ENG1", match_day.isoformat(), row)
    assert "COMPLETED_MATCH_MISSING_FROM_RESEARCH" in warning
