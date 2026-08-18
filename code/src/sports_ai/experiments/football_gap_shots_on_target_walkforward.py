"""Chronological football GAP walk-forward experiment for shots on target.

This is the second bounded Day 7 statistic lane. It reuses the same chronology,
warm-up, isolation, and reproducibility shell as the shots runner while keeping
the statistic-specific state independent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from .football_gap_shots_walkforward import (
    DEFAULT_LEARNING_RATE,
    LEARNING_RATE_LABEL,
    run_gap_walkforward_experiment,
    write_gap_walkforward_artifacts,
)

EXPERIMENT_ID = "EXP-008"
EXPERIMENT_VERSION = "football_gap_shots_on_target_walkforward_v1"
ENGINE_MODEL_VERSION = "football_gap_shots_on_target_reference_v1"
STATISTIC = "shots_on_target"


def run_gap_shots_on_target_walkforward_experiment(
    *,
    research_db_path: str | Path,
    learning_rate: float = DEFAULT_LEARNING_RATE,
    model_version: str = ENGINE_MODEL_VERSION,
) -> dict[str, Any]:
    return run_gap_walkforward_experiment(
        research_db_path=research_db_path,
        statistic=STATISTIC,
        learning_rate=learning_rate,
        model_version=model_version,
        experiment_id=EXPERIMENT_ID,
        experiment_version=EXPERIMENT_VERSION,
        experiment_module="sports_ai.experiments.football_gap_shots_on_target_walkforward",
    )


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the chronological football GAP shots-on-target walk-forward experiment."
    )
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument("--manifest-out", help="Optional manifest JSON path.")
    parser.add_argument("--predictions-out", help="Optional predictions JSONL path.")
    parser.add_argument("--final-state-out", help="Optional final state JSON path.")
    parser.add_argument("--markdown-out", help="Optional Markdown summary path.")
    parser.add_argument("--learning-rate", type=float, default=DEFAULT_LEARNING_RATE, help="Fixed reference learning rate.")
    args = parser.parse_args(argv)

    result = run_gap_shots_on_target_walkforward_experiment(
        research_db_path=args.research_db,
        learning_rate=args.learning_rate,
        model_version=ENGINE_MODEL_VERSION,
    )

    payload: dict[str, Any] = {
        "manifest": result["manifest"],
        "statistic": STATISTIC,
        "learning_rate_label": LEARNING_RATE_LABEL,
    }
    if args.manifest_out and args.predictions_out and args.final_state_out and args.markdown_out:
        payload["artifacts"] = write_gap_walkforward_artifacts(
            result,
            manifest_path=args.manifest_out,
            predictions_jsonl_path=args.predictions_out,
            final_state_path=args.final_state_out,
            markdown_path=args.markdown_out,
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
