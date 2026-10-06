"""Read-only reconciliation manifest for stale dashboard fixture result-status.

Bridges the frozen stale cohort (open AH model picks whose fixture has a stored
score but a non-final dashboard status) to authoritative final results in the
persisted research store, using the deterministic
``football_research_event_identity_v1`` normalization.

This pass is ZERO-WRITE. It produces the 24-row identity manifest for review;
alias persistence and status repair are deliberate follow-ups after review.

Usage::

    python -m sports_ai.dashboard.football_stale_result_reconciliation --dry-run
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .football_research_identity import (
    IDENTITY_AMBIGUOUS,
    IDENTITY_MATCHED,
    RESEARCH_EVENT_IDENTITY_VERSION,
    RESEARCH_SOURCE_TIMEZONE,
    SCORE_CONFLICT,
    SCORE_MATCH,
    dashboard_fixture_identity,
    index_research_events,
    resolve_identity,
    validate_score,
)

FINISHED_STATUSES = frozenset(
    {"FINISHED", "FT", "AET", "PEN", "FINISHED_AFTER_EXTRA_TIME", "FINISHED_AFTER_PENALTIES"}
)
RECONCILIATION_VERSION = "football_fixture_result_reconciliation_v1"


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def open_stale_cohort(ledger_conn: sqlite3.Connection, dashboard_conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Frozen cohort: open AH model picks, kickoff passed, dashboard score present."""
    fixtures = {
        str(row["fixture_id"]): dict(row)
        for row in dashboard_conn.execute(
            "SELECT fixture_id, competition_id, competition_name, home_team_id, home_team_name, "
            "away_team_id, away_team_name, kickoff_utc, status, home_score, away_score FROM dashboard_fixtures"
        ).fetchall()
    }
    now = datetime.now(timezone.utc)
    cohort: list[dict[str, Any]] = []
    for row in ledger_conn.execute(
        "SELECT model_pick_id, fixture_id, line, selection, kickoff_utc FROM football_model_pick_entries "
        "WHERE market='asian_handicap' AND (trade_state IS NULL OR trade_state='')"
    ).fetchall():
        fixture = fixtures.get(str(row["fixture_id"]))
        if fixture is None:
            continue
        kickoff = _parse(row["kickoff_utc"])
        if kickoff is None or kickoff > now:
            continue
        if fixture["home_score"] is None or fixture["away_score"] is None:
            continue
        cohort.append({"pick": dict(row), "fixture": fixture})
    cohort.sort(key=lambda item: str(item["fixture"]["fixture_id"]))
    return cohort


def _provider_truth(dashboard_conn: sqlite3.Connection, fixture_id: str) -> dict[str, Any]:
    """Persisted authoritative provider final truth for a fixture, if any."""
    truth: dict[str, Any] = {"provider_result_row": False, "provider_finished_score": None}
    try:
        row = dashboard_conn.execute(
            "SELECT home_corners, away_corners, total_corners FROM dashboard_fixture_results WHERE fixture_id = ?",
            (fixture_id,),
        ).fetchone()
        if row is not None:
            truth["provider_result_row"] = True
    except sqlite3.OperationalError:
        pass
    return truth


