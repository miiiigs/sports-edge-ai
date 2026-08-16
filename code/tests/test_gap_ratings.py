import pytest

from sports_ai.agents.football.gap_ratings import (
    VENUE_AWAY,
    VENUE_HOME,
    GapConfigEntry,
    GapObservation,
    GapRatingsEngine,
)


def _engine() -> GapRatingsEngine:
    return GapRatingsEngine(
        [
            GapConfigEntry(
                competition_id="ENG1",
                statistic="shots",
                learning_rate=0.1,
                initial_home_baseline=10.0,
                initial_away_baseline=8.0,
                model_version="gap_shots_v1",
            ),
            GapConfigEntry(
                competition_id="ENG1",
                statistic="shots_on_target",
                learning_rate=0.05,
                initial_home_baseline=4.0,
                initial_away_baseline=3.0,
                model_version="gap_sot_v1",
            ),
            GapConfigEntry(
                competition_id="ENG2",
                statistic="shots",
                learning_rate=0.2,
                initial_home_baseline=11.0,
                initial_away_baseline=9.0,
                model_version="gap_shots_v1",
            ),
        ]
    )


def test_gap_hand_computed_update():
    engine = _engine()
    observation = GapObservation(
        event_id="evt_1",
        competition_id="ENG1",
        match_date="2026-08-01",
        home_team_id="Alpha",
        away_team_id="Beta",
        statistic="shots",
        home_value=12,
        away_value=6,
    )

    result = engine.apply_observation(observation)

    assert result.prediction.expected_home == pytest.approx(9.0)
    assert result.prediction.expected_away == pytest.approx(9.0)
    assert result.error_home == pytest.approx(3.0)
    assert result.error_away == pytest.approx(-3.0)

    home_state = engine.state_record(
        competition_id="ENG1",
        statistic="shots",
        team_id="Alpha",
        venue=VENUE_HOME,
    )
    away_state = engine.state_record(
        competition_id="ENG1",
        statistic="shots",
        team_id="Beta",
        venue=VENUE_AWAY,
    )
    assert home_state.attack_rating == pytest.approx(10.3)
    assert home_state.defense_rating == pytest.approx(9.7)
    assert away_state.attack_rating == pytest.approx(7.7)
    assert away_state.defense_rating == pytest.approx(8.3)
    assert home_state.matches_observed == 1
    assert away_state.matches_observed == 1


def test_gap_chronological_isolation_for_predictions():
    engine = _engine()
    first = GapObservation(
        event_id="evt_1",
        competition_id="ENG1",
        match_date="2026-08-01",
        home_team_id="Alpha",
        away_team_id="Beta",
        statistic="shots",
        home_value=12,
        away_value=6,
    )
    future = GapObservation(
        event_id="evt_2",
        competition_id="ENG1",
        match_date="2026-08-02",
        home_team_id="Alpha",
        away_team_id="Gamma",
        statistic="shots",
        home_value=9,
        away_value=8,
    )
    engine.apply_observation(first)
    prediction_before_future = engine.predict_fixture(
        competition_id="ENG1",
        statistic="shots",
        home_team_id="Alpha",
        away_team_id="Beta",
    )

    comparison_engine = _engine()
    comparison_engine.apply_observation(first)
    comparison_prediction = comparison_engine.predict_fixture(
        competition_id="ENG1",
        statistic="shots",
        home_team_id="Alpha",
        away_team_id="Beta",
    )

    engine.apply_observation(future)

    assert prediction_before_future.expected_home == pytest.approx(comparison_prediction.expected_home)
    assert prediction_before_future.expected_away == pytest.approx(comparison_prediction.expected_away)


def test_gap_independent_statistic_isolation():
    engine = _engine()
    shots_observation = GapObservation(
        event_id="evt_1",
        competition_id="ENG1",
        match_date="2026-08-01",
        home_team_id="Alpha",
        away_team_id="Beta",
        statistic="shots",
        home_value=12,
        away_value=6,
    )
    engine.apply_observation(shots_observation)

    shots_on_target_state = engine.state_record(
        competition_id="ENG1",
        statistic="shots_on_target",
        team_id="Alpha",
        venue=VENUE_HOME,
    )
    assert shots_on_target_state.attack_rating == pytest.approx(4.0)
    assert shots_on_target_state.defense_rating == pytest.approx(4.0)
    assert shots_on_target_state.matches_observed == 0


