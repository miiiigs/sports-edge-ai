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
    "SPORTS_EDGE_AI_CODEX_MASTER_HANDOFF.md",
    "SPORTS_EDGE_AI_MASTER_README.md",
    "SPORTS_EDGE_AI_PROGRESS_CHECKLIST.md",
    "docs/PROJECT_STATUS.md",
    "docs/BLOCKERS.md",
    "docs/DECISION_LOG.md",
    "docs/EXPERIMENT_LOG.md",
    "docs/REQUIREMENTS_TRACEABILITY.md",
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
    "research/spec_migration/FOOTBALL_DAY10_ACCEPTANCE_AUDIT_2026-08-18.md",
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
            "- Forecast model quality is improving.",
            "- Decision-layer DQ semantics are much better than the original v3 proxy, but not yet conceptually approved.",
            "- Historical DQ v2 now has useful spread, but 548 of 636 old-policy candidates still fall below 0.82.",
            "- Dominant below-threshold reasons are model not converged, low recent match coverage, and high predictive uncertainty.",
            "- The v3 optimizer issue is real. At 250 iterations many events still move materially; at 1000 iterations fits are much closer to tolerance.",
            "- Policy-threshold tuning should remain blocked until DQ semantics and optimizer mechanics are resolved.",
            "- v4 forecasting work should stay separate from this decision-layer investigation.",
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
        "- validate the conceptual weights in historical DQ v2 without optimizing for candidate count",
        "- investigate why early-season decision quality scores higher than mid/late season",
        "- compare v3 forecasting behavior under 250, 500, and 1000 optimizer iterations",
        "- keep decision-threshold tuning blocked until DQ and optimizer investigations are resolved",
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
