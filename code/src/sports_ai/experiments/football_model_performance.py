"""Historical football performance benchmark for the accepted model stack."""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Iterator, Sequence
from ..storage.football_paper_trading import settlement_state_for_market

EXPERIMENT_VERSION = "football_model_performance_benchmark_v1"
SUMMARY_FILENAME = "football_performance_summary.json"
REPORT_FILENAME = "FOOTBALL_MODEL_PERFORMANCE_REPORT.md"
MARKET_CSV_FILENAME = "football_performance_by_market.csv"
COMPETITION_CSV_FILENAME = "football_performance_by_competition.csv"
SEASON_CSV_FILENAME = "football_performance_by_season.csv"
CALIBRATION_CSV_FILENAME = "football_calibration.csv"
CONFUSION_CSV_FILENAME = "football_1x2_confusion_matrix.csv"
IMPROVEMENT_BACKLOG_FILENAME = "FOOTBALL_MODEL_IMPROVEMENT_BACKLOG.md"
DATE_STAMP = "2026-08-20"
CLV_STATUS_UNAVAILABLE = "UNAVAILABLE_HISTORICALLY"
PROFITABILITY_STATUS_UNPROVEN = "NO"
_CALIBRATION_BINS = [(index / 10.0, (index + 1) / 10.0) for index in range(10)]
_HALF_LINE_MARKETS = {
    ("goals_total", 1.5),
    ("goals_total", 2.5),
    ("goals_total", 3.5),
    ("corner_total", 8.5),
    ("corner_total", 9.5),
    ("corner_total", 10.5),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class PerformanceBenchmarkConfig:
    day8_predictions_path: Path
    day10_market_prices_path: Path
    day11_decisions_path: Path
    research_db_path: Path
    output_dir: Path

    @classmethod
    def default(cls) -> "PerformanceBenchmarkConfig":
        repo_root = _repo_root()
        research_dir = repo_root / "research" / "football_research"
        return cls(
            day8_predictions_path=research_dir / "football_day8_goal_conversion_2026-08-17_predictions.jsonl",
            day10_market_prices_path=research_dir / "football_day10_market_pricing_2026-08-18_market_prices.jsonl",
            day11_decisions_path=repo_root / "research" / "product_outputs" / "football" / "day11" / "football_day11_decisions.jsonl",
            research_db_path=research_dir / "store" / "football_research.sqlite",
            output_dir=repo_root / "research" / "product_outputs" / "football" / "performance",
        )


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def _kickoff_at(match_date: str, kickoff_time: str | None) -> str:
    return f"{match_date}T{kickoff_time or '00:00'}:00"


def _parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_binary_metric_line(market: str, line: float | None) -> bool:
    if market in {"1x2", "btts"}:
        return True
    if line is None:
        return False
    return (market, float(line)) in _HALF_LINE_MARKETS


def _selection_from_score(actual_home_goals: int, actual_away_goals: int) -> str:
    if actual_home_goals > actual_away_goals:
        return "HOME"
    if actual_home_goals < actual_away_goals:
        return "AWAY"
    return "DRAW"


def _bucket_index(probability: float) -> int:
    return min(9, max(0, int(probability * 10)))


def _bucket_label(index: int) -> str:
    lo = int(_CALIBRATION_BINS[index][0] * 100)
    hi = int(_CALIBRATION_BINS[index][1] * 100)
    return f"{lo}-{hi}%"


@dataclass
class BinaryMetricAccumulator:
    n: int = 0
    hit_sum: int = 0
    probability_sum: float = 0.0
    brier_sum: float = 0.0
    log_loss_sum: float = 0.0
    buckets: list[dict[str, float]] = field(
        default_factory=lambda: [{"n": 0, "p_sum": 0.0, "y_sum": 0.0} for _ in range(10)]
    )

    def add(self, probability: float, outcome: int) -> None:
        bounded_probability = min(1.0 - 1e-12, max(1e-12, float(probability)))
        outcome_value = int(outcome)
        self.n += 1
        self.hit_sum += outcome_value
        self.probability_sum += bounded_probability
        self.brier_sum += (bounded_probability - outcome_value) ** 2
        self.log_loss_sum += -(outcome_value * math.log(bounded_probability) + (1 - outcome_value) * math.log(1 - bounded_probability))
        bucket = self.buckets[_bucket_index(bounded_probability)]
        bucket["n"] += 1
        bucket["p_sum"] += bounded_probability
        bucket["y_sum"] += outcome_value

    def summary(self) -> dict[str, Any]:
        return {
            "sample_size": self.n,
            "hit_rate": _safe_ratio(self.hit_sum, self.n),
            "mean_predicted_probability": _safe_ratio(self.probability_sum, self.n),
            "actual_frequency": _safe_ratio(self.hit_sum, self.n),
            "brier": _safe_ratio(self.brier_sum, self.n),
            "log_loss": _safe_ratio(self.log_loss_sum, self.n),
            "expected_calibration_error": self.expected_calibration_error(),
        }

    def expected_calibration_error(self) -> float | None:
        if self.n == 0:
            return None
        total = 0.0
        for bucket in self.buckets:
            if bucket["n"] == 0:
                continue
            mean_pred = bucket["p_sum"] / bucket["n"]
            observed = bucket["y_sum"] / bucket["n"]
            total += (bucket["n"] / self.n) * abs(mean_pred - observed)
        return total

    def calibration_rows(
        self,
        *,
        scope_type: str,
        scope_value: str,
        market: str,
        line: float | None,
        selection: str,
        subset: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, bucket in enumerate(self.buckets):
            n = int(bucket["n"])
            mean_pred = None if n == 0 else bucket["p_sum"] / n
            observed = None if n == 0 else bucket["y_sum"] / n
            rows.append(
                {
                    "scope_type": scope_type,
                    "scope_value": scope_value,
                    "market": market,
                    "line": line,
                    "selection": selection,
                    "subset": subset,
                    "bucket_label": _bucket_label(index),
                    "bucket_lo": _CALIBRATION_BINS[index][0],
                    "bucket_hi": _CALIBRATION_BINS[index][1],
                    "predicted_mean_probability": _round(mean_pred),
                    "observed_hit_rate": _round(observed),
                    "sample_size": n,
                    "calibration_gap": _round(None if mean_pred is None or observed is None else mean_pred - observed),
                }
            )
        return rows


@dataclass
class MulticlassAccumulator:
    n: int = 0
    top_correct: int = 0
    multiclass_log_loss_sum: float = 0.0
    multiclass_brier_sum: float = 0.0
    predicted_probability_sums: dict[str, float] = field(default_factory=lambda: {"HOME": 0.0, "DRAW": 0.0, "AWAY": 0.0})
    actual_counts: dict[str, int] = field(default_factory=lambda: {"HOME": 0, "DRAW": 0, "AWAY": 0})
    confusion: Counter[tuple[str, str]] = field(default_factory=Counter)
    by_selection: dict[str, BinaryMetricAccumulator] = field(
        default_factory=lambda: {
            "HOME": BinaryMetricAccumulator(),
            "DRAW": BinaryMetricAccumulator(),
            "AWAY": BinaryMetricAccumulator(),
        }
    )

    def add(self, probabilities: dict[str, float], actual_selection: str) -> None:
        self.n += 1
        predicted_selection = max(probabilities.items(), key=lambda item: (item[1], item[0]))[0]
        self.top_correct += int(predicted_selection == actual_selection)
        self.actual_counts[actual_selection] += 1
        self.confusion[(actual_selection, predicted_selection)] += 1
        self.multiclass_log_loss_sum += -math.log(min(1.0 - 1e-12, max(1e-12, probabilities[actual_selection])))
        self.multiclass_brier_sum += sum(
            (probabilities[selection] - (1.0 if selection == actual_selection else 0.0)) ** 2
            for selection in ("HOME", "DRAW", "AWAY")
        )
        for selection, probability in probabilities.items():
            self.predicted_probability_sums[selection] += probability
            self.by_selection[selection].add(probability, int(selection == actual_selection))

    def summary(self) -> dict[str, Any]:
        return {
            "sample_size": self.n,
            "top_probability_accuracy": _safe_ratio(self.top_correct, self.n),
            "multiclass_log_loss": _safe_ratio(self.multiclass_log_loss_sum, self.n),
            "multiclass_brier": _safe_ratio(self.multiclass_brier_sum, self.n),
            "mean_predicted_home_probability": _safe_ratio(self.predicted_probability_sums["HOME"], self.n),
            "actual_home_frequency": _safe_ratio(self.actual_counts["HOME"], self.n),
            "mean_predicted_draw_probability": _safe_ratio(self.predicted_probability_sums["DRAW"], self.n),
            "actual_draw_frequency": _safe_ratio(self.actual_counts["DRAW"], self.n),
            "mean_predicted_away_probability": _safe_ratio(self.predicted_probability_sums["AWAY"], self.n),
            "actual_away_frequency": _safe_ratio(self.actual_counts["AWAY"], self.n),
            "expected_calibration_error": self.by_selection["HOME"].expected_calibration_error(),
        }


@dataclass
class SettlementAccumulator:
    positions: int = 0
    full_wins: int = 0
    half_wins: int = 0
    pushes: int = 0
    half_losses: int = 0
    full_losses: int = 0

    def add(self, state: str) -> None:
        self.positions += 1
        if state == "WON":
            self.full_wins += 1
        elif state == "HALF_WIN":
            self.half_wins += 1
        elif state == "PUSH":
            self.pushes += 1
        elif state == "HALF_LOSS":
            self.half_losses += 1
        elif state == "LOST":
            self.full_losses += 1
        else:
            raise ValueError(f"unsupported settlement state {state!r}")

    def summary(self) -> dict[str, Any]:
        weighted = (
            self.full_wins
            + (0.5 * self.half_wins)
            + (0.5 * self.pushes)
            + (0.0 * self.half_losses)
            + (0.0 * self.full_losses)
        )
        return {
            "positions": self.positions,
            "weighted_win_equivalent_result": _safe_ratio(weighted, self.positions),
            "full_wins": self.full_wins,
            "half_wins": self.half_wins,
            "pushes": self.pushes,
            "half_losses": self.half_losses,
            "full_losses": self.full_losses,
        }


@dataclass
class ErrorAccumulator:
    n: int = 0
    abs_sum: float = 0.0
    squared_sum: float = 0.0

    def add(self, predicted: float, actual: float) -> None:
        error = float(predicted) - float(actual)
        self.n += 1
        self.abs_sum += abs(error)
        self.squared_sum += error * error

    def summary(self) -> dict[str, float | None]:
        return {
            "sample_size": self.n,
            "mae": _safe_ratio(self.abs_sum, self.n),
            "rmse": None if self.n == 0 else math.sqrt(self.squared_sum / self.n),
        }


def _load_actual_match_stats(path: Path) -> dict[str, dict[str, int | None]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            event_id,
            home_corners,
            away_corners,
            home_shots,
            away_shots,
            home_shots_on_target,
            away_shots_on_target
        FROM football_events
        """
    ).fetchall()
    conn.close()
    return {
        str(row["event_id"]): {
            "actual_home_corners": None if row["home_corners"] is None else int(row["home_corners"]),
            "actual_away_corners": None if row["away_corners"] is None else int(row["away_corners"]),
            "actual_home_shots": None if row["home_shots"] is None else int(row["home_shots"]),
            "actual_away_shots": None if row["away_shots"] is None else int(row["away_shots"]),
            "actual_home_shots_on_target": None if row["home_shots_on_target"] is None else int(row["home_shots_on_target"]),
            "actual_away_shots_on_target": None if row["away_shots_on_target"] is None else int(row["away_shots_on_target"]),
        }
        for row in rows
    }


def _load_fixture_facts(
    path: Path,
    *,
    research_db_path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    fixture_facts: dict[str, dict[str, Any]] = {}
    actual_match_stats = _load_actual_match_stats(research_db_path)
    chronology_rows = 0
    as_of_after_kickoff = 0
    as_of_equal_kickoff = 0
    for row in _iter_jsonl(path):
        kickoff_at = _kickoff_at(str(row["match_date"]), row.get("kickoff_time"))
        chronology_rows += 1
        if str(row["as_of"]) > kickoff_at:
            as_of_after_kickoff += 1
        if str(row["as_of"]) == kickoff_at:
            as_of_equal_kickoff += 1
        match_stats = actual_match_stats.get(str(row["event_id"]), {})
        actual_home_corners = match_stats.get("actual_home_corners")
        actual_away_corners = match_stats.get("actual_away_corners")
        actual_home_shots = match_stats.get("actual_home_shots")
        actual_away_shots = match_stats.get("actual_away_shots")
        actual_home_shots_on_target = match_stats.get("actual_home_shots_on_target")
        actual_away_shots_on_target = match_stats.get("actual_away_shots_on_target")
        fixture_facts[str(row["event_id"])] = {
            "event_id": str(row["event_id"]),
            "competition_id": str(row["competition_id"]),
            "competition_name": str(row["competition_name"]),
            "season_code": str(row["season_code"]),
            "division_code": str(row["division_code"]),
            "match_date": str(row["match_date"]),
            "kickoff_at": kickoff_at,
            "as_of": str(row["as_of"]),
            "actual_home_goals": int(row["actual_home_goals"]),
            "actual_away_goals": int(row["actual_away_goals"]),
            "actual_total_goals": int(row["actual_total_goals"]),
            "actual_total_corners": int(row["actual_total_corners"]),
            "expected_goals_home": float(row["expected_goals_home"]),
            "expected_goals_away": float(row["expected_goals_away"]),
            "expected_total_goals": float(row["expected_total_goals"]),
            "predicted_home_corners": float(row["predicted_home_corners"]),
            "predicted_away_corners": float(row["predicted_away_corners"]),
            "predicted_total_corners": float(row["predicted_home_corners"]) + float(row["predicted_away_corners"]),
            "predicted_home_shots": float(row["predicted_home_shots"]),
            "predicted_away_shots": float(row["predicted_away_shots"]),
            "predicted_total_shots": float(row["predicted_home_shots"]) + float(row["predicted_away_shots"]),
            "predicted_home_shots_on_target": float(row["predicted_home_shots_on_target"]),
            "predicted_away_shots_on_target": float(row["predicted_away_shots_on_target"]),
            "predicted_total_shots_on_target": float(row["predicted_home_shots_on_target"])
            + float(row["predicted_away_shots_on_target"]),
            "actual_home_corners": actual_home_corners,
            "actual_away_corners": actual_away_corners,
            "actual_total_corners": int(row["actual_total_corners"]),
            "actual_home_shots": actual_home_shots,
            "actual_away_shots": actual_away_shots,
            "actual_total_shots": None
            if actual_home_shots is None or actual_away_shots is None
            else actual_home_shots + actual_away_shots,
            "actual_home_shots_on_target": actual_home_shots_on_target,
            "actual_away_shots_on_target": actual_away_shots_on_target,
            "actual_total_shots_on_target": None
            if actual_home_shots_on_target is None or actual_away_shots_on_target is None
            else actual_home_shots_on_target + actual_away_shots_on_target,
        }
    chronology_summary = {
        "rows_checked": chronology_rows,
        "as_of_after_kickoff": as_of_after_kickoff,
        "as_of_equal_kickoff": as_of_equal_kickoff,
        "chronology_status": "PASS" if as_of_after_kickoff == 0 else "FAIL",
        "source_time_status": "UNAVAILABLE_IN_FROZEN_ARCHIVE",
    }
    return fixture_facts, chronology_summary


def _candidate_summary(path: Path) -> dict[str, Any]:
    competition_counts: Counter[str] = Counter()
    selection_counts: Counter[str] = Counter()
    candidate_prices: list[float] = []
    for row in _iter_jsonl(path):
        if str(row.get("decision")) != "PRELIMINARY_CANDIDATE":
            continue
        competition_counts[str(row["competition_id"])] += 1
        selection_counts[str(row["selection"])] += 1
        candidate_prices.append(float(row["best_available_price"]))
    average_closing_price = mean(candidate_prices) if candidate_prices else None
    return {
        "candidate_count": sum(competition_counts.values()),
        "candidate_count_by_competition": dict(competition_counts),
        "candidate_count_by_selection": dict(selection_counts),
        "average_closing_snapshot_price": average_closing_price,
        "closing_snapshot_break_even_rate": None if average_closing_price is None else (1.0 / average_closing_price),
        "historical_price_context": "CLOSING_SNAPSHOT_ONLY",
        "roi_status": "UNAVAILABLE_ENTRY_TIMING",
    }


def _stream_day10_metrics(
    *,
    path: Path,
    fixture_facts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    one_x_two_overall = MulticlassAccumulator()
    one_x_two_by_competition: dict[str, MulticlassAccumulator] = defaultdict(MulticlassAccumulator)
    one_x_two_by_season: dict[str, MulticlassAccumulator] = defaultdict(MulticlassAccumulator)
    btts_overall = BinaryMetricAccumulator()
    btts_by_competition: dict[str, BinaryMetricAccumulator] = defaultdict(BinaryMetricAccumulator)
    btts_by_season: dict[str, BinaryMetricAccumulator] = defaultdict(BinaryMetricAccumulator)
    market_binary_accumulators: dict[tuple[str, float | None, str], BinaryMetricAccumulator] = defaultdict(BinaryMetricAccumulator)
    market_binary_by_competition: dict[tuple[str, float | None, str, str], BinaryMetricAccumulator] = defaultdict(BinaryMetricAccumulator)
    market_binary_by_season: dict[tuple[str, float | None, str, str], BinaryMetricAccumulator] = defaultdict(BinaryMetricAccumulator)
    ah_accumulators: dict[tuple[float, str], SettlementAccumulator] = defaultdict(SettlementAccumulator)
    grouped_rows: list[dict[str, Any]] = []
    grouped_key: tuple[str, str, float | None] | None = None

    def finalize_group(rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        first = rows[0]
        facts = fixture_facts[str(first["event_id"])]
        competition_id = str(facts["competition_id"])
        season_code = str(facts["season_code"])
        market = str(first["market_family"])
        line = None if first["line"] is None else float(first["line"])
        if market == "1x2":
            probabilities = {str(row["selection"]): float(row["effective_win_stake_probability"]) for row in rows}
            actual_selection = _selection_from_score(facts["actual_home_goals"], facts["actual_away_goals"])
            one_x_two_overall.add(probabilities, actual_selection)
            one_x_two_by_competition[competition_id].add(probabilities, actual_selection)
            one_x_two_by_season[season_code].add(probabilities, actual_selection)
            return
        if market == "btts":
            yes_probability = next(float(row["effective_win_stake_probability"]) for row in rows if str(row["selection"]) == "YES")
            outcome_yes = int(facts["actual_home_goals"] >= 1 and facts["actual_away_goals"] >= 1)
            btts_overall.add(yes_probability, outcome_yes)
            btts_by_competition[competition_id].add(yes_probability, outcome_yes)
            btts_by_season[season_code].add(yes_probability, outcome_yes)
        for row in rows:
            selection = str(row["selection"])
            state = settlement_state_for_market(
                market=market,
                selection=selection,
                line=line,
                actual_home_goals=facts["actual_home_goals"],
                actual_away_goals=facts["actual_away_goals"],
                actual_total_corners=facts["actual_total_corners"],
            )
            if market == "asian_handicap":
                ah_accumulators[(float(line), selection)].add(state)
            if _is_binary_metric_line(market, line):
                outcome = 1 if state == "WON" else 0
                probability = float(row["effective_win_stake_probability"])
                market_binary_accumulators[(market, line, selection)].add(probability, outcome)
                market_binary_by_competition[(competition_id, market, line, selection)].add(probability, outcome)
                market_binary_by_season[(season_code, market, line, selection)].add(probability, outcome)

    for row in _iter_jsonl(path):
        key = (
            str(row["event_id"]),
            str(row["market_family"]),
            None if row["line"] is None else float(row["line"]),
        )
        if grouped_key is None:
            grouped_key = key
        if key != grouped_key:
            finalize_group(grouped_rows)
            grouped_rows = []
            grouped_key = key
        grouped_rows.append(row)
    finalize_group(grouped_rows)

    market_binary_rows: list[dict[str, Any]] = []
    calibration_rows: list[dict[str, Any]] = []
    for (market, line, selection), accumulator in sorted(market_binary_accumulators.items()):
        summary = accumulator.summary()
        market_binary_rows.append(
            {
                "market": market,
                "line": line,
                "selection": selection,
                "subset": "ALL_PREDICTIONS",
                "sample_size": summary["sample_size"],
                "hit_rate": summary["hit_rate"],
                "mean_predicted_probability": summary["mean_predicted_probability"],
                "actual_frequency": summary["actual_frequency"],
                "brier": summary["brier"],
                "log_loss": summary["log_loss"],
                "expected_calibration_error": summary["expected_calibration_error"],
            }
        )
        calibration_rows.extend(
            accumulator.calibration_rows(
                scope_type="overall",
                scope_value="ALL",
                market=market,
                line=line,
                selection=selection,
                subset="ALL_PREDICTIONS",
            )
        )

    competition_rows: list[dict[str, Any]] = []
    for competition_id in sorted({facts["competition_id"] for facts in fixture_facts.values()}):
        one_x_two_summary = one_x_two_by_competition[competition_id].summary()
        competition_rows.append(
            {
                "competition_id": competition_id,
                "1x2_accuracy": one_x_two_summary["top_probability_accuracy"],
                "1x2_brier": one_x_two_summary["multiclass_brier"],
                "1x2_log_loss": one_x_two_summary["multiclass_log_loss"],
            }
        )
        for selection, accumulator in one_x_two_by_competition[competition_id].by_selection.items():
            calibration_rows.extend(
                accumulator.calibration_rows(
                    scope_type="competition",
                    scope_value=competition_id,
                    market="1x2",
                    line=None,
                    selection=selection,
                    subset="ALL_PREDICTIONS",
                )
            )
        if btts_by_competition[competition_id].n > 0:
            calibration_rows.extend(
                btts_by_competition[competition_id].calibration_rows(
                    scope_type="competition",
                    scope_value=competition_id,
                    market="btts",
                    line=None,
                    selection="YES",
                    subset="ALL_PREDICTIONS",
                )
            )
    for season_code, accumulator in sorted(one_x_two_by_season.items()):
        for selection, selection_accumulator in accumulator.by_selection.items():
            calibration_rows.extend(
                selection_accumulator.calibration_rows(
                    scope_type="season",
                    scope_value=season_code,
                    market="1x2",
                    line=None,
                    selection=selection,
                    subset="ALL_PREDICTIONS",
                )
            )
    for (competition_id, market, line, selection), accumulator in sorted(market_binary_by_competition.items()):
        calibration_rows.extend(
            accumulator.calibration_rows(
                scope_type="competition",
                scope_value=competition_id,
                market=market,
                line=line,
                selection=selection,
                subset="ALL_PREDICTIONS",
            )
        )
    for (season_code, market, line, selection), accumulator in sorted(market_binary_by_season.items()):
        calibration_rows.extend(
            accumulator.calibration_rows(
                scope_type="season",
                scope_value=season_code,
                market=market,
                line=line,
                selection=selection,
                subset="ALL_PREDICTIONS",
            )
        )

    return {
        "one_x_two_overall": one_x_two_overall,
        "one_x_two_by_competition": one_x_two_by_competition,
        "one_x_two_by_season": one_x_two_by_season,
        "btts_overall": btts_overall,
        "btts_by_competition": btts_by_competition,
        "btts_by_season": btts_by_season,
        "market_binary_rows": market_binary_rows,
        "calibration_rows": calibration_rows,
        "ah_summaries": {
            f"{line}:{selection}": accumulator.summary()
            for (line, selection), accumulator in sorted(ah_accumulators.items())
        },
    }


def _forecast_error_summaries(fixture_facts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    metrics = {
        "expected_goals_home": ErrorAccumulator(),
        "expected_goals_away": ErrorAccumulator(),
        "expected_goals_total": ErrorAccumulator(),
        "expected_corners_home": ErrorAccumulator(),
        "expected_corners_away": ErrorAccumulator(),
        "expected_corners_total": ErrorAccumulator(),
        "shots_home": ErrorAccumulator(),
        "shots_away": ErrorAccumulator(),
        "shots_total": ErrorAccumulator(),
        "shots_on_target_home": ErrorAccumulator(),
        "shots_on_target_away": ErrorAccumulator(),
        "shots_on_target_total": ErrorAccumulator(),
    }
    by_competition: dict[str, dict[str, ErrorAccumulator]] = defaultdict(
        lambda: {name: ErrorAccumulator() for name in metrics}
    )
    by_season: dict[str, dict[str, ErrorAccumulator]] = defaultdict(
        lambda: {name: ErrorAccumulator() for name in metrics}
    )
    for facts in fixture_facts.values():
        metrics["expected_goals_home"].add(facts["expected_goals_home"], facts["actual_home_goals"])
        metrics["expected_goals_away"].add(facts["expected_goals_away"], facts["actual_away_goals"])
        metrics["expected_goals_total"].add(facts["expected_total_goals"], facts["actual_total_goals"])
        if facts["actual_home_corners"] is not None:
            metrics["expected_corners_home"].add(facts["predicted_home_corners"], facts["actual_home_corners"])
        if facts["actual_away_corners"] is not None:
            metrics["expected_corners_away"].add(facts["predicted_away_corners"], facts["actual_away_corners"])
        metrics["expected_corners_total"].add(facts["predicted_total_corners"], facts["actual_total_corners"])
        if facts["actual_home_shots"] is not None:
            metrics["shots_home"].add(facts["predicted_home_shots"], facts["actual_home_shots"])
        if facts["actual_away_shots"] is not None:
            metrics["shots_away"].add(facts["predicted_away_shots"], facts["actual_away_shots"])
        if facts["actual_total_shots"] is not None:
            metrics["shots_total"].add(facts["predicted_total_shots"], facts["actual_total_shots"])
        if facts["actual_home_shots_on_target"] is not None:
            metrics["shots_on_target_home"].add(facts["predicted_home_shots_on_target"], facts["actual_home_shots_on_target"])
        if facts["actual_away_shots_on_target"] is not None:
            metrics["shots_on_target_away"].add(facts["predicted_away_shots_on_target"], facts["actual_away_shots_on_target"])
        if facts["actual_total_shots_on_target"] is not None:
            metrics["shots_on_target_total"].add(facts["predicted_total_shots_on_target"], facts["actual_total_shots_on_target"])

        competition_metrics = by_competition[facts["competition_id"]]
        season_metrics = by_season[facts["season_code"]]
        for target in (competition_metrics, season_metrics):
            target["expected_goals_home"].add(facts["expected_goals_home"], facts["actual_home_goals"])
            target["expected_goals_away"].add(facts["expected_goals_away"], facts["actual_away_goals"])
            target["expected_goals_total"].add(facts["expected_total_goals"], facts["actual_total_goals"])
            if facts["actual_home_corners"] is not None:
                target["expected_corners_home"].add(facts["predicted_home_corners"], facts["actual_home_corners"])
            if facts["actual_away_corners"] is not None:
                target["expected_corners_away"].add(facts["predicted_away_corners"], facts["actual_away_corners"])
            target["expected_corners_total"].add(facts["predicted_total_corners"], facts["actual_total_corners"])
            if facts["actual_home_shots"] is not None:
                target["shots_home"].add(facts["predicted_home_shots"], facts["actual_home_shots"])
            if facts["actual_away_shots"] is not None:
                target["shots_away"].add(facts["predicted_away_shots"], facts["actual_away_shots"])
            if facts["actual_total_shots"] is not None:
                target["shots_total"].add(facts["predicted_total_shots"], facts["actual_total_shots"])
            if facts["actual_home_shots_on_target"] is not None:
                target["shots_on_target_home"].add(facts["predicted_home_shots_on_target"], facts["actual_home_shots_on_target"])
            if facts["actual_away_shots_on_target"] is not None:
                target["shots_on_target_away"].add(facts["predicted_away_shots_on_target"], facts["actual_away_shots_on_target"])
            if facts["actual_total_shots_on_target"] is not None:
                target["shots_on_target_total"].add(facts["predicted_total_shots_on_target"], facts["actual_total_shots_on_target"])
    return {
        "overall": {name: accumulator.summary() for name, accumulator in metrics.items()},
        "by_competition": {
            competition_id: {name: accumulator.summary() for name, accumulator in metric_map.items()}
            for competition_id, metric_map in by_competition.items()
        },
        "by_season": {
            season_code: {name: accumulator.summary() for name, accumulator in metric_map.items()}
            for season_code, metric_map in by_season.items()
        },
    }


def _competition_market_rows(
    *,
    fixture_facts: dict[str, dict[str, Any]],
    one_x_two_by_competition: dict[str, MulticlassAccumulator],
    forecast_errors: dict[str, Any],
    candidate_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    counts = Counter(facts["competition_id"] for facts in fixture_facts.values())
    rows: list[dict[str, Any]] = []
    for competition_id in sorted(counts):
        one_x_two_summary = one_x_two_by_competition[competition_id].summary()
        error_summary = forecast_errors["by_competition"][competition_id]
        rows.append(
            {
                "competition_id": competition_id,
                "fixtures": counts[competition_id],
                "1x2_accuracy": _round(one_x_two_summary["top_probability_accuracy"]),
                "1x2_brier": _round(one_x_two_summary["multiclass_brier"]),
                "1x2_log_loss": _round(one_x_two_summary["multiclass_log_loss"]),
                "goals_mae": _round(error_summary["expected_goals_total"]["mae"]),
                "corners_mae": _round(error_summary["expected_corners_total"]["mae"]),
                "candidate_count": candidate_summary["candidate_count_by_competition"].get(competition_id, 0),
                "candidate_roi": None,
                "status": "PRICE_CONTEXT_CLOSING_ONLY",
            }
        )
    return rows


def _season_rows(
    *,
    fixture_facts: dict[str, dict[str, Any]],
    one_x_two_by_season: dict[str, MulticlassAccumulator],
    forecast_errors: dict[str, Any],
) -> list[dict[str, Any]]:
    counts = Counter(facts["season_code"] for facts in fixture_facts.values())
    rows: list[dict[str, Any]] = []
    for season_code in sorted(counts):
        one_x_two_summary = one_x_two_by_season[season_code].summary()
        error_summary = forecast_errors["by_season"][season_code]
        rows.append(
            {
                "season_code": season_code,
                "fixtures": counts[season_code],
                "1x2_accuracy": _round(one_x_two_summary["top_probability_accuracy"]),
                "1x2_brier": _round(one_x_two_summary["multiclass_brier"]),
                "1x2_log_loss": _round(one_x_two_summary["multiclass_log_loss"]),
                "goals_mae": _round(error_summary["expected_goals_total"]["mae"]),
                "corners_mae": _round(error_summary["expected_corners_total"]["mae"]),
                "status": "READY",
            }
        )
    return rows


def _market_status_rows(
    *,
    one_x_two_overall: MulticlassAccumulator,
    btts_overall: BinaryMetricAccumulator,
    market_binary_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in market_binary_rows:
        by_market[row["market"]].append(row)
    rows = [
        {
            "market": "1X2",
            "line": None,
            "selection": "ALL",
            "sample_size": one_x_two_overall.n,
            "hit_rate": one_x_two_overall.summary()["top_probability_accuracy"],
            "mean_predicted_probability": None,
            "actual_frequency": None,
            "brier": one_x_two_overall.summary()["multiclass_brier"],
            "log_loss": one_x_two_overall.summary()["multiclass_log_loss"],
            "avg_odds": None,
            "break_even_rate": None,
            "roi": None,
            "status": "READY",
            "notes": "Top-probability selection accuracy. Historical profitability remains unproven because only closing 1X2 snapshots are available.",
        },
        {
            "market": "BTTS",
            "line": None,
            "selection": "YES",
            "sample_size": btts_overall.n,
            "hit_rate": btts_overall.summary()["hit_rate"],
            "mean_predicted_probability": btts_overall.summary()["mean_predicted_probability"],
            "actual_frequency": btts_overall.summary()["actual_frequency"],
            "brier": btts_overall.summary()["brier"],
            "log_loss": btts_overall.summary()["log_loss"],
            "avg_odds": None,
            "break_even_rate": None,
            "roi": None,
            "status": "READY",
            "notes": "Binary event benchmark from the frozen Day 10 model outputs.",
        },
    ]
    for market_name in ("goals_total", "corner_total"):
        relevant_rows = [row for row in by_market.get(market_name, []) if row["line"] in {1.5, 2.5, 8.5, 9.5, 10.5}]
        if not relevant_rows:
            rows.append(
                {
                    "market": market_name.upper(),
                    "line": None,
                    "selection": "ALL",
                    "sample_size": 0,
                    "hit_rate": None,
                    "mean_predicted_probability": None,
                    "actual_frequency": None,
                    "brier": None,
                    "log_loss": None,
                    "avg_odds": None,
                    "break_even_rate": None,
                    "roi": None,
                    "status": "PARTIAL",
                    "notes": "No supported half-line records were available for the compact market summary.",
                }
            )
            continue
        pick = max(relevant_rows, key=lambda row: row["sample_size"])
        rows.append(
            {
                "market": market_name.upper(),
                "line": pick["line"],
                "selection": pick["selection"],
                "sample_size": pick["sample_size"],
                "hit_rate": pick["hit_rate"],
                "mean_predicted_probability": pick["mean_predicted_probability"],
                "actual_frequency": pick["actual_frequency"],
                "brier": pick["brier"],
                "log_loss": pick["log_loss"],
                "avg_odds": None,
                "break_even_rate": None,
                "roi": None,
                "status": "READY",
                "notes": "Representative half-line benchmark row shown in the compact market table.",
            }
        )
    rows.append(
        {
            "market": "ASIAN_HANDICAP",
            "line": None,
            "selection": "ALL",
            "sample_size": 0,
            "hit_rate": None,
            "mean_predicted_probability": None,
            "actual_frequency": None,
            "brier": None,
            "log_loss": None,
            "avg_odds": None,
            "break_even_rate": None,
            "roi": None,
            "status": "SETTLEMENT_READY",
            "notes": "Settlement-aware grading is supported, but naive binary accuracy is intentionally not reported.",
        }
    )
    return rows


def _strength_score(row: dict[str, Any]) -> float:
    hit_rate = row.get("hit_rate") or 0.0
    brier_value = row.get("brier")
    log_loss_value = row.get("log_loss")
    penalty = 0.0
    if brier_value is not None:
        penalty += float(brier_value)
    if log_loss_value is not None:
        penalty += min(1.0, float(log_loss_value))
    return hit_rate - penalty


def _manager_summary(
    *,
    fixture_facts: dict[str, dict[str, Any]],
    one_x_two_overall: MulticlassAccumulator,
    btts_overall: BinaryMetricAccumulator,
    market_rows: list[dict[str, Any]],
    forecast_errors: dict[str, Any],
) -> dict[str, Any]:
    strongest_market = max(
        [row for row in market_rows if row["status"] == "READY"],
        key=_strength_score,
    )
    weakest_market = min(
        [row for row in market_rows if row["status"] == "READY"],
        key=_strength_score,
    )
    overall_goals_mae = forecast_errors["overall"]["expected_goals_total"]["mae"]
    overall_corners_mae = forecast_errors["overall"]["expected_corners_total"]["mae"]
    calibration_ece = mean(
        [
            value
            for value in [
                one_x_two_overall.by_selection["HOME"].expected_calibration_error(),
                one_x_two_overall.by_selection["DRAW"].expected_calibration_error(),
                one_x_two_overall.by_selection["AWAY"].expected_calibration_error(),
                btts_overall.expected_calibration_error(),
            ]
            if value is not None
        ]
    )
    model_validation = "PROMISING" if one_x_two_overall.n >= 9000 and calibration_ece <= 0.08 else "MIXED"
    return {
        "MODEL_VALIDATION": model_validation,
        "PROFITABILITY_PROVEN": "NO",
        "CLV_PROVEN": "NO",
        "LIVE_EDGE_PROVEN": "NO",
        "evaluation_sample": len(fixture_facts),
        "one_x_two_accuracy": _round(one_x_two_overall.summary()["top_probability_accuracy"]),
        "one_x_two_brier": _round(one_x_two_overall.summary()["multiclass_brier"]),
        "one_x_two_log_loss": _round(one_x_two_overall.summary()["multiclass_log_loss"]),
        "goals_mae": _round(overall_goals_mae),
        "corners_mae": _round(overall_corners_mae),
        "calibration_summary": f"average expected calibration error {calibration_ece:.4f}",
        "strongest_market": strongest_market["market"],
        "weakest_market": weakest_market["market"],
        "historical_profitability_evidence": "Only closing 1X2 snapshots exist historically, so entry-time ROI and CLV are not proven.",
        "clv_availability": CLV_STATUS_UNAVAILABLE,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _confusion_rows(accumulator: MulticlassAccumulator) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for actual in ("HOME", "DRAW", "AWAY"):
        for predicted in ("HOME", "DRAW", "AWAY"):
            rows.append(
                {
                    "actual_selection": actual,
                    "predicted_selection": predicted,
                    "count": accumulator.confusion[(actual, predicted)],
                }
            )
    return rows


def _build_report(
    *,
    summary: dict[str, Any],
    chronology: dict[str, Any],
    candidate_summary: dict[str, Any],
    market_rows: list[dict[str, Any]],
    competition_rows: list[dict[str, Any]],
    forecast_errors: dict[str, Any],
) -> str:
    market_lines = "\n".join(
        f"| {row['market']} | {row['line'] if row['line'] is not None else 'ALL'} | {row['selection']} | {row['sample_size']} | "
        f"{'N/A' if row['hit_rate'] is None else f'{row['hit_rate']:.2%}'} | "
        f"{'N/A' if row['brier'] is None else f'{row['brier']:.4f}'} | "
        f"{'N/A' if row['log_loss'] is None else f'{row['log_loss']:.4f}'} | {row['status']} |"
        for row in market_rows
    )
    competition_lines = "\n".join(
        f"| {row['competition_id']} | {row['fixtures']} | "
        f"{'N/A' if row['1x2_accuracy'] is None else f'{row['1x2_accuracy']:.2%}'} | "
        f"{'N/A' if row['1x2_brier'] is None else f'{row['1x2_brier']:.4f}'} | "
        f"{'N/A' if row['1x2_log_loss'] is None else f'{row['1x2_log_loss']:.4f}'} | "
        f"{'N/A' if row['goals_mae'] is None else f'{row['goals_mae']:.3f}'} | "
        f"{'N/A' if row['corners_mae'] is None else f'{row['corners_mae']:.3f}'} | "
        f"{row['candidate_count']} | {row['status']} |"
        for row in competition_rows
    )
    return (
        "# FOOTBALL MODEL PERFORMANCE REPORT\n\n"
        f"- generated at: `{_utc_now()}`\n"
        f"- benchmark version: `{EXPERIMENT_VERSION}`\n"
        f"- MODEL_VALIDATION: `{summary['MODEL_VALIDATION']}`\n"
        f"- PROFITABILITY_PROVEN: `{summary['PROFITABILITY_PROVEN']}`\n"
        f"- CLV_PROVEN: `{summary['CLV_PROVEN']}`\n"
        f"- LIVE_EDGE_PROVEN: `{summary['LIVE_EDGE_PROVEN']}`\n\n"
        "## Manager Summary\n\n"
        f"- The frozen football model is technically `{summary['MODEL_VALIDATION'].lower()}` over `{summary['evaluation_sample']}` historical fixtures.\n"
        f"- 1X2 top-probability accuracy: `{summary['one_x_two_accuracy']:.2%}`.\n"
        f"- 1X2 multiclass Brier: `{summary['one_x_two_brier']:.4f}`.\n"
        f"- 1X2 multiclass log loss: `{summary['one_x_two_log_loss']:.4f}`.\n"
        f"- Goals MAE: `{summary['goals_mae']:.3f}`.\n"
        f"- Corners MAE: `{summary['corners_mae']:.3f}`.\n"
        f"- Calibration: `{summary['calibration_summary']}`.\n"
        f"- Strongest market in the current measurable slice: `{summary['strongest_market']}`.\n"
        f"- Weakest market in the current measurable slice: `{summary['weakest_market']}`.\n"
        f"- Historical profitability: `{summary['historical_profitability_evidence']}`.\n"
        f"- CLV availability: `{summary['clv_availability']}`.\n\n"
        "## Chronology\n\n"
        f"- rows checked: `{chronology['rows_checked']}`\n"
        f"- `as_of > kickoff`: `{chronology['as_of_after_kickoff']}`\n"
        f"- `as_of == kickoff`: `{chronology['as_of_equal_kickoff']}`\n"
        f"- chronology status: `{chronology['chronology_status']}`\n"
        f"- source-time check: `{chronology['source_time_status']}`\n\n"
        "## Historical Price Context\n\n"
        "- Historical market quotes in the loaded repo are `1X2` closing snapshots only.\n"
        "- They are valid for market-comparison context and historical manager demonstration rows.\n"
        "- They are not valid proof of entry-time ROI or CLV.\n"
        f"- preliminary candidate count from Day 11A demo lane: `{candidate_summary['candidate_count']}`\n"
        f"- average closing snapshot price across those demo candidates: `{candidate_summary['average_closing_snapshot_price']:.3f}`\n"
        f"- closing-snapshot break-even rate: `{candidate_summary['closing_snapshot_break_even_rate']:.2%}`\n\n"
        "## Market Benchmark\n\n"
        "| Market | Line | Selection | Sample | Hit Rate | Brier | Log Loss | Status |\n"
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |\n"
        f"{market_lines}\n\n"
        "## Competition Breakdown\n\n"
        "| Competition | Fixtures | 1X2 Accuracy | 1X2 Brier | 1X2 Log Loss | Goals MAE | Corners MAE | Candidate Count | Status |\n"
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |\n"
        f"{competition_lines}\n\n"
        "## Forecast Error\n\n"
        f"- expected total goals MAE: `{forecast_errors['overall']['expected_goals_total']['mae']:.3f}`\n"
        f"- expected total corners MAE: `{forecast_errors['overall']['expected_corners_total']['mae']:.3f}`\n"
        f"- shots total MAE: `{forecast_errors['overall']['shots_total']['mae']:.3f}`\n"
        f"- shots on target total MAE: `{forecast_errors['overall']['shots_on_target_total']['mae']:.3f}`\n"
    )


def _build_improvement_backlog(
    *,
    summary: dict[str, Any],
    chronology: dict[str, Any],
    candidate_summary: dict[str, Any],
    market_rows: list[dict[str, Any]],
) -> str:
    weakest_ready = min([row for row in market_rows if row["status"] == "READY"], key=_strength_score)
    return (
        "# FOOTBALL MODEL IMPROVEMENT BACKLOG\n\n"
        "1. Add a genuine current odds provider and append-only entry-time market capture so CLV and real paper ROI can be measured.\n"
        "2. Expand historical market coverage beyond closing 1X2 snapshots so BTTS, totals, Asian handicap, and corners can be benchmarked against true market prices.\n"
        f"3. Review the weakest currently measurable market slice: `{weakest_ready['market']}`.\n"
        f"4. Preserve the no-leakage benchmark discipline. Current `as_of > kickoff` count is `{chronology['as_of_after_kickoff']}` and must remain zero.\n"
        f"5. Tighten candidate-selection evidence only after better market capture exists. Current historical demo candidate count is `{candidate_summary['candidate_count']}` but price timing is still closing-only.\n"
        f"6. If the validation state remains `{summary['MODEL_VALIDATION']}`, prioritize calibration and market data quality before any model retuning.\n"
    )


def run_historical_performance_benchmark(
    config: PerformanceBenchmarkConfig | None = None,
) -> dict[str, Any]:
    resolved_config = PerformanceBenchmarkConfig.default() if config is None else config
    fixture_facts, chronology = _load_fixture_facts(
        resolved_config.day8_predictions_path,
        research_db_path=resolved_config.research_db_path,
    )
    candidate_summary = _candidate_summary(resolved_config.day11_decisions_path)
    streamed_metrics = _stream_day10_metrics(
        path=resolved_config.day10_market_prices_path,
        fixture_facts=fixture_facts,
    )
    forecast_errors = _forecast_error_summaries(fixture_facts)
    market_rows = _market_status_rows(
        one_x_two_overall=streamed_metrics["one_x_two_overall"],
        btts_overall=streamed_metrics["btts_overall"],
        market_binary_rows=streamed_metrics["market_binary_rows"],
    )
    competition_rows = _competition_market_rows(
        fixture_facts=fixture_facts,
        one_x_two_by_competition=streamed_metrics["one_x_two_by_competition"],
        forecast_errors=forecast_errors,
        candidate_summary=candidate_summary,
    )
    season_rows = _season_rows(
        fixture_facts=fixture_facts,
        one_x_two_by_season=streamed_metrics["one_x_two_by_season"],
        forecast_errors=forecast_errors,
    )
    manager_summary = _manager_summary(
        fixture_facts=fixture_facts,
        one_x_two_overall=streamed_metrics["one_x_two_overall"],
        btts_overall=streamed_metrics["btts_overall"],
        market_rows=market_rows,
        forecast_errors=forecast_errors,
    )
    output_payload = {
        "generated_at": _utc_now(),
        "benchmark_version": EXPERIMENT_VERSION,
        "date_stamp": DATE_STAMP,
        "summary": manager_summary,
        "chronology": chronology,
        "candidate_summary": candidate_summary,
        "market_rows": market_rows,
        "competition_rows": competition_rows,
        "season_rows": season_rows,
        "calibration_row_count": len(streamed_metrics["calibration_rows"]),
        "confusion_matrix_rows": _confusion_rows(streamed_metrics["one_x_two_overall"]),
        "ah_settlement_summary": streamed_metrics["ah_summaries"],
        "forecast_errors": forecast_errors,
        "status_flags": {
            "HISTORICAL_BENCHMARK_COMPLETE": "YES",
            "PROBABILITY_CALIBRATION_MEASURED": "YES",
            "HISTORICAL_PROFITABILITY_PROVEN": "NO",
            "CURRENT_ODDS_PROVIDER_CONNECTED": "NO",
            "PAPER_TRADING_LEDGER_READY": "YES",
            "CURRENT_PAPER_TRACKING_ACTIVE": "NO",
            "CLV_TRACKING_READY": "YES",
            "LIVE_CLV_PROVEN": "NO",
            "LIVE_PROFITABILITY_PROVEN": "NO",
            "TENNIS_REMAINS_PAUSED": "YES",
        },
    }

    output_dir = resolved_config.output_dir
    _write_json(output_dir / SUMMARY_FILENAME, output_payload)
    _write_csv(output_dir / MARKET_CSV_FILENAME, market_rows)
    _write_csv(output_dir / COMPETITION_CSV_FILENAME, competition_rows)
    _write_csv(output_dir / SEASON_CSV_FILENAME, season_rows)
    _write_csv(output_dir / CALIBRATION_CSV_FILENAME, streamed_metrics["calibration_rows"])
    _write_csv(output_dir / CONFUSION_CSV_FILENAME, _confusion_rows(streamed_metrics["one_x_two_overall"]))
    (output_dir / REPORT_FILENAME).write_text(
        _build_report(
            summary=manager_summary,
            chronology=chronology,
            candidate_summary=candidate_summary,
            market_rows=market_rows,
            competition_rows=competition_rows,
            forecast_errors=forecast_errors,
        ),
        encoding="utf-8",
    )
    (output_dir / IMPROVEMENT_BACKLOG_FILENAME).write_text(
        _build_improvement_backlog(
            summary=manager_summary,
            chronology=chronology,
            candidate_summary=candidate_summary,
            market_rows=market_rows,
        ),
        encoding="utf-8",
    )
    return output_payload


def main() -> int:
    payload = run_historical_performance_benchmark()
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
