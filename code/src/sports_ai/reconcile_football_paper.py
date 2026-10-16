"""Deterministic cross-provider paper reconciliation CLI.

Usage:
    python -m sports_ai.reconcile_football_paper --paper-db <path> --dashboard-db <path> --dry-run
    python -m sports_ai.reconcile_football_paper --paper-db <path> --dashboard-db <path> --apply

DRY RUN is the default and performs no mutation. APPLY validates all safety
invariants and then records additive `paper_position_reconciliations` rows;
original paper rows, entry odds, and settlement trails are never modified.

No provider HTTP calls are made by this tool.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sports_ai.dashboard import storage as dashboard_storage
from sports_ai.dashboard.paper_market_evidence import partition_counted_paper_rows
from sports_ai.storage import football_canonical_fixture, football_paper_trading


def _build_fixture_identity(
    paper_db: str | Path,
    dashboard_db: str | Path,
    fixture_json: str | None,
) -> dict[str, dict[str, Any]]:
    if fixture_json:
        with open(fixture_json, "r", encoding="utf-8") as handle:
            return json.load(handle)

    with football_paper_trading.connect(paper_db) as paper_conn:
        rows = football_paper_trading.fetch_paper_trade_rows(paper_conn)
    fixture_ids = sorted({str(row["fixture_id"]) for row in rows if row.get("fixture_id") not in {None, ""}})
    identity: dict[str, dict[str, Any]] = {}
    with dashboard_storage.connect(dashboard_db) as conn:
        for fixture_id in fixture_ids:
            fixture = dashboard_storage.fetch_fixture(conn, fixture_id)
            if fixture is not None:
                identity[fixture_id] = {
                    "fixture_id": fixture_id,
                    "competition_id": fixture.get("competition_id"),
                    "home_team_name": fixture.get("home_team_name"),
                    "away_team_name": fixture.get("away_team_name"),
                    "kickoff_utc": fixture.get("kickoff_utc"),
                    "season": fixture.get("season"),
                }
    missing = [fixture_id for fixture_id in fixture_ids if fixture_id not in identity]
    if missing:
        with football_paper_trading.connect(paper_db) as paper_conn:
            for fixture_id in missing:
                row = paper_conn.execute(
                    "SELECT competition_id, home_team_name, away_team_name, kickoff_utc "
                    "FROM market_snapshots WHERE fixture_id = ? LIMIT 1",
                    (fixture_id,),
                ).fetchone()
                if row is not None:
                    identity[fixture_id] = {
                        "fixture_id": fixture_id,
                        "competition_id": row["competition_id"],
                        "home_team_name": row["home_team_name"],
                        "away_team_name": row["away_team_name"],
                        "kickoff_utc": row["kickoff_utc"],
                        "season": None,
                    }
    return identity


def _print_report(paper_rows: list[dict[str, Any]], identity: dict[str, dict[str, Any]], tolerance: int) -> None:
    plan = football_canonical_fixture.build_reconciliation_plan(
        paper_rows, identity, kickoff_tolerance_seconds=tolerance
    )
    # Duplicate detection needs every row; P/L counts only trusted evidence.
    before_counted, before_quarantined = partition_counted_paper_rows(paper_rows, set())
    after_counted, after_quarantined = partition_counted_paper_rows(plan.counted_rows(paper_rows), set())
    before = football_canonical_fixture.summarize_performance(before_counted)
    after = football_canonical_fixture.summarize_performance(after_counted)
    print("=" * 64)
    print("CANONICAL FIXTURE RECONCILIATION REPORT (dry-run projection)")
    print("=" * 64)
    print(f"kickoff_tolerance_seconds: {tolerance}")
    print(f"total paper rows:            {plan.total_rows}")
    print(f"canonical fixtures:          {plan.canonical_fixture_count}")
    print(f"cross-provider fixture pairs:{plan.cross_provider_fixture_groups}")
    print(f"duplicate business groups:   {len(plan.duplicate_groups)}")
    print(f"duplicate representations:   {plan.duplicate_representation_count}")
    print(f"ambiguous fixtures:          {len(plan.ambiguous_fixture_ids)} {list(plan.ambiguous_fixture_ids)}")
    print(f"unresolved fixtures:         {len(plan.unresolved_fixture_ids)}")
    print(f"settlement conflicts:        {len(plan.conflicts)}")
    print(f"settlement inconsistencies:  {len(plan.inconsistencies)}")
    print(f"quarantined (excluded from P/L): before={len(before_quarantined)} after={len(after_quarantined)}")
    print("-" * 64)
    print(f"BEFORE  total={before['total']} open={before['open']} settled={before['settled']} "
          f"W={before['won']} L={before['lost']} P={before['push']} HW={before['half_win']} HL={before['half_loss']} "
          f"V={before['void']} P/L={before['pl']} ROI={before['roi']} SR={before['strike_rate']}")
    print(f"AFTER   total={after['total']} open={after['open']} settled={after['settled']} "
          f"W={after['won']} L={after['lost']} P={after['push']} HW={after['half_win']} HL={after['half_loss']} "
          f"V={after['void']} P/L={after['pl']} ROI={after['roi']} SR={after['strike_rate']}")
    for group in sorted(plan.duplicate_groups, key=lambda g: str(g.canonical_row.get("created_at") or "")):
        print(f"  dup group: {group.market} {group.selection} line={group.line} | "
              f"kept={group.canonical_row.get('paper_trade_id')} ({group.canonical_row.get('fixture_id')}) | "
              f"dups={[r.get('paper_trade_id') for r in group.duplicate_rows]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-provider paper reconciliation")
    parser.add_argument("--paper-db", required=True, help="Path to football_paper_trading.sqlite")
    parser.add_argument("--dashboard-db", default=None, help="Path to football_dashboard.sqlite (for fixture identity)")
    parser.add_argument("--fixture-json", default=None, help="Saved fixture identity JSON map (alternative to --dashboard-db)")
    parser.add_argument("--kickoff-tolerance", type=int, default=football_canonical_fixture.DEFAULT_KICKOFF_TOLERANCE_SECONDS)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="Report only (default, non-mutating)")
    group.add_argument("--apply", action="store_true", help="Validate invariants and apply additive reconciliation")
    args = parser.parse_args()

    if args.dashboard_db is None and args.fixture_json is None:
        parser.error("provide --dashboard-db or --fixture-json")
    if args.apply and args.fixture_json:
        parser.error("--apply requires a live --dashboard-db; --fixture-json is read-only dry-run only")

    identity = _build_fixture_identity(args.paper_db, args.dashboard_db or "", args.fixture_json)
    with football_paper_trading.connect(args.paper_db) as paper_conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(paper_conn)

        if args.apply:
            result = football_canonical_fixture.apply_reconciliation(
                paper_conn, identity, kickoff_tolerance_seconds=args.kickoff_tolerance
            )
            if result.violations:
                print("APPLY BLOCKED - invariants violated:")
                for violation in result.violations:
                    print(f"  {violation}")
                return 2
            print("APPLY OK")
            print(f"  reconciliation records written: {result.records_written}")
            print(f"  existing excluded positions:   {result.existing_excluded}")
            _print_report(paper_rows, identity, args.kickoff_tolerance)
        else:
            _print_report(paper_rows, identity, args.kickoff_tolerance)
            print("Dry run only - no production mutation performed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
