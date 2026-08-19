from pathlib import Path

from sports_ai.tools.chatgpt_handoff_bundle import HandoffBundleConfig, build_chatgpt_handoff_bundle


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_build_chatgpt_handoff_bundle_creates_summary_and_manifest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\ntestpaths = code/tests\n", encoding="utf-8")
    (repo / "SPORTS_EDGE_AI_MASTER_README.md").write_text("# Readme\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "PROJECT_STATUS.md").write_text("status\n", encoding="utf-8")
    (repo / "research").mkdir()
    (repo / "research" / "football_research").mkdir(parents=True, exist_ok=True)
    (repo / "research" / "football_research" / "football_data_quality_audit_v3_2026-08-15.md").write_text(
        "dq\n",
        encoding="utf-8",
    )
    (repo / "code").mkdir()
    (repo / "code" / "file.py").write_text("print('a')\n", encoding="utf-8")

    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")

    (repo / "code" / "file.py").write_text("print('b')\n", encoding="utf-8")
    (repo / "docs" / "BLOCKERS.md").write_text("blocker\n", encoding="utf-8")

    output_root = repo / "research" / "chatgpt_handoff"
    manifest = build_chatgpt_handoff_bundle(
        HandoffBundleConfig(
            repo_root=repo,
            output_root=output_root,
            bundle_date="2026-08-18",
            max_copy_bytes=1_000_000,
        )
    )

    bundle_root = output_root / "chatgpt_handoff_2026-08-18"
    assert bundle_root.exists()
    assert (bundle_root / "CHATGPT_HANDOFF_SUMMARY.md").exists()
    assert (bundle_root / "handoff_manifest.json").exists()
    assert (bundle_root / "git_status.txt").exists()
    assert (bundle_root / "git_diff.patch").exists()
    assert any(item["path"] == "code/file.py" for item in manifest["included_files"])
    assert any(item["path"] == "docs/BLOCKERS.md" for item in manifest["included_files"])
    assert any(item["path"] == "pytest.ini" for item in manifest["included_files"])
    assert (bundle_root / "files" / "pytest.ini").exists()
    summary_text = (bundle_root / "CHATGPT_HANDOFF_SUMMARY.md").read_text(encoding="utf-8")
    assert "implemented through Day 12" in summary_text
    assert "Day 12" in summary_text
    assert "Every completed milestone must now ship" in summary_text