def test_gap_league_isolation():
    engine = _engine()
    engine.apply_observation(
        GapObservation(
            event_id="evt_1",
            competition_id="ENG1",
            match_date="2026-08-01",
            home_team_id="Alpha",
            away_team_id="Beta",
            statistic="shots",
            home_value=12,
            away_value=6,
        )
    )

    eng2_state = engine.state_record(
        competition_id="ENG2",
        statistic="shots",
        team_id="Alpha",
        venue=VENUE_HOME,
    )
    assert eng2_state.attack_rating == pytest.approx(11.0)
    assert eng2_state.defense_rating == pytest.approx(11.0)
    assert eng2_state.matches_observed == 0


def test_gap_home_away_isolation():
    engine = _engine()
    engine.apply_observation(
        GapObservation(
            event_id="evt_1",
            competition_id="ENG1",
            match_date="2026-08-01",
            home_team_id="Alpha",
            away_team_id="Beta",
            statistic="shots",
            home_value=12,
            away_value=6,
        )
    )

    alpha_home = engine.state_record(
        competition_id="ENG1",
        statistic="shots",
        team_id="Alpha",
        venue=VENUE_HOME,
    )
    alpha_away = engine.state_record(
        competition_id="ENG1",
        statistic="shots",
        team_id="Alpha",
        venue=VENUE_AWAY,
    )
    beta_away = engine.state_record(
        competition_id="ENG1",
        statistic="shots",
        team_id="Beta",
        venue=VENUE_AWAY,
    )
    beta_home = engine.state_record(
        competition_id="ENG1",
        statistic="shots",
        team_id="Beta",
        venue=VENUE_HOME,
    )

    assert alpha_home.matches_observed == 1
    assert alpha_away.matches_observed == 0
    assert beta_away.matches_observed == 1
    assert beta_home.matches_observed == 0
    assert alpha_away.attack_rating == pytest.approx(8.0)
    assert beta_home.attack_rating == pytest.approx(10.0)


def test_gap_missing_input_failure():
    with pytest.raises(ValueError, match="non-null"):
        GapObservation(
            event_id="evt_1",
            competition_id="ENG1",
            match_date="2026-08-01",
            home_team_id="Alpha",
            away_team_id="Beta",
            statistic="shots",
            home_value=None,
            away_value=6,
        )


def test_gap_goals_prohibition():
    with pytest.raises(ValueError, match="goals may not be used"):
        GapObservation(
            event_id="evt_1",
            competition_id="ENG1",
            match_date="2026-08-01",
            home_team_id="Alpha",
            away_team_id="Beta",
            statistic="goals",
            home_value=2,
            away_value=1,
        )


def test_gap_rejects_out_of_order_observations_per_competition_and_statistic():
    engine = _engine()
    engine.apply_observation(
        GapObservation(
            event_id="evt_1",
            competition_id="ENG1",
            match_date="2026-08-02",
            home_team_id="Alpha",
            away_team_id="Beta",
            statistic="shots",
            home_value=12,
            away_value=6,
        )
    )

    with pytest.raises(ValueError, match="chronological order"):
        engine.apply_observation(
            GapObservation(
                event_id="evt_2",
                competition_id="ENG1",
                match_date="2026-08-01",
                home_team_id="Alpha",
                away_team_id="Gamma",
                statistic="shots",
                home_value=9,
                away_value=8,
            )
        )


def test_gap_rejects_duplicate_event_ids_per_competition_and_statistic():
    engine = _engine()
    observation = GapObservation(
        event_id="evt_1",
        competition_id="ENG1",
        match_date="2026-08-01",
        home_team_id="Alpha",
        away_team_id="Beta",
        statistic="shots",
        home_value=12,
        away_value=6,
    )

    engine.apply_observation(observation)

    with pytest.raises(ValueError, match="duplicate GAP observation"):
        engine.apply_observation(observation)
