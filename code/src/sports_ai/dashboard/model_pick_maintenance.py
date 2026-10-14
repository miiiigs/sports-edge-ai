"""Controlled one-shot maintenance for the Football MODEL PICK ledger.

This is an observational, evidence-only command. It consumes ALREADY-PERSISTED
production evidence and NEVER:

* fetches bookmaker odds (no The Odds API), never invokes 5Dollar;
* mutates ``paper_trade_entries`` / ``paper_trade_updates`` (separate DB);
* runs a historical model replay or writes ``CURRENT_SYSTEM_REPLAY`` rows;
* moves the prospective boundary, triggers a fixture refresh, or alters Manager
  decisions.

Usage::

    python -m sports_ai.dashboard.model_pick_maintenance --dry-run
    python -m sports_ai.dashboard.model_pick_maintenance --apply-live-issued
    python -m sports_ai.dashboard.model_pick_maintenance --settle
    python -m sports_ai.dashboard.model_pick_maintenance --apply-live-issued --settle
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..locking import canonical_hash
from ..storage import football_model_pick_ledger as ledger
from .model_pick_reconciliation import build_live_issued_ledger

# Dashboard fixture statuses considered final (sporting result usable).
_FINISHED_STATUSES = frozenset(
    {"FINISHED", "FT", "AET", "PEN", "FINISHED_AFTER_EXTRA_TIME", "FINISHED_AFTER_PENALTIES"}
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def manifest_hash(proposed_picks: list[dict[str, Any]]) -> str:
    """Deterministic hash of the proposed immutable rows (order-independent)."""
    rows = [{field: pick.get(field) for field in ledger._IMMUTABLE_FIELDS} for pick in proposed_picks]
    rows.sort(key=lambda row: (str(row.get("fixture_id")), str(row.get("market"))))
    return canonical_hash({"policy_rows": rows})


def results_from_dashboard(dashboard_conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Sporting results from persisted dashboard truth (finished fixtures only).

    AH uses the persisted final score; corners use the persisted final total-corner
    statistic. Incomplete results are omitted so picks stay OPEN (never inferred).
    """
    results: dict[str, dict[str, Any]] = {}
    for row in dashboard_conn.execute(
        "SELECT fixture_id, status, home_score, away_score FROM dashboard_fixtures"
    ).fetchall():
        status = str(row["status"] or "").upper()
        if status not in _FINISHED_STATUSES:
            continue
        if row["home_score"] is None or row["away_score"] is None:
            continue
        results[str(row["fixture_id"])] = {
            "home_goals": row["home_score"],
            "away_goals": row["away_score"],
        }
    try:
        corner_rows = dashboard_conn.execute(
            "SELECT fixture_id, home_corners, away_corners, total_corners FROM dashboard_fixture_results"
        ).fetchall()
    except sqlite3.OperationalError:
        return results
    for row in corner_rows:
        fixture_id = str(row["fixture_id"])
        if fixture_id not in results:
            continue
        total = row["total_corners"]
        if total is None and row["home_corners"] is not None and row["away_corners"] is not None:
            total = int(row["home_corners"]) + int(row["away_corners"])
        if total is not None:
            results[fixture_id]["total_corners"] = total
    return results


def _summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "manifest_hash": manifest_hash(manifest["proposed_picks"]),
        "proposed_rows": len(manifest["proposed_picks"]),
        "ah": manifest["proposed_ah_model_picks"],
        "corners": manifest["proposed_corner_model_picks"],
        "source_prediction_count": manifest["predictions"],
        "ready_predictions": manifest["ready_predictions"],
        "blocked_predictions": manifest["blocked_predictions"],
        "kickoff_passed_not_issued": manifest.get("kickoff_passed_not_issued", 0),
    }


def run_dry_run(dashboard_conn: sqlite3.Connection, *, now: str | None = None) -> dict[str, Any]:
    manifest = build_live_issued_ledger(dashboard_conn, dry_run=True, now=now)
    summary = _summary(manifest)
    summary["dry_run"] = True
    return summary


