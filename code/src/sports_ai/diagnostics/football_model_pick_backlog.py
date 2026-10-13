"""Read-only Football Model Pick settlement-backlog + duplicate-identity diagnostic.

Two pure, read-only reports over the Model Pick ledger and the dashboard
sporting-truth stores:

* :func:`canonical_duplicate_report` lists canonical duplicates — the same
  ``canonical_fixture_id`` + ``market`` recorded under more than one
  ``fixture_id`` (typically a football-data.org id and an API-Football id for the
  same match) — with both provider ids, each row's settlement state and whether
  the persisted sporting truth for the two ids disagrees;
* :func:`classify_open_picks` classifies OPEN ``LIVE_ISSUED`` picks whose kickoff
  is in the past into explicit backlog causes.

Safety invariants:

* every SQLite connection is opened ``mode=ro`` with ``PRAGMA query_only = ON``;
* only SELECT statements are issued; nothing is merged, deleted or settled;
* no provider client is instantiated or called.

CLI::

    python -m sports_ai.diagnostics.football_model_pick_backlog \\
        --model-pick-db football_model_picks.sqlite \\
        --dashboard-db football_dashboard.sqlite
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..dashboard.result_sync import (
    RECENT_RESULT_LOOKBACK_HOURS,
    is_pollable_api_football_fixture_id,
)

RECORD_ORIGIN_LIVE_ISSUED = "LIVE_ISSUED"
DEFAULT_SUPPORTED_COMPETITIONS = ("ENG1", "ENG2", "ENG3", "ENG4", "ESP1")
_SETTLED_STATES = frozenset({"FULL_WIN", "HALF_WIN", "PUSH", "HALF_LOSS", "FULL_LOSS", "WON", "LOST"})

CLASS_GENUINE_UNRESOLVED = "GENUINE_UNRESOLVED_POSTPONED_OR_CANCELLED"
CLASS_FIXTURE_ROW_MISSING = "OTHER_FIXTURE_ROW_MISSING"
CLASS_UNSUPPORTED_COMPETITION = "UNSUPPORTED_COMPETITION"
CLASS_MISSING_FINAL_SCORE = "MISSING_FINAL_RESULT_SCORE"
CLASS_MISSING_CORNER_TRUTH = "MISSING_FINAL_RESULT_CORNERS"
CLASS_SETTLEABLE_NOW = "OTHER_SETTLEABLE_NOW"
CLASS_PROVIDER_MISMATCH = "PROVIDER_FIXTURE_MISMATCH"
CLASS_RESULT_SYNC_BACKLOG = "RESULT_SYNC_BACKLOG_OUTSIDE_LOOKBACK"
CLASS_RECENT_PENDING = "OTHER_RECENT_PENDING_RESULT_SYNC"

_TERMINAL_UNRESOLVED = frozenset({"POSTPONED", "CANCELLED", "ABANDONED"})


def _readonly_connect(path: str | Path) -> sqlite3.Connection:
    uri = f"file:{Path(path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def provider_of(fixture_id: Any) -> str:
    text = str(fixture_id or "")
    head, sep, _ = text.partition(":")
    return head if sep else "unprefixed"


def _is_settled(row: Mapping[str, Any]) -> bool:
    return str(row.get("trade_state") or "") in _SETTLED_STATES


def load_snapshot(
    *,
    model_pick_db: str | Path,
    dashboard_db: str | Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read model picks, dashboard fixtures and fixture results (read-only)."""
    conn = _readonly_connect(model_pick_db)
    try:
        picks = [dict(r) for r in conn.execute("SELECT * FROM football_model_pick_entries ORDER BY kickoff_utc, fixture_id, market")]
    finally:
        conn.close()
    fixtures: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    if dashboard_db is not None and Path(dashboard_db).exists():
        dconn = _readonly_connect(dashboard_db)
        try:
            if _table_exists(dconn, "dashboard_fixtures"):
                for r in dconn.execute(
                    "SELECT fixture_id, provider, provider_fixture_id, competition_id, kickoff_utc, status, home_score, away_score FROM dashboard_fixtures"
                ):
                    fixtures[str(r["fixture_id"])] = dict(r)
            if _table_exists(dconn, "dashboard_fixture_results"):
                for r in dconn.execute(
                    "SELECT fixture_id, statistics_provider, home_corners, away_corners, total_corners FROM dashboard_fixture_results"
                ):
                    results[str(r["fixture_id"])] = dict(r)
        finally:
            dconn.close()
    return picks, fixtures, results


