"""Build a lightweight but faithful ChatGPT handoff bundle."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable


MAX_COPY_BYTES = 2_000_000
HEAD_TAIL_LINES = 40
TEXT_EXTENSIONS = {
    ".ini",
    ".md",
    ".txt",
    ".json",
    ".jsonl",
    ".csv",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
    ".html",
    ".sql",
}
PRIORITY_DOCS = (
    "pytest.ini",
    "SPORTS_EDGE_AI_CODEX_MASTER_HANDOFF.md",
    "SPORTS_EDGE_AI_MASTER_README.md",
    "SPORTS_EDGE_AI_PROGRESS_CHECKLIST.md",
    "docs/PROJECT_STATUS.md",
    "docs/BLOCKERS.md",
    "docs/DECISION_LOG.md",
    "docs/EXPERIMENT_LOG.md",
    "docs/REQUIREMENTS_TRACEABILITY.md",
)
PRIORITY_CODE = (
    "code/src/sports_ai/agents/football/gap_ratings.py",
    "code/src/sports_ai/experiments/football_gap_shots_walkforward.py",
    "code/src/sports_ai/experiments/football_gap_shots_on_target_walkforward.py",
    "code/src/sports_ai/experiments/football_gap_corners_walkforward.py",
    "code/src/sports_ai/experiments/football_gap_lambda_fitting.py",
    "code/src/sports_ai/experiments/football_day8_goal_conversion.py",
    "code/src/sports_ai/experiments/football_day9_scoreline_model.py",
    "code/src/sports_ai/experiments/football_day10_market_pricing.py",
    "code/src/sports_ai/experiments/football_day10_product_outputs.py",
    "code/src/sports_ai/experiments/football_day11_manager_core.py",
    "code/src/sports_ai/experiments/football_day11_product_outputs.py",
    "code/src/sports_ai/experiments/football_day12_safeguards.py",
    "code/src/sports_ai/experiments/football_day12_product_outputs.py",
    "code/src/sports_ai/tools/chatgpt_handoff_bundle.py",
    "code/tests/test_chatgpt_handoff_bundle.py",
    "code/tests/test_football_gap_shots_walkforward.py",
    "code/tests/test_football_gap_shots_on_target_walkforward.py",
    "code/tests/test_football_gap_corners_walkforward.py",
    "code/tests/test_football_gap_lambda_fitting.py",
    "code/tests/test_football_day8_goal_conversion.py",
    "code/tests/test_football_day9_scoreline_model.py",
    "code/tests/test_football_day10_market_pricing.py",
    "code/tests/test_football_day10_product_outputs.py",
    "code/tests/test_football_day11_manager_core.py",
    "code/tests/test_football_day11_product_outputs.py",
    "code/tests/test_football_day12_safeguards.py",
)
PRIORITY_REPORTS = (
    "research/football_research/football_data_quality_audit_v3_2026-08-15.md",
    "research/football_research/football_data_quality_audit_v3_2026-08-15.json",
    "research/football_research/football_v3_optimizer_diagnostic_2026-08-15.md",
    "research/football_research/football_v3_optimizer_diagnostic_2026-08-15.json",
    "research/football_research/decision_policy_validation_v3_2026-08-16.md",
    "research/football_research/decision_policy_validation_v3_2026-08-16.json",
    "research/football_research/decision_layer_audit_v3_2026-08-16.json",
    "research/football_research/football_day10_market_pricing_2026-08-18.md",
    "research/spec_migration/FOOTBALL_DAY12_ACCEPTANCE_AUDIT_2026-08-19.md",
    "research/spec_migration/FOOTBALL_DAY10_ACCEPTANCE_AUDIT_2026-08-18.md",
    "research/spec_migration/FOOTBALL_DAY11A_ACCEPTANCE_AUDIT_2026-08-19.md",
    "research/product_outputs/football/day10/FOOTBALL_DAY10_MODEL_PRICING_REPORT.md",
    "research/product_outputs/football/day10/football_day10_model_prices_sample.csv",
    "research/product_outputs/football/day10/football_day10_product_manifest.json",
    "research/product_outputs/football/day11/FOOTBALL_DAY11_MANAGER_REPORT.md",
    "research/product_outputs/football/day11/football_day11_product_manifest.json",
    "research/product_outputs/football/day12/FOOTBALL_DAY12_EXPERIMENTAL_CANDIDATE_REPORT.md",
    "research/product_outputs/football/day12/football_day12_product_manifest.json",
    "research/product_outputs/football/latest/FOOTBALL_MODEL_PRICES.md",
    "research/product_outputs/football/latest/football_model_prices_sample.csv",
    "research/product_outputs/football/latest/football_pipeline_manifest.json",
    "research/product_outputs/football/latest/FOOTBALL_DECISIONS.md",
    "research/product_outputs/football/latest/football_decisions_sample.csv",
    "research/product_outputs/football/latest/FOOTBALL_EXPERIMENTAL_CANDIDATES.md",
    "research/product_outputs/football/latest/football_experimental_candidates_sample.csv",
    "research/product_outputs/football/latest/FOOTBALL_CURRENT_STATUS.md",
)


@dataclass(frozen=True)
class HandoffBundleConfig:
    repo_root: str | Path
    output_root: str | Path
    bundle_date: str
    max_copy_bytes: int = MAX_COPY_BYTES


def _run_git(repo_root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout


def _is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS


def _read_text_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def _relative_key(path: Path, repo_root: Path) -> str:
    return path.relative_to(repo_root).as_posix()


def _copy_or_preview_file(
    *,
    repo_root: Path,
    source: Path,
    bundle_root: Path,
    max_copy_bytes: int,
) -> dict[str, Any]:
    relative = source.relative_to(repo_root)
    destination = bundle_root / "files" / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = source.stat().st_size

    if _is_text_file(source) and size <= max_copy_bytes:
        shutil.copy2(source, destination)
        return {
            "path": relative.as_posix(),
            "size_bytes": size,
            "included_mode": "full_copy",
        }

    preview_dir = bundle_root / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{relative.as_posix().replace('/', '__')}.preview.txt"

    if _is_text_file(source):
        lines = _read_text_lines(source)
        head = lines[:HEAD_TAIL_LINES]
        tail = lines[-HEAD_TAIL_LINES:] if len(lines) > HEAD_TAIL_LINES else []
        preview_text = [
            f"PATH: {relative.as_posix()}",
            f"SIZE_BYTES: {size}",
            "",
            "HEAD:",
            *head,
        ]
        if tail and tail != head:
            preview_text.extend(["", "TAIL:", *tail])
        preview_path.write_text("\n".join(preview_text), encoding="utf-8")
        return {
            "path": relative.as_posix(),
            "size_bytes": size,
            "included_mode": "preview_only",
            "preview_path": preview_path.relative_to(bundle_root).as_posix(),
        }

    return {
        "path": relative.as_posix(),
        "size_bytes": size,
        "included_mode": "metadata_only",
    }


def _select_existing(repo_root: Path, paths: Iterable[str]) -> list[Path]:
    selected: list[Path] = []
    for value in paths:
        candidate = repo_root / value
        if candidate.exists() and candidate.is_file():
            selected.append(candidate)
    return selected


def _summarize_current_state() -> str:
    return "\n".join(
        [
            "Current high-signal state:",
            "- The football migration slice is now implemented through Day 12 for the currently loaded scope, with Day 12 honestly marked `PARTIAL` because the required point-in-time-valid xG path is still missing.",
            "- Day 7, Day 8, Day 9, Day 10, and Day 11A all have explicit acceptance evidence and reproducible repo-backed artifacts.",
            "- Day 10 still provides the frozen fair-model pricing authority, and Day 11A now consumes that authority unchanged instead of rebuilding model prices.",
            "- The real historical/demo Day 11A manager run produced `28992` decision records: `10299` `PRELIMINARY_CANDIDATE` and `18693` `NO_BET`.",
            "- `DAY11_MANAGER_CORE_READY = YES` is now supported by implemented code, tests, technical artifacts, and manager-facing outputs.",
            "- `LIVE_PAPER_PIPELINE_READY = NO` remains the honest status because live provider and current forward-fit dependencies are still unresolved.",
            "- Day 10 derives `1X2`, `BTTS`, goals totals, Asian handicap, and corner totals from frozen pre-match model objects only.",
            "- Day 11A uses settlement-aware edge and minimum-price arithmetic directly from Day 10 `W` and `L`, plus normalized market snapshots, booksum validation, and Shin de-vig.",
            "- The currently supplied in-repo market snapshot path is a real historical/demo archived closing `1X2` slate, so the manager build is honest about its current input coverage and does not overstate point-in-time-live provenance.",
            "- The real Day 12 run consumed `10299` Day 11 preliminary candidates and produced `4317` `EXPERIMENTAL_CANDIDATE` plus `5982` `BLOCKED_MISSING_XG_DATA`.",
            "- `FOOTBALL_LOGICAL_PIPELINE_READY = NO`, `LIVE_PAPER_PIPELINE_READY = NO`, and `TENNIS AUTHORIZED = NO` remain the honest status after Day 12.",
            "- The next football dependency is the truthful xG/data-path resolution needed to move Day 12 from `PARTIAL` to `COMPLETE`, followed by the football logical-pipeline freeze before tennis begins.",
            "- Every completed milestone must now ship machine-readable artifacts, reproducibility manifests, a human-readable product/output report, and a compact latest/current artifact.",
        ]
    )


def build_chatgpt_handoff_bundle(config: HandoffBundleConfig) -> dict[str, Any]:
    repo_root = Path(config.repo_root).resolve()
    output_root = Path(config.output_root).resolve()
    bundle_root = output_root / f"chatgpt_handoff_{config.bundle_date}"
    bundle_root.mkdir(parents=True, exist_ok=True)

    git_status = _run_git(repo_root, ["status", "--short"])
    tracked_diff_names = _run_git(repo_root, ["diff", "--name-only"]).splitlines()
    untracked_names = _run_git(repo_root, ["ls-files", "--others", "--exclude-standard"]).splitlines()

    tracked_text_files = [
        repo_root / path
        for path in tracked_diff_names
        if (repo_root / path).exists() and _is_text_file(repo_root / path) and "__pycache__" not in path
    ]
    untracked_text_files = [
        repo_root / path
        for path in untracked_names
        if (repo_root / path).exists() and _is_text_file(repo_root / path) and "__pycache__" not in path
    ]

    status_path = bundle_root / "git_status.txt"
    status_path.write_text(git_status, encoding="utf-8")

    diff_path = bundle_root / "git_diff.patch"
    diff_path.write_text(_run_git(repo_root, ["diff", "--", *[str(path.relative_to(repo_root)) for path in tracked_text_files]]), encoding="utf-8")

    untracked_path = bundle_root / "untracked_files.txt"
    untracked_path.write_text("\n".join(untracked_names), encoding="utf-8")

    selected_paths: list[Path] = []
    selected_paths.extend(tracked_text_files)
    selected_paths.extend(untracked_text_files)
    selected_paths.extend(_select_existing(repo_root, PRIORITY_DOCS))
    selected_paths.extend(_select_existing(repo_root, PRIORITY_CODE))
    selected_paths.extend(_select_existing(repo_root, PRIORITY_REPORTS))

    deduped: dict[str, Path] = {}
    for item in selected_paths:
        deduped[_relative_key(item, repo_root)] = item
    selected_paths = [deduped[key] for key in sorted(deduped.keys())]

    included_files = [
        _copy_or_preview_file(
            repo_root=repo_root,
            source=source,
            bundle_root=bundle_root,
            max_copy_bytes=config.max_copy_bytes,
        )
        for source in selected_paths
    ]

    summary_lines = [
        "# ChatGPT Project Handoff",
        "",
        f"Bundle date: {config.bundle_date}",
        "",
        "## What this bundle contains",
        "",
        "- exact tracked modifications via `git_status.txt` and `git_diff.patch`",
        "- untracked project files via `untracked_files.txt`",
        "- changed and priority text files copied in full when small enough",
        "- large text artifacts included as previews when full copy would be heavy",
        "",
        "## Current state",
        "",
        _summarize_current_state(),
        "",
        "## Immediate next work",
        "",
        "- keep the implemented Day 12 layer frozen unless a real correctness issue is found",
        "- do not reopen closed Day 7 through Day 12 arithmetic layers casually",
        "- keep Day 11A's product boundary intact: frozen Day 10 fair price plus supplied market snapshot yields preliminary manager decisions, not live betting readiness",
        "- preserve the implemented Day 11A normalized market contract, Shin/reference booksum path, settlement-aware edge arithmetic, full decision logging, and Day 12 safeguard dispositions as the new frozen upstream baseline",
        "- preserve the historical/demo archived closing `1X2` limitation explicitly and do not upgrade it into a genuine point-in-time-live market snapshot claim",
        "- resolve the point-in-time-valid xG input dependency honestly before upgrading Day 12 beyond `PARTIAL`",
        "- handle live provider integration and current forward-fit readiness as parallel dependencies, not as excuses to reopen the accepted Day 11A or Day 12 core logic",
        "- stop short of tennis or shared later-day work until football is explicitly re-authorized",
        "",
        "## Files included",
        "",
        *(f"- {item['path']} [{item['included_mode']}]" for item in included_files),
        "",
        "## Notes",
        "",
        "- large `.jsonl` and database artifacts are previewed or referenced rather than fully copied when they would bloat the handoff",
        "- this bundle is intended to be uploaded to ChatGPT instead of the entire heavy repo",
    ]
    summary_path = bundle_root / "CHATGPT_HANDOFF_SUMMARY.md"
    summary_path.write_text("\n".join(summary_lines), encoding="utf-8")

    manifest = {
        "bundle_root": str(bundle_root),
        "bundle_date": config.bundle_date,
        "git_status_path": status_path.name,
        "git_diff_path": diff_path.name,
        "untracked_path": untracked_path.name,
        "summary_path": summary_path.name,
        "included_files": included_files,
    }
    manifest_path = bundle_root / "handoff_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    bundle_date = date.today().isoformat()
    manifest = build_chatgpt_handoff_bundle(
        HandoffBundleConfig(
            repo_root=Path.cwd().parent,
            output_root=Path.cwd().parent / "research" / "chatgpt_handoff",
            bundle_date=bundle_date,
        )
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
