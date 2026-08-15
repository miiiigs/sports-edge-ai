from pathlib import Path

from sports_ai.pipeline.daily import main


def test_daily_pipeline_logs_and_builds_dashboard(tmp_path):
    ledger_db = tmp_path / "paper_shadow.sqlite"
    dashboard_html = tmp_path / "dashboard.html"

    exit_code = main(
        [
            "--demo",
            "--ledger-db",
            str(ledger_db),
            "--closing-price",
            "1.66",
            "--settlement",
            "WIN",
            "--dashboard-html",
            str(dashboard_html),
        ]
    )

    assert exit_code == 0
    assert ledger_db.exists()
    assert dashboard_html.exists()
    text = dashboard_html.read_text(encoding="utf-8")
    assert "Sports Edge AI - Live Research Dashboard" in text
    assert "Performance &amp; Remarks" in text
