from sports_ai.experiments.football_api_history_probe import (
    HistoricalProbeTarget,
    resolve_historical_league_candidate,
    run_historical_coverage_probe,
    season_code_from_start_year,
    target_for_competition,
)


TARGET = HistoricalProbeTarget(
    competition_id="FRA1",
    competition_name="Ligue 1",
    division_code="F1",
    search="Ligue 1",
    country="France",
    competition_type="League",
    accepted_provider_names=("Ligue 1",),
)


def _candidate(*, league_id=61, stats=True, name="Ligue 1", country="France", league_type="League"):
    return {
        "league": {"id": league_id, "name": name, "type": league_type},
        "country": {"name": country},
        "seasons": [
            {
                "year": 2025,
                "coverage": {"fixtures": {"statistics_fixtures": stats}},
            }
        ],
    }


def _fixture(fixture_id, date):
    return {
        "fixture": {
            "id": fixture_id,
            "date": date,
            "referee": "Ref",
            "status": {"short": "FT"},
        },
        "teams": {
            "home": {"id": fixture_id * 10, "name": f"Home {fixture_id}"},
            "away": {"id": fixture_id * 10 + 1, "name": f"Away {fixture_id}"},
        },
        "goals": {"home": 2, "away": 1},
        "score": {"halftime": {"home": 1, "away": 0}},
    }


def _statistics(fixture):
    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]
    return [
        {
            "team": dict(home),
            "statistics": [
                {"type": "Total Shots", "value": 13},
                {"type": "Shots on Goal", "value": 5},
                {"type": "Corner Kicks", "value": 6},
            ],
        },
        {
            "team": dict(away),
            "statistics": [
                {"type": "Total Shots", "value": 9},
                {"type": "Shots on Goal", "value": 3},
                {"type": "Corner Kicks", "value": 4},
            ],
        },
    ]


class _FakeProvider:
    def __init__(self, *, candidates, fixtures, stats_by_fixture=None):
        self.candidates = list(candidates)
        self.fixtures = list(fixtures)
        self.stats_by_fixture = dict(stats_by_fixture or {})
        self.calls = []

    def get_historical_league_candidates(self, *, search, season):
        self.calls.append(("league", search, season))
        return list(self.candidates)

    def get_historical_fixtures(self, *, league_id, season):
        self.calls.append(("fixtures", league_id, season))
        return list(self.fixtures)

    def get_historical_fixture_statistics(self, *, provider_fixture_id):
        self.calls.append(("statistics", provider_fixture_id))
        return list(self.stats_by_fixture.get(str(provider_fixture_id), []))


def test_target_registry_and_season_code_are_explicit():
    assert target_for_competition("FRA1") == TARGET
    assert season_code_from_start_year(2021) == "2122"
    assert season_code_from_start_year(2025) == "2526"


def test_candidate_resolution_requires_exact_name_country_type_and_season():
    candidate, error = resolve_historical_league_candidate(
        [
            _candidate(country="France"),
            _candidate(league_id=999, country="Canada"),
        ],
        target=TARGET,
        season=2025,
    )
    assert error is None
    assert candidate["league"]["id"] == 61

    candidate, error = resolve_historical_league_candidate(
        [_candidate(), _candidate(league_id=62)],
        target=TARGET,
        season=2025,
    )
    assert candidate is None
    assert error == "LEAGUE_REFERENCE_AMBIGUOUS"


def test_probe_blocks_before_fixture_call_when_statistics_coverage_is_unavailable():
    provider = _FakeProvider(candidates=[_candidate(stats=False)], fixtures=[])

    result = run_historical_coverage_probe(
        provider,
        target=TARGET,
        season=2025,
    )

    assert result.state == "BLOCKED"
    assert result.reason == "FIXTURE_STATISTICS_COVERAGE_UNAVAILABLE"
    assert result.statistics_fixture_coverage is False
    assert result.bounded_probe_requests == 1
    assert provider.calls == [("league", "Ligue 1", 2025)]


def test_probe_samples_first_middle_last_and_projects_full_backfill_cost():
    fixtures = [
        _fixture(100 + index, f"202{index + 1}-01-01T12:00:00+00:00")
        for index in range(5)
    ]
    stats = {
        str(row["fixture"]["id"]): _statistics(row)
        for row in fixtures
    }
    provider = _FakeProvider(
        candidates=[_candidate()],
        fixtures=fixtures,
        stats_by_fixture=stats,
    )

    result = run_historical_coverage_probe(
        provider,
        target=TARGET,
        season=2025,
        sample_size=3,
    )

    assert result.state == "READY_TO_BACKFILL"
    assert result.provider_league_id == 61
    assert result.total_fixture_count == 5
    assert result.completed_fixture_count == 5
    assert result.sampled_fixture_count == 3
    assert result.sampled_gap_ready_count == 3
    assert result.bounded_probe_requests == 5
    # reference + fixture-list + one statistics request for every completed fixture
    assert result.projected_backfill_requests == 7
    stat_calls = [call for call in provider.calls if call[0] == "statistics"]
    assert stat_calls == [
        ("statistics", "100"),
        ("statistics", "102"),
        ("statistics", "104"),
    ]


def test_probe_fails_closed_when_any_sample_lacks_gap_inputs():
    fixtures = [
        _fixture(201, "2025-08-01T12:00:00+00:00"),
        _fixture(202, "2025-12-01T12:00:00+00:00"),
        _fixture(203, "2026-05-01T12:00:00+00:00"),
    ]
    stats = {
        "201": _statistics(fixtures[0]),
        "202": _statistics(fixtures[1]),
        "203": [
            {
                "team": dict(fixtures[2]["teams"]["home"]),
                "statistics": [
                    {"type": "Total Shots", "value": 10},
                    {"type": "Shots on Goal", "value": 4},
                ],
            }
        ],
    }
    provider = _FakeProvider(
        candidates=[_candidate()],
        fixtures=fixtures,
        stats_by_fixture=stats,
    )

    result = run_historical_coverage_probe(
        provider,
        target=TARGET,
        season=2025,
        sample_size=3,
    )

    assert result.state == "SAMPLE_INCOMPLETE"
    assert result.sampled_gap_ready_count == 2
    assert result.reason == "SAMPLED_GAP_INPUTS_INCOMPLETE"
    assert result.sample_failures[0]["provider_fixture_id"] == "203"