def _truth(fixture_id: str, fixtures: Mapping[str, Mapping[str, Any]], results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    fixture = fixtures.get(fixture_id) or {}
    result = results.get(fixture_id) or {}
    return {
        "status": fixture.get("status"),
        "home_score": fixture.get("home_score"),
        "away_score": fixture.get("away_score"),
        "total_corners": result.get("total_corners"),
    }


def canonical_duplicate_report(
    picks: Sequence[Mapping[str, Any]],
    fixtures: Mapping[str, Mapping[str, Any]] | None = None,
    results: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Groups of (canonical_fixture_id, market) with >1 distinct fixture_id. Pure."""
    fixtures = fixtures or {}
    results = results or {}
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in picks:
        canonical = str(row.get("canonical_fixture_id") or "").strip()
        if not canonical:
            continue
        groups.setdefault((canonical, str(row.get("market") or "")), []).append(row)
    out: list[dict[str, Any]] = []
    for (canonical, market), rows in sorted(groups.items()):
        if len({str(r.get("fixture_id") or "") for r in rows}) < 2:
            continue
        members = []
        for r in sorted(rows, key=lambda x: str(x.get("fixture_id") or "")):
            fid = str(r.get("fixture_id") or "")
            members.append(
                {
                    "model_pick_id": r.get("model_pick_id"),
                    "fixture_id": fid,
                    "provider": provider_of(fid),
                    "record_origin": r.get("record_origin"),
                    "selection": r.get("selection"),
                    "line": r.get("line"),
                    "kickoff_utc": r.get("kickoff_utc"),
                    "trade_state": r.get("trade_state"),
                    "settled": _is_settled(r),
                    "truth": _truth(fid, fixtures, results),
                }
            )
        states = {m["trade_state"] for m in members}
        all_settled = all(m["settled"] for m in members)
        scores = {
            (m["truth"]["home_score"], m["truth"]["away_score"])
            for m in members
            if m["truth"]["status"] == "FINISHED" and m["truth"]["home_score"] is not None and m["truth"]["away_score"] is not None
        }
        corners = {m["truth"]["total_corners"] for m in members if m["truth"]["total_corners"] is not None}
        out.append(
            {
                "canonical_fixture_id": canonical,
                "market": market,
                "providers": sorted({m["provider"] for m in members}),
                "members": members,
                "settled_count": sum(1 for m in members if m["settled"]),
                "all_settled": all_settled,
                "settled_identically": all_settled and len(states) == 1,
                "settlement_states_disagree": all_settled and len(states) > 1,
                "selection_or_line_differs": len({(m["selection"], m["line"]) for m in members}) > 1,
                "results_disagree": len(scores) > 1 or (market == "corner_total" and len(corners) > 1),
            }
        )
    summary = {
        "duplicate_groups": len(out),
        "duplicate_rows": sum(len(g["members"]) for g in out),
        "all_settled_identically": sum(1 for g in out if g["settled_identically"]),
        "all_settled_disagreeing": sum(1 for g in out if g["settlement_states_disagree"]),
        "partially_settled": sum(1 for g in out if 0 < g["settled_count"] < len(g["members"])),
        "none_settled": sum(1 for g in out if g["settled_count"] == 0),
        "results_disagree": sum(1 for g in out if g["results_disagree"]),
        "selection_or_line_differs": sum(1 for g in out if g["selection_or_line_differs"]),
    }
    return {"summary": summary, "groups": out, "read_only": True}


def classify_open_pick(
    pick: Mapping[str, Any],
    *,
    fixtures: Mapping[str, Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    now_utc: datetime,
    supported_competitions: Iterable[str] = DEFAULT_SUPPORTED_COMPETITIONS,
) -> str:
    fixture_id = str(pick.get("fixture_id") or "")
    fixture = fixtures.get(fixture_id)
    if fixture is None:
        return CLASS_FIXTURE_ROW_MISSING
    status = str(fixture.get("status") or "").strip().upper()
    if status in _TERMINAL_UNRESOLVED:
        return CLASS_GENUINE_UNRESOLVED
    competition = str(fixture.get("competition_id") or pick.get("competition_id") or "")
    if competition not in set(supported_competitions):
        return CLASS_UNSUPPORTED_COMPETITION
    if status == "FINISHED":
        if fixture.get("home_score") is None or fixture.get("away_score") is None:
            return CLASS_MISSING_FINAL_SCORE
        if str(pick.get("market") or "") == "corner_total":
            result = results.get(fixture_id) or {}
            if result.get("home_corners") is None or result.get("away_corners") is None or result.get("total_corners") is None:
                return CLASS_MISSING_CORNER_TRUTH
        return CLASS_SETTLEABLE_NOW
    if not is_pollable_api_football_fixture_id(fixture_id):
        return CLASS_PROVIDER_MISMATCH
    kickoff = _parse(fixture.get("kickoff_utc") or pick.get("kickoff_utc"))
    if kickoff is not None and now_utc - kickoff > timedelta(hours=RECENT_RESULT_LOOKBACK_HOURS):
        return CLASS_RESULT_SYNC_BACKLOG
    return CLASS_RECENT_PENDING


def classify_open_picks(
    picks: Sequence[Mapping[str, Any]],
    fixtures: Mapping[str, Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    *,
    now_utc: datetime,
    past_kickoff_hours: float = 3.0,
    supported_competitions: Iterable[str] = DEFAULT_SUPPORTED_COMPETITIONS,
) -> dict[str, Any]:
    """Classify OPEN LIVE_ISSUED picks with kickoff older than ``past_kickoff_hours``. Pure."""
    duplicates = canonical_duplicate_report(picks, fixtures, results)
    duplicate_fixture_ids = {m["fixture_id"] for g in duplicates["groups"] for m in g["members"]}
    supported = tuple(supported_competitions)
    counts: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_market: dict[str, dict[str, int]] = {}
    by_provider: dict[str, int] = {}
    duplicate_flagged = 0
    examined = 0
    rows: list[dict[str, Any]] = []
    cutoff = now_utc - timedelta(hours=past_kickoff_hours)
    for pick in picks:
        if str(pick.get("record_origin") or "") != RECORD_ORIGIN_LIVE_ISSUED or _is_settled(pick):
            continue
        kickoff = _parse(pick.get("kickoff_utc"))
        if kickoff is None or kickoff > cutoff:
            continue
        examined += 1
        cls = classify_open_pick(pick, fixtures=fixtures, results=results, now_utc=now_utc, supported_competitions=supported)
        counts[cls] = counts.get(cls, 0) + 1
        status = str((fixtures.get(str(pick.get("fixture_id"))) or {}).get("status") or "MISSING")
        by_status[status] = by_status.get(status, 0) + 1
        market = str(pick.get("market") or "")
        by_market.setdefault(market, {})
        by_market[market][cls] = by_market[market].get(cls, 0) + 1
        provider = provider_of(pick.get("fixture_id"))
        by_provider[provider] = by_provider.get(provider, 0) + 1
        is_dup = str(pick.get("fixture_id") or "") in duplicate_fixture_ids
        duplicate_flagged += int(is_dup)
        rows.append({"model_pick_id": pick.get("model_pick_id"), "fixture_id": pick.get("fixture_id"), "market": market, "class": cls, "duplicate_identity": is_dup})
    return {
        "examined": examined,
        "class_counts": dict(sorted(counts.items())),
        "fixture_status_counts": dict(sorted(by_status.items())),
        "market_class_counts": by_market,
        "provider_counts": dict(sorted(by_provider.items())),
        "duplicate_identity_flagged": duplicate_flagged,
        "rows": rows,
        "read_only": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--model-pick-db", required=True)
    parser.add_argument("--dashboard-db", default=None)
    parser.add_argument("--now", default=None, help="ISO timestamp (default: current UTC time)")
    parser.add_argument("--rows", action="store_true", help="include per-pick rows / duplicate members")
    args = parser.parse_args(argv)
    picks, fixtures, results = load_snapshot(model_pick_db=args.model_pick_db, dashboard_db=args.dashboard_db)
    now = _parse(args.now) if args.now else datetime.now(timezone.utc)
    classification = classify_open_picks(picks, fixtures, results, now_utc=now or datetime.now(timezone.utc))
    duplicates = canonical_duplicate_report(picks, fixtures, results)
    if not args.rows:
        classification.pop("rows", None)
    payload = {"classification": classification, "duplicates": duplicates if args.rows else {"summary": duplicates["summary"]}}
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
