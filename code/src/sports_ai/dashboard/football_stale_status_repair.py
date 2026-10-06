"""Approved-cohort stale dashboard status repair (evidence-only, audited).

Repairs ``dashboard_fixtures.status`` ONLY for the reviewed deterministic cohort
(open AH model picks whose research final result is proven and whose dashboard
score already agrees). It never overwrites a differing score, never writes model
picks, never touches the paper/value-bet ledger, and never calls any provider.

Identity evidence is persisted through the existing canonical fixture/alias
infrastructure (``fixture_provider_aliases`` / ``canonical_fixtures``) using
``matching_method = football_research_event_identity_v1``.

Usage::

    python -m sports_ai.dashboard.football_stale_status_repair --dry-run
    python -m sports_ai.dashboard.football_stale_status_repair --apply --expected-manifest-hash <hash>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..locking import canonical_hash
from .football_research_identity import (
    RESEARCH_SOURCE_PROVIDER,
    dashboard_fixture_identity,
)
from .football_stale_result_reconciliation import (
    _load_research_events,
    _open_readonly,
    _resolve_paths,
    build_manifest,
)

REPAIR_IDENTITY_VERSION = "football_research_event_identity_v1"
RECONCILIATION_VERSION = "football_fixture_result_reconciliation_v1"
FINAL_DASHBOARD_STATUS = "FINISHED"
REPAIR_ORIGIN = "LIVE_ISSUED"

AUDIT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS football_fixture_result_reconciliations (
        reconciliation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        fixture_id TEXT NOT NULL,
        old_status TEXT,
        new_status TEXT NOT NULL,
        home_score INTEGER,
        away_score INTEGER,
        research_event_id TEXT,
        canonical_fixture_id TEXT,
        identity_version TEXT NOT NULL,
        reconciled_at TEXT NOT NULL,
        UNIQUE(fixture_id, identity_version)
    )
    """,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repair_hash_payload(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "fixture_id", "old_status", "new_status", "home_score", "away_score",
        "research_event_id", "canonical_fixture_id",
    )
    rows = [{k: row.get(k) for k in fields} for row in plan]
    rows.sort(key=lambda row: str(row["fixture_id"]))
    return rows


def manifest_hash(plan: list[dict[str, Any]]) -> str:
    return canonical_hash({"repairs": _repair_hash_payload(plan)})


