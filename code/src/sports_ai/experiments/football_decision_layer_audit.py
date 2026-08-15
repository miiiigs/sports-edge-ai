"""Decision-layer diagnostics around frozen football probability models."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Sequence

from .football_poisson_backtest import FootballPoissonBacktestConfig, run_football_poisson_backtest


@dataclass(frozen=True)
class PortsmouthLikeSliceConfig:
    competition_id: str = "ENG2"
    selection: str = "home"
    min_probability: float = 0.40
    max_probability: float = 0.50
    min_edge: float = 0.03
    max_edge: float = 0.05
    max_fair_odds: float = 2.8


def _safe_mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def _summarize_candidate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    units = [row["unit_return"] for row in rows]
    odds = [row["available_price"] for row in rows]
    model_vs_market = [row["model_vs_market"] for row in rows if row.get("model_vs_market") is not None]
    probabilities = [row["fair_probability"] for row in rows]
    wins = [1 if row.get("settlement") == "WIN" else 0 for row in rows]
    return {
        "sample_size": len(rows),
        "win_rate": _safe_mean(wins),
        "average_odds": _safe_mean(odds),
        "roi_per_bet": (sum(units) / len(units)) if units else None,
        "average_model_probability": _safe_mean(probabilities),
        "realized_rate": _safe_mean(wins),
        "calibration_gap": (
            _safe_mean(probabilities) - _safe_mean(wins)
            if probabilities and wins
            else None
        ),
        "average_model_market_discrepancy": _safe_mean(model_vs_market),
        "clv": None,
        "clv_note": "Historical football market quotes currently provide entry/closing comparison only where separate captured prices exist; this slice has no reliable CLV field.",
    }


def _slice_rows(rows: list[dict[str, Any]], config: PortsmouthLikeSliceConfig) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("competition_id") == config.competition_id
        and row.get("selection") == config.selection
        and config.min_probability <= row.get("fair_probability", -1) < config.max_probability
        and config.min_edge <= row.get("edge", -1) < config.max_edge
        and row.get("fair_odds", 999) <= config.max_fair_odds
    ]


def build_decision_layer_audit(
    *,
    research_db_path: str | Path,
    season_code: str,
    history_season_codes: Sequence[str],
    competition_ids: Sequence[str],
    output_path: str | Path,
) -> dict[str, Any]:
    """Build the first policy-focused audit report around frozen v3."""

    backtest = run_football_poisson_backtest(
        FootballPoissonBacktestConfig(
            research_db_path=research_db_path,
            season_code=season_code,
            competition_ids=tuple(competition_ids),
            model_name="poisson_1x2_v3",
            strength_regularization=0.03,
            edge_threshold=0.03,
            max_fair_odds=None,
            allowed_selections=("home", "draw", "away"),
            history_season_codes=tuple(history_season_codes),
            include_candidate_rows=True,
        )
    )
    candidate_rows = backtest.get("candidate_rows", [])
    slice_config = PortsmouthLikeSliceConfig()
    sliced_rows = _slice_rows(candidate_rows, slice_config)
    reports = backtest["reports"]
    reliability = {
        "by_competition": reports.get("by_competition", {}),
        "by_selection": reports.get("by_selection", {}),
        "by_competition_selection": reports.get("by_competition_selection", {}),
    }
    audit = {
        "model_name": "poisson_1x2_v3",
        "model_status": "frozen_promoted_probability_model",
        "policy_status": "under_decision_layer_audit",
        "season_code": season_code,
        "history_season_codes": list(history_season_codes),
        "competition_ids": list(competition_ids),
        "model_evaluation": backtest["model_evaluation"]["prediction_metrics"],
        "policy_evaluation": backtest["policy_evaluation"]["candidate_metrics"],
        "portsmouth_like_slice": {
            "definition": {
                "competition_id": slice_config.competition_id,
                "selection": slice_config.selection,
                "model_probability_range": [slice_config.min_probability, slice_config.max_probability],
                "edge_range": [slice_config.min_edge, slice_config.max_edge],
                "max_fair_odds": slice_config.max_fair_odds,
            },
            "summary": _summarize_candidate_rows(sliced_rows),
            "sample_rows": sliced_rows[:20],
        },
        "reliability_metadata": reliability,
        "interpretation": [
            "This audit is policy-focused. It should not be used to retune frozen v3 from one live loss.",
            "Probability quality and betting-policy quality are reported separately.",
            "CLV remains unavailable for this historical slice unless trusted entry and close timestamps are present.",
        ],
    }
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    return audit


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build a frozen-v3 football decision-layer audit report.")
    parser.add_argument("--research-db", required=True)
    parser.add_argument("--season", default="2526")
    parser.add_argument("--history-season", action="append", dest="history_seasons", required=True)
    parser.add_argument("--competition", action="append", dest="competitions", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    audit = build_decision_layer_audit(
        research_db_path=args.research_db,
        season_code=args.season,
        history_season_codes=tuple(args.history_seasons),
        competition_ids=tuple(args.competitions),
        output_path=args.output,
    )
    print(json.dumps({"output": args.output, "portsmouth_like_slice": audit["portsmouth_like_slice"]["summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
