"""Generic GAP ratings core for football process statistics.

The August 15, 2026 football specification requires:

- independent rating systems for shots, shots on target, and corners
- distinct home and away state
- chronological point-in-time updates
- goals excluded as GAP inputs

This module implements the minimal generic state/update core needed to begin
that path with shots as the first supported statistic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

VENUE_HOME = "home"
VENUE_AWAY = "away"
VALID_VENUES = {VENUE_HOME, VENUE_AWAY}
SUPPORTED_GAP_STATISTICS = {"shots", "shots_on_target", "corners"}
FORBIDDEN_GAP_STATISTICS = {"goals", "goal", "home_goals", "away_goals", "fthg", "ftag"}


@dataclass(frozen=True)
class GapStateRecord:
    team_id: str
    competition_id: str
    statistic: str
    venue: str
    attack_rating: float
    defense_rating: float
    matches_observed: int
    as_of: str | None
    learning_rate: float
    model_version: str


@dataclass(frozen=True)
class GapObservation:
    event_id: str
    competition_id: str
    match_date: str
    home_team_id: str
    away_team_id: str
    statistic: str
    home_value: float
    away_value: float

    def __post_init__(self) -> None:
        statistic = self.statistic.strip().lower()
        if statistic in FORBIDDEN_GAP_STATISTICS:
            raise ValueError("goals may not be used as a GAP rating input")
        if statistic not in SUPPORTED_GAP_STATISTICS:
            raise ValueError(f"unsupported GAP statistic {self.statistic!r}")
        if self.home_value is None or self.away_value is None:
            raise ValueError("GAP observations require explicit non-null home and away values")


@dataclass(frozen=True)
class GapPrediction:
    competition_id: str
    statistic: str
    home_team_id: str
    away_team_id: str
    expected_home: float
    expected_away: float


@dataclass(frozen=True)
class GapUpdateResult:
    observation: GapObservation
    prediction: GapPrediction
    error_home: float
    error_away: float
    updated_records: tuple[GapStateRecord, ...]


@dataclass(frozen=True)
class GapConfigEntry:
    competition_id: str
    statistic: str
    learning_rate: float
    initial_home_baseline: float
    initial_away_baseline: float
    model_version: str

    def __post_init__(self) -> None:
        statistic = self.statistic.strip().lower()
        if statistic in FORBIDDEN_GAP_STATISTICS:
            raise ValueError("goals may not be used as a GAP rating input")
        if statistic not in SUPPORTED_GAP_STATISTICS:
            raise ValueError(f"unsupported GAP statistic {self.statistic!r}")
        if self.learning_rate <= 0:
            raise ValueError("learning_rate must be > 0")


class GapRatingsEngine:
    """Generic in-memory GAP state manager across leagues and statistics."""

    def __init__(self, config_entries: list[GapConfigEntry]):
        if not config_entries:
            raise ValueError("at least one GAP config entry is required")
        self._configs: dict[tuple[str, str], GapConfigEntry] = {}
        self._states: dict[tuple[str, str, str, str], GapStateRecord] = {}
        self._last_processed_date: dict[tuple[str, str], str] = {}
        self._processed_event_ids: set[tuple[str, str, str]] = set()
        for entry in config_entries:
            key = (entry.competition_id, entry.statistic)
            if key in self._configs:
                raise ValueError(f"duplicate GAP config for {key}")
            self._configs[key] = entry

    def _config(self, competition_id: str, statistic: str) -> GapConfigEntry:
        key = (competition_id, statistic)
        try:
            return self._configs[key]
        except KeyError as exc:
            raise KeyError(f"missing GAP config for competition/statistic {key}") from exc

    def _baseline(self, *, competition_id: str, statistic: str, venue: str) -> float:
        config = self._config(competition_id, statistic)
        if venue == VENUE_HOME:
            return config.initial_home_baseline
        if venue == VENUE_AWAY:
            return config.initial_away_baseline
        raise ValueError(f"invalid venue {venue!r}")

    def _state_key(self, *, competition_id: str, statistic: str, team_id: str, venue: str) -> tuple[str, str, str, str]:
        if venue not in VALID_VENUES:
            raise ValueError(f"invalid venue {venue!r}")
        return (competition_id, statistic, team_id, venue)

    def _get_or_create_state(
        self,
        *,
        competition_id: str,
        statistic: str,
        team_id: str,
        venue: str,
    ) -> GapStateRecord:
        key = self._state_key(
            competition_id=competition_id,
            statistic=statistic,
            team_id=team_id,
            venue=venue,
        )
        existing = self._states.get(key)
        if existing is not None:
            return existing
        config = self._config(competition_id, statistic)
        baseline = self._baseline(
            competition_id=competition_id,
            statistic=statistic,
            venue=venue,
        )
        record = GapStateRecord(
            team_id=team_id,
            competition_id=competition_id,
            statistic=statistic,
            venue=venue,
            attack_rating=baseline,
            defense_rating=baseline,
            matches_observed=0,
            as_of=None,
            learning_rate=config.learning_rate,
            model_version=config.model_version,
        )
        self._states[key] = record
        return record

    def predict_fixture(
        self,
        *,
        competition_id: str,
        statistic: str,
        home_team_id: str,
        away_team_id: str,
    ) -> GapPrediction:
        statistic_key = statistic.strip().lower()
        if statistic_key in FORBIDDEN_GAP_STATISTICS:
            raise ValueError("goals may not be used as a GAP rating input")
        if statistic_key not in SUPPORTED_GAP_STATISTICS:
            raise ValueError(f"unsupported GAP statistic {statistic!r}")

        home_state = self._get_or_create_state(
            competition_id=competition_id,
            statistic=statistic_key,
            team_id=home_team_id,
            venue=VENUE_HOME,
        )
        away_state = self._get_or_create_state(
            competition_id=competition_id,
            statistic=statistic_key,
            team_id=away_team_id,
            venue=VENUE_AWAY,
        )
        expected_home = (home_state.attack_rating + away_state.defense_rating) / 2.0
        expected_away = (away_state.attack_rating + home_state.defense_rating) / 2.0
        return GapPrediction(
            competition_id=competition_id,
            statistic=statistic_key,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            expected_home=expected_home,
            expected_away=expected_away,
        )

    def apply_observation(self, observation: GapObservation) -> GapUpdateResult:
        statistic_key = observation.statistic.strip().lower()
        config = self._config(observation.competition_id, statistic_key)
        event_key = (observation.competition_id, statistic_key, observation.event_id)
        if event_key in self._processed_event_ids:
            raise ValueError("duplicate GAP observation event_id for competition/statistic")
        last_date = self._last_processed_date.get((observation.competition_id, statistic_key))
        if last_date is not None and observation.match_date < last_date:
            raise ValueError("GAP observations must be applied in chronological order per competition/statistic")

        prediction = self.predict_fixture(
            competition_id=observation.competition_id,
            statistic=statistic_key,
            home_team_id=observation.home_team_id,
            away_team_id=observation.away_team_id,
        )
        error_home = observation.home_value - prediction.expected_home
        error_away = observation.away_value - prediction.expected_away
        learning_rate = config.learning_rate

        home_state = self._get_or_create_state(
            competition_id=observation.competition_id,
            statistic=statistic_key,
            team_id=observation.home_team_id,
            venue=VENUE_HOME,
        )
        away_state = self._get_or_create_state(
            competition_id=observation.competition_id,
            statistic=statistic_key,
            team_id=observation.away_team_id,
            venue=VENUE_AWAY,
        )

        updated_home = GapStateRecord(
            team_id=home_state.team_id,
            competition_id=home_state.competition_id,
            statistic=home_state.statistic,
            venue=home_state.venue,
            attack_rating=home_state.attack_rating + learning_rate * error_home,
            defense_rating=home_state.defense_rating + learning_rate * error_away,
            matches_observed=home_state.matches_observed + 1,
            as_of=observation.match_date,
            learning_rate=home_state.learning_rate,
            model_version=home_state.model_version,
        )
        updated_away = GapStateRecord(
            team_id=away_state.team_id,
            competition_id=away_state.competition_id,
            statistic=away_state.statistic,
            venue=away_state.venue,
            attack_rating=away_state.attack_rating + learning_rate * error_away,
            defense_rating=away_state.defense_rating + learning_rate * error_home,
            matches_observed=away_state.matches_observed + 1,
            as_of=observation.match_date,
            learning_rate=away_state.learning_rate,
            model_version=away_state.model_version,
        )

        self._states[self._state_key(
            competition_id=updated_home.competition_id,
            statistic=updated_home.statistic,
            team_id=updated_home.team_id,
            venue=updated_home.venue,
        )] = updated_home
        self._states[self._state_key(
            competition_id=updated_away.competition_id,
            statistic=updated_away.statistic,
            team_id=updated_away.team_id,
            venue=updated_away.venue,
        )] = updated_away
        self._last_processed_date[(observation.competition_id, statistic_key)] = observation.match_date
        self._processed_event_ids.add(event_key)

        return GapUpdateResult(
            observation=observation,
            prediction=prediction,
            error_home=error_home,
            error_away=error_away,
            updated_records=(updated_home, updated_away),
        )

    def state_record(
        self,
        *,
        competition_id: str,
        statistic: str,
        team_id: str,
        venue: str,
    ) -> GapStateRecord:
        statistic_key = statistic.strip().lower()
        return self._get_or_create_state(
            competition_id=competition_id,
            statistic=statistic_key,
            team_id=team_id,
            venue=venue,
        )

    def export_state_records(self) -> list[dict[str, Any]]:
        records = sorted(
            self._states.values(),
            key=lambda item: (item.competition_id, item.statistic, item.team_id, item.venue),
        )
        return [asdict(record) for record in records]