def build_repair_plan(
    dashboard_conn: sqlite3.Connection,
    ledger_conn: sqlite3.Connection,
    research_events: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest = build_manifest(dashboard_conn, ledger_conn, research_events)
    plan: list[dict[str, Any]] = []
    for row in manifest["rows"]:
        if row["repair_eligible"] != "YES":
            continue
        plan.append(
            {
                "fixture_id": row["dashboard_fixture_id"],
                "competition_id": row["competition"],
                "old_status": row["dashboard_status"],
                "new_status": FINAL_DASHBOARD_STATUS,
                "home_score": row["dashboard_score"][0],
                "away_score": row["dashboard_score"][1],
                "home_team_name": row["raw_dashboard_home"],
                "away_team_name": row["raw_dashboard_away"],
                "kickoff_utc": row["dashboard_kickoff_utc"],
                "research_event_id": row["research_event_id"],
                "research_home_team": row["canonical_research_home"],
                "research_away_team": row["canonical_research_away"],
                "research_kickoff_utc": row["normalized_research_kickoff_utc"],
                "canonical_fixture_id": row["canonical_fixture_id"],
            }
        )
    return {
        "reconciliation_version": RECONCILIATION_VERSION,
        "identity_version": REPAIR_IDENTITY_VERSION,
        "final_status": FINAL_DASHBOARD_STATUS,
        "plan": plan,
        "manifest_hash": manifest_hash(plan),
        "source_summary": manifest["summary"],
    }


def persist_identity_evidence(paper_conn: sqlite3.Connection, plan: list[dict[str, Any]]) -> int:
    """Persist deterministic identity aliases via the existing canonical infra."""
    from ..storage.football_canonical_fixture import (
        ensure_schema,
        resolve_or_create_canonical_fixture,
    )

    ensure_schema(paper_conn)
    recorded = 0
    for row in plan:
        canonical, _ = resolve_or_create_canonical_fixture(
            paper_conn,
            provider=RESEARCH_SOURCE_PROVIDER,
            provider_fixture_id=str(row["research_event_id"]),
            competition_id=str(row["competition_id"]),
            home_team_name=str(row["research_home_team"]),
            away_team_name=str(row["research_away_team"]),
            kickoff_utc=str(row["research_kickoff_utc"]),
            initialize_schema=False,
        )
        provider, _, provider_fixture_id = str(row["fixture_id"]).partition(":")
        resolve_or_create_canonical_fixture(
            paper_conn,
            provider=provider,
            provider_fixture_id=provider_fixture_id,
            competition_id=str(row["competition_id"]),
            home_team_name=str(row["home_team_name"]),
            away_team_name=str(row["away_team_name"]),
            kickoff_utc=str(row["kickoff_utc"]),
            initialize_schema=False,
        )
        if canonical != row["canonical_fixture_id"]:
            raise sqlite3.IntegrityError(
                f"CANONICAL_IDENTITY_MISMATCH: {row['fixture_id']} expected={row['canonical_fixture_id']} got={canonical}"
            )
        recorded += 1
    return recorded


def _ensure_audit_schema(conn: sqlite3.Connection) -> None:
    for statement in AUDIT_SCHEMA:
        conn.execute(statement)


def apply_repairs(
    dashboard_conn: sqlite3.Connection,
    plan: list[dict[str, Any]],
    *,
    expected_manifest_hash: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """One transaction. Any precondition drift/hash mismatch rolls the whole apply back."""
    digest = manifest_hash(plan)
    if expected_manifest_hash is not None and expected_manifest_hash != digest:
        raise RuntimeError(f"MANIFEST_HASH_MISMATCH: expected={expected_manifest_hash} apply={digest}")
    _ensure_audit_schema(dashboard_conn)
    timestamp = now or _utc_now()
    updated = 0
    new_status = FINAL_DASHBOARD_STATUS
    with dashboard_conn:  # single transaction; rollback on any exception
        for row in plan:
            fixture_id = str(row["fixture_id"])
            current = dashboard_conn.execute(
                "SELECT status, home_score, away_score, competition_id, home_team_name, away_team_name, kickoff_utc "
                "FROM dashboard_fixtures WHERE fixture_id = ?",
                (fixture_id,),
            ).fetchone()
            if current is None:
                raise sqlite3.IntegrityError(f"REPAIR_PRECONDITION_FAILED: {fixture_id} missing")
            if str(current["status"]) != str(row["old_status"]):
                raise sqlite3.IntegrityError(
                    f"REPAIR_PRECONDITION_FAILED: {fixture_id} status changed {current['status']!r} != {row['old_status']!r}"
                )
            if current["home_score"] != row["home_score"] or current["away_score"] != row["away_score"]:
                raise sqlite3.IntegrityError(f"REPAIR_PRECONDITION_FAILED: {fixture_id} score changed")
            identity = dashboard_fixture_identity(dict(current, fixture_id=fixture_id))
            if identity["canonical_fixture_id"] != row["canonical_fixture_id"]:
                raise sqlite3.IntegrityError(f"REPAIR_PRECONDITION_FAILED: {fixture_id} identity drift")
            dashboard_conn.execute(
                "UPDATE dashboard_fixtures SET status = ? WHERE fixture_id = ? AND status = ?",
                (new_status, fixture_id, row["old_status"]),
            )
            dashboard_conn.execute(
                "INSERT OR IGNORE INTO football_fixture_result_reconciliations "
                "(fixture_id, old_status, new_status, home_score, away_score, research_event_id, "
                " canonical_fixture_id, identity_version, reconciled_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    fixture_id, row["old_status"], new_status, row["home_score"], row["away_score"],
                    row["research_event_id"], row["canonical_fixture_id"], REPAIR_IDENTITY_VERSION, timestamp,
                ),
            )
            updated += 1
    return {"manifest_hash": digest, "applied": updated, "final_status": new_status}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sports_ai.dashboard.football_stale_status_repair",
        description="Approved-cohort stale dashboard status repair (audited, evidence-only).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-manifest-hash", default=None)
    parser.add_argument("--dashboard-db", default=None)
    parser.add_argument("--ledger-db", default=None)
    parser.add_argument("--research-db", default=None)
    parser.add_argument("--paper-db", default=None)
    args = parser.parse_args(argv)
    if not (args.dry_run or args.apply):
        args.dry_run = True

    from .services import DashboardConfig

    config = DashboardConfig.default()
    dashboard_db, ledger_db, research_db = _resolve_paths(args)
    paper_db = args.paper_db or str(config.paper_trading_db_path)

    res = _open_readonly(research_db)
    research_events = _load_research_events(res)
    res.close()

    if args.dry_run:
        dash = _open_readonly(dashboard_db)
        led = _open_readonly(ledger_db)
        try:
            plan_result = build_repair_plan(dash, led, research_events)
        finally:
            led.close()
            dash.close()
        plan_result["dry_run"] = True
        print(json.dumps(plan_result, indent=1, sort_keys=True))
        return 0

    # apply
    dash = sqlite3.connect(dashboard_db)
    dash.row_factory = sqlite3.Row
    led = _open_readonly(ledger_db)
    paper = sqlite3.connect(paper_db)
    try:
        plan_result = build_repair_plan(dash, led, research_events)
        plan = plan_result["plan"]
        if not plan:
            raise RuntimeError("EMPTY_REPAIR_PLAN")
        aliases = persist_identity_evidence(paper, plan)
        paper.commit()
        result = apply_repairs(dash, plan, expected_manifest_hash=args.expected_manifest_hash)
        result["identity_aliases_persisted"] = aliases
        result["dry_run"] = False
    finally:
        paper.close()
        led.close()
        dash.close()
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