def apply_live_issued(
    dashboard_conn: sqlite3.Connection,
    ledger_conn: sqlite3.Connection,
    *,
    expected_manifest_hash: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    issued_at = now or _utc_now()
    manifest = build_live_issued_ledger(dashboard_conn, dry_run=True, now=issued_at)
    proposed = manifest["proposed_picks"]
    digest = manifest_hash(proposed)
    if expected_manifest_hash is not None and expected_manifest_hash != digest:
        raise RuntimeError(f"MANIFEST_HASH_MISMATCH: expected={expected_manifest_hash} apply={digest}")
    ledger.ensure_schema(ledger_conn)
    ledger_conn.commit()
    before = int(ledger_conn.execute("SELECT COUNT(*) FROM football_model_pick_entries").fetchone()[0])
    canonical_duplicates: list[dict[str, Any]] = []
    # One explicit transaction under the write lock: any immutable conflict rolls
    # back the whole apply, and a concurrent scheduler cycle cannot interleave.
    with ledger_conn:
        ledger.begin_immediate(ledger_conn)
        for pick in proposed:
            canonical = pick.get("canonical_fixture_id")
            if canonical:
                # Never a second pick for the same match under another provider id.
                holder = ledger.canonical_pick_holder(
                    ledger_conn,
                    policy_id=str(pick["policy_id"]),
                    canonical_fixture_id=str(canonical),
                    market=str(pick["market"]),
                    fixture_id=str(pick["fixture_id"]),
                )
                if holder is not None:
                    canonical_duplicates.append(
                        {
                            "canonical_fixture_id": canonical,
                            "market": pick["market"],
                            "issued_fixture_id": holder,
                            "skipped_fixture_id": str(pick["fixture_id"]),
                        }
                    )
                    continue
            ledger.record_model_pick(ledger_conn, pick, now=issued_at, initialize_schema=False)
    after = int(ledger_conn.execute("SELECT COUNT(*) FROM football_model_pick_entries").fetchone()[0])
    return {
        **_summary(manifest),
        "dry_run": False,
        "rows_written": after - before,
        "canonical_duplicates_skipped": canonical_duplicates,
    }


def settle_open(
    ledger_conn: sqlite3.Connection,
    dashboard_conn: sqlite3.Connection,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    results = results_from_dashboard(dashboard_conn)
    ledger.ensure_schema(ledger_conn)
    with ledger_conn:
        settled = ledger.settle_rows(ledger_conn, results, now=now, initialize_schema=False)
    return {"settled": settled, "results_available": len(results)}


def _resolve_paths(args: argparse.Namespace) -> tuple[str, str]:
    from .services import DashboardConfig

    config = DashboardConfig.default()
    dashboard_db = args.dashboard_db or str(config.dashboard_db_path)
    ledger_db = args.ledger_db or str(
        config.model_pick_db_path or (config.operational_data_dir / "football_model_picks.sqlite")
    )
    return dashboard_db, ledger_db


def _open_readonly(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sports_ai.dashboard.model_pick_maintenance",
        description="Model-pick ledger maintenance (observational, evidence-only).",
    )
    parser.add_argument("--dry-run", action="store_true", help="produce a manifest; zero writes")
    parser.add_argument(
        "--apply-live-issued",
        action="store_true",
        help="write LIVE_ISSUED picks for fixtures that have not kicked off yet (idempotent)",
    )
    parser.add_argument("--settle", action="store_true", help="settle open picks from persisted results")
    parser.add_argument("--dashboard-db", default=None)
    parser.add_argument("--ledger-db", default=None)
    parser.add_argument("--expected-manifest-hash", default=None)
    args = parser.parse_args(argv)

    if not (args.dry_run or args.apply_live_issued or args.settle):
        args.dry_run = True

    dashboard_db, ledger_db = _resolve_paths(args)
    outputs: dict[str, Any] = {}

    if args.dry_run:
        dash = _open_readonly(dashboard_db)
        try:
            outputs["dry_run"] = run_dry_run(dash)
        finally:
            dash.close()

    if args.apply_live_issued:
        dash = _open_readonly(dashboard_db)
        led = ledger.connect(ledger_db)
        try:
            outputs["apply"] = apply_live_issued(
                dash, led, expected_manifest_hash=args.expected_manifest_hash
            )
        finally:
            led.close()
            dash.close()

    if args.settle:
        dash = _open_readonly(dashboard_db)
        led = ledger.connect(ledger_db)
        try:
            outputs["settle"] = settle_open(led, dash)
        finally:
            led.close()
            dash.close()

    print(json.dumps(outputs, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
