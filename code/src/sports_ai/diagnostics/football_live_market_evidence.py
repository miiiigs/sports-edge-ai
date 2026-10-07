"""Read-only live-market evidence diagnostics for the current football slate.

This entry point answers, for every fixture on the current slate, whether usable
Asian Handicap and Corner Total evidence exists:

* ``AVAILABLE``           fresh pre-match pair at an explicit line
* ``STALE``               only stale or in-play observations
* ``MISSING``             no persisted quotes for the market
* ``INVALID``             persisted rows exist but are malformed
* ``IDENTITY_UNRESOLVED`` the fixture cannot be mapped to a canonical Model Pick

It is strictly observational: it opens the operational databases read-only,
performs ZERO provider requests, writes nothing, and never touches Model Picks,
paper entries, or settlement.

Run ``python -m sports_ai.diagnostics.football_live_market_evidence`` for the
current operational paths, or pass an explicit database path.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..dashboard.runtime_paths import resolve_dashboard_operational_paths
from ..market_data.market_evidence import (
    audit_provider_request_accounting,
    build_market_evidence_coverage,
    documented_provider_limits,
    resolve_actionable_max_age_minutes,
    resolve_collector_health_max_age_minutes,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _connect_readonly(db_path: str | Path) -> sqlite3.Connection | None:
    path = Path(db_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (table,),
    ).fetchone()
    return bool(row and int(row[0]) > 0)


def load_slate_fixtures(dashboard_conn: sqlite3.Connection | None) -> list[dict[str, Any]]:
    if dashboard_conn is None or not _has_table(dashboard_conn, "dashboard_fixtures"):
        return []
    rows = dashboard_conn.execute(
        """
        SELECT fixture_id, provider, provider_fixture_id, competition_id, competition_name,
               division_code, home_team_name, away_team_name, kickoff_utc, status
        FROM dashboard_fixtures
        ORDER BY kickoff_utc, fixture_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def run_diagnostics(
    *,
    dashboard_db_path: str | Path | None = None,
    paper_db_path: str | Path | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    repo_root = _repo_root()
    paths = resolve_dashboard_operational_paths(repo_root)
    resolved_dashboard = Path(dashboard_db_path) if dashboard_db_path else paths.dashboard_db_path
    resolved_paper = Path(paper_db_path) if paper_db_path else paths.paper_trading_db_path
    resolved_now = now_utc or datetime.now(timezone.utc).isoformat(timespec="seconds")

    dashboard_conn = _connect_readonly(resolved_dashboard)
    paper_conn = _connect_readonly(resolved_paper)
    try:
        fixtures = load_slate_fixtures(dashboard_conn)
        market_conn = paper_conn if paper_conn is not None and _has_table(paper_conn, "market_snapshots") else dashboard_conn
        if market_conn is not None and _has_table(market_conn, "market_snapshots"):
            coverage = build_market_evidence_coverage(
                conn=market_conn,
                fixtures=fixtures,
                now_utc=resolved_now,
                initialize_schema=False,
            ).to_dict()
        else:
            coverage = {
                "generated_at": resolved_now,
                "collector_health_max_age_minutes": None,
                "fixtures": [],
                "summary": {
                    "fixtures": len(fixtures),
                    "asian_handicap": {},
                    "corner_total": {},
                    "identity_unresolved_count": 0,
                    "missing_by_competition": {},
                },
                "error": "market_snapshots table not present in operational databases",
            }
        budget_audit = (
            audit_provider_request_accounting(dashboard_conn)
            if dashboard_conn is not None
            else {"error": "dashboard database not found"}
        )
    finally:
        if dashboard_conn is not None:
            dashboard_conn.close()
        if paper_conn is not None:
            paper_conn.close()

    return {
        "generated_at": resolved_now,
        "read_only": True,
        "provider_requests_consumed": 0,
        "dashboard_db_path": str(resolved_dashboard),
        "paper_db_path": str(resolved_paper),
        "slate_fixtures": len(fixtures),
        "coverage": coverage,
        "provider_request_accounting": budget_audit,
        "provider_safety_limits": documented_provider_limits(),
        # Decision freshness is provider-specific; collector health is separate.
        "actionable_freshness_minutes_by_provider": {
            "api-football": resolve_actionable_max_age_minutes("api-football"),
            "5dollar-football-api": resolve_actionable_max_age_minutes("5dollar-football-api"),
        },
        "collector_health_max_age_minutes": resolve_collector_health_max_age_minutes(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-db", default=None)
    parser.add_argument("--paper-db", default=None)
    parser.add_argument("--now", default=None, help="Override 'now' (ISO-8601 UTC) for reproducibility")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = run_diagnostics(
        dashboard_db_path=args.dashboard_db,
        paper_db_path=args.paper_db,
        now_utc=args.now,
    )
    print(json.dumps(report, indent=2 if args.pretty else None, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