def build_manifest(
    dashboard_conn: sqlite3.Connection,
    ledger_conn: sqlite3.Connection,
    research_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    research_index = index_research_events(research_events)
    cohort = open_stale_cohort(ledger_conn, dashboard_conn)

    rows: list[dict[str, Any]] = []
    matched = ambiguous = unmatched = 0
    final_proven = score_agreeing = score_conflicts = eligible = 0
    for item in cohort:
        fixture = item["fixture"]
        dash_identity = dashboard_fixture_identity(fixture)
        state, candidates = resolve_identity(dash_identity, research_index)
        event = candidates[0] if state == IDENTITY_MATCHED and candidates else None
        score_validation = None
        repair_eligible = "NO"
        research_score = None
        research_final = "UNKNOWN"
        if state == IDENTITY_MATCHED:
            matched += 1
            research_score = [event.get("home_goals"), event.get("away_goals")]
            research_final = "FINAL" if event.get("home_goals") is not None and event.get("away_goals") is not None else "UNKNOWN"
            if research_final == "FINAL":
                final_proven += 1
            score_validation = validate_score((fixture["home_score"], fixture["away_score"]), event)
            if score_validation == SCORE_MATCH:
                score_agreeing += 1
            elif score_validation == SCORE_CONFLICT:
                score_conflicts += 1
            if (
                research_final == "FINAL"
                and score_validation == SCORE_MATCH
                and str(fixture["status"]).upper() not in FINISHED_STATUSES
            ):
                repair_eligible = "YES"
                eligible += 1
        elif state == IDENTITY_AMBIGUOUS:
            ambiguous += 1
        else:
            unmatched += 1

        rows.append(
            {
                "dashboard_fixture_id": fixture["fixture_id"],
                "competition": fixture["competition_id"],
                "raw_dashboard_home": fixture["home_team_name"],
                "raw_dashboard_away": fixture["away_team_name"],
                "dashboard_kickoff_utc": fixture["kickoff_utc"],
                "dashboard_status": fixture["status"],
                "dashboard_score": [fixture["home_score"], fixture["away_score"]],
                "research_event_id": event.get("research_event_id") if event else None,
                "raw_research_home": event.get("raw_home_team") if event else None,
                "raw_research_away": event.get("raw_away_team") if event else None,
                "raw_research_kickoff": event.get("raw_kickoff") if event else None,
                "research_timezone_assumption": RESEARCH_SOURCE_TIMEZONE,
                "normalized_research_kickoff_utc": event.get("normalized_kickoff_utc") if event else None,
                "canonical_dashboard_home": dash_identity["canonical_home_team"],
                "canonical_dashboard_away": dash_identity["canonical_away_team"],
                "canonical_research_home": event.get("canonical_home_team") if event else None,
                "canonical_research_away": event.get("canonical_away_team") if event else None,
                "identity_status": state,
                "identity_candidate_count": len(candidates),
                "research_final_status": research_final,
                "research_score": research_score,
                "score_validation": score_validation,
                "provider_truth": _provider_truth(dashboard_conn, str(fixture["fixture_id"])),
                "repair_eligible": repair_eligible,
            }
        )

    return {
        "reconciliation_version": RECONCILIATION_VERSION,
        "identity_normalization_version": RESEARCH_EVENT_IDENTITY_VERSION,
        "dry_run": True,
        "summary": {
            "cohort": len(cohort),
            "matched": matched,
            "ambiguous": ambiguous,
            "unmatched": unmatched,
            "final_results_proven": final_proven,
            "score_agreeing": score_agreeing,
            "score_conflicts": score_conflicts,
            "eligible_status_repairs": eligible,
        },
        "rows": rows,
    }


def _resolve_paths(args: argparse.Namespace) -> tuple[str, str, str]:
    from .services import DashboardConfig

    config = DashboardConfig.default()
    dashboard_db = args.dashboard_db or str(config.dashboard_db_path)
    ledger_db = args.ledger_db or str(
        config.model_pick_db_path or (config.operational_data_dir / "football_model_picks.sqlite")
    )
    research_db = args.research_db
    if not research_db:
        live = config.operational_data_dir / "football_research_live.sqlite"
        research_db = str(live if live.exists() else config.research_db_path)
    return dashboard_db, ledger_db, research_db


def _open_readonly(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_research_events(research_conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        return [
            dict(row)
            for row in research_conn.execute(
                "SELECT event_id, competition_id, match_date, kickoff_time, home_team, away_team, "
                "home_goals, away_goals, result_code FROM football_events"
            ).fetchall()
        ]
    except sqlite3.OperationalError:
        return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sports_ai.dashboard.football_stale_result_reconciliation",
        description="Zero-write stale result-status identity manifest.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dashboard-db", default=None)
    parser.add_argument("--ledger-db", default=None)
    parser.add_argument("--research-db", default=None)
    args = parser.parse_args(argv)
    args.dry_run = True  # this pass is dry-run only

    dashboard_db, ledger_db, research_db = _resolve_paths(args)
    dash = _open_readonly(dashboard_db)
    led = _open_readonly(ledger_db)
    res = _open_readonly(research_db)
    try:
        manifest = build_manifest(dash, led, _load_research_events(res))
    finally:
        res.close()
        led.close()
        dash.close()
    print(json.dumps(manifest, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
