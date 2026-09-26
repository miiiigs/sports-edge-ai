from sports_ai.data_sources.api_football_history import (
    backfill_meets_gap_coverage_gate,
    estimate_api_football_backfill_requests,
    event_is_gap_ready,
    normalize_api_football_historical_event,
    summarize_api_football_history_coverage,
)
from sports_ai.data_sources.football_history import DIVISION_TO_COMPETITION


FIXTURE = {
    "fixture": {
        "id": 12345,
        "date": "2026-09-20T15:30:00+00:00",
        "referee": "A Ref",
        "status": {"short": "FT"},
    },
    "league": {"id": 61, "name": "Ligue 1", "season": 2026},
    "teams": {
        "home": {"id": 10, "name": "Home FC"},
        "away": {"id": 20, "name": "Away FC"},
    },
    "goals": {"home": 2, "away": 1},
    "score": {"halftime": {"home": 1, "away": 0}},
}


def _team_stats(team_id, team_name, *, shots, sot, corners):
    return {
        "team": {"id": team_id, "name": team_name},
        "statistics": [
            {"type": "Total Shots", "value": shots},
            {"type": "Shots on Goal", "value": sot},
            {"type": "Fouls", "value": 11},
            {"type": "Corner Kicks", "value": corners},
            {"type": "Yellow Cards", "value": 2},
            {"type": "Red Cards", "value": 0},
        ],
    }


def test_api_football_history_normalizes_completed_fixture_and_gap_fields():
    row = normalize_api_football_historical_event(
        FIXTURE,
        [
            _team_stats(10, "Home FC", shots=14, sot=6, corners=7),
            _team_stats(20, "Away FC", shots=9, sot=3, corners=2),
        ],
        competition_id="FRA1",
        competition_name="Ligue 1",
        division_code="F1",
        season_code="2627",
    )

    assert row is not None
    assert row.event_id == "evt_api_football_12345"
    assert row.competition_id == "FRA1"
    assert row.match_date == "2026-09-20"
    assert row.kickoff_time == "15:30"
    assert row.result_code == "H"
    assert row.half_time_result_code == "H"
    assert row.home_shots == 14
    assert row.away_shots_on_target == 3
    assert row.home_corners == 7
    assert row.away_corners == 2
    assert event_is_gap_ready(row) is True


def test_api_football_history_fails_closed_on_missing_gap_statistics():
    row = normalize_api_football_historical_event(
        FIXTURE,
        [
            _team_stats(10, "Home FC", shots=14, sot=6, corners=7),
            {
                "team": {"id": 20, "name": "Away FC"},
                "statistics": [
                    {"type": "Total Shots", "value": 9},
                    {"type": "Shots on Goal", "value": 3},
                ],
            },
        ],
        competition_id="FRA1",
        competition_name="Ligue 1",
        division_code="F1",
        season_code="2627",
    )

    assert row is not None
    assert row.away_corners is None
    assert event_is_gap_ready(row) is False

    coverage = summarize_api_football_history_coverage([FIXTURE], [row])
    assert coverage.completed_fixture_count == 1
    assert coverage.normalized_event_count == 1
    assert coverage.gap_ready_count == 0
    assert coverage.gap_ready_fraction == 0.0
    assert coverage.missing_required_fields["away_corners"] == 1


def test_completed_fixture_rejected_by_normalizer_reduces_coverage():
    invalid_completed = {
        **FIXTURE,
        "fixture": {**FIXTURE["fixture"], "id": 54321},
        "goals": {"home": None, "away": None},
    }
    good_row = normalize_api_football_historical_event(
        FIXTURE,
        [
            _team_stats(10, "Home FC", shots=14, sot=6, corners=7),
            _team_stats(20, "Away FC", shots=9, sot=3, corners=2),
        ],
        competition_id="FRA1",
        competition_name="Ligue 1",
        division_code="F1",
        season_code="2627",
    )
    assert good_row is not None

    coverage = summarize_api_football_history_coverage(
        [FIXTURE, invalid_completed],
        [good_row],
    )
    assert coverage.completed_fixture_count == 2
    assert coverage.normalized_event_count == 1
    assert coverage.gap_ready_count == 1
    assert coverage.gap_ready_fraction == 0.5
    assert coverage.rejected_fixture_ids == ("54321",)


def test_api_football_history_rejects_nonfinished_fixture():
    fixture = {
        **FIXTURE,
        "fixture": {**FIXTURE["fixture"], "id": 999, "status": {"short": "NS"}},
    }
    assert (
        normalize_api_football_historical_event(
            fixture,
            [],
            competition_id="FRA1",
            competition_name="Ligue 1",
            division_code="F1",
            season_code="2627",
        )
        is None
    )


def test_api_football_history_uses_team_ids_before_names():
    stats = [
        _team_stats(20, "Wrong Label", shots=9, sot=3, corners=2),
        _team_stats(10, "Another Label", shots=14, sot=6, corners=7),
    ]
    row = normalize_api_football_historical_event(
        FIXTURE,
        stats,
        competition_id="FRA1",
        competition_name="Ligue 1",
        division_code="F1",
        season_code="2627",
    )
    assert row is not None
    assert row.home_shots == 14
    assert row.away_shots == 9
    assert event_is_gap_ready(row) is True


def test_backfill_request_estimate_is_fixture_scoped_for_statistics():
    estimate = estimate_api_football_backfill_requests(
        league_id=61,
        season=2025,
        expected_fixture_count=306,
    )
    assert estimate.fixture_list_requests == 1
    assert estimate.fixture_statistics_requests == 306
    assert estimate.total_requests == 307


def test_gap_coverage_gate_requires_fraction_and_minimum_rows():
    ready_fixture = FIXTURE
    rows = []
    for index in range(100):
        fixture = {
            **ready_fixture,
            "fixture": {**ready_fixture["fixture"], "id": 20000 + index},
        }
        row = normalize_api_football_historical_event(
            fixture,
            [
                _team_stats(10, "Home FC", shots=14, sot=6, corners=7),
                _team_stats(20, "Away FC", shots=9, sot=3, corners=2),
            ],
            competition_id="FRA1",
            competition_name="Ligue 1",
            division_code="F1",
            season_code="2627",
        )
        assert row is not None
        rows.append(row)

    coverage = summarize_api_football_history_coverage(
        [
            {
                **ready_fixture,
                "fixture": {**ready_fixture["fixture"], "id": 20000 + index},
            }
            for index in range(100)
        ],
        rows,
    )
    assert backfill_meets_gap_coverage_gate(coverage) is True
    assert backfill_meets_gap_coverage_gate(
        coverage,
        min_gap_ready_fraction=1.01,
    ) is False


def test_missing_top_league_canonical_mappings_are_explicit():
    assert DIVISION_TO_COMPETITION["F1"] == ("FRA1", "Ligue 1")
    assert DIVISION_TO_COMPETITION["D1"] == ("GER1", "Bundesliga")
    assert DIVISION_TO_COMPETITION["I1"] == ("ITA1", "Serie A")
    assert DIVISION_TO_COMPETITION["SC0"] == ("SCO1", "Scottish Premiership")
