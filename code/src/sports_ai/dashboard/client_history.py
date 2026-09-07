"""Read-only client-normalized football history summaries.

This module intentionally sits beside the dashboard presentation layer. It does
not alter paper-trading truth. It collapses the immutable tracked ledger into the
same client-facing grain used by the match cards: one representative qualified
position per canonical fixture and market family.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..storage import (
    football_canonical_fixture,
    football_paper_trading,
    football_portfolio_selection,
)
from .portfolio_selection import (
    PORTFOLIO_POLICY_ID_V1,
    PORTFOLIO_POLICY_VERSION_V1,
    PORTFOLIO_STATE_SELECTED,
)

FINAL_STATES = {"WON", "LOST", "PUSH", "HALF_WIN", "HALF_LOSS", "VOID"}
ACTIVE_STATES = {"PENDING", "OPEN", "UNSETTLED"}
MARKET_ORDER = {
    "1x2": 0,
    "btts": 1,
    "goals_total": 2,
    "corner_total": 3,
    "asian_handicap": 4,
}
MARKET_LABELS = {
    "1x2": "1X2",
    "btts": "BTTS",
    "goals_total": "Goals",
    "corner_total": "Corners",
    "asian_handicap": "Asian Handicap",
}


def _read_only_connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _local_date(value: Any, timezone_name: str) -> date | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.date()
    return parsed.astimezone(ZoneInfo(timezone_name)).date()


def _local_datetime_label(value: Any, timezone_name: str) -> str:
    parsed = _parse_datetime(value)
    if parsed is None:
        return "Kickoff unavailable"
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo(timezone_name))
    clock = parsed.strftime("%I:%M %p").lstrip("0")
    return f"{parsed.strftime('%b')} {parsed.day}, {clock}"


def _safe_float(value: Any, default: float = float("-inf")) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _representative_sort_key(row: dict[str, Any]) -> tuple[float, str, str]:
    return (
        _safe_float(row.get("edge")),
        str(row.get("decision_timestamp") or ""),
        str(row.get("paper_trade_id") or ""),
    )


def _fixture_map(dashboard_db_path: Path, fixture_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not fixture_ids or not dashboard_db_path.exists():
        return {}
    placeholders = ",".join("?" for _ in fixture_ids)
    try:
        with _read_only_connect(dashboard_db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT fixture_id, competition_name, home_team_name, away_team_name, kickoff_utc,
                       status, home_score, away_score
                FROM dashboard_fixtures
                WHERE fixture_id IN ({placeholders})
                """,
                tuple(sorted(fixture_ids)),
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(row["fixture_id"]): dict(row) for row in rows}


def _selection_label(row: dict[str, Any], fixture: dict[str, Any] | None) -> str:
    market = str(row.get("market") or "")
    selection = str(row.get("selection") or "").upper()
    line = row.get("line")
    home = str((fixture or {}).get("home_team_name") or "Home")
    away = str((fixture or {}).get("away_team_name") or "Away")

    if market == "1x2":
        return {"HOME": f"{home} to Win", "AWAY": f"{away} to Win", "DRAW": "Match Draw"}.get(
            selection, selection.title()
        )
    if market == "btts":
        side = "Yes" if selection == "YES" else "No" if selection == "NO" else selection.title()
        return f"BTTS - {side}"
    if market in {"goals_total", "corner_total"}:
        side = "Over" if selection == "OVER" else "Under" if selection == "UNDER" else selection.title()
        unit = "Goals" if market == "goals_total" else "Corners"
        return f"{side} {float(line):g} {unit}" if line is not None else f"{side} {unit}"
    if market == "asian_handicap":
        team = home if selection == "HOME" else away if selection == "AWAY" else selection.title()
        if line is None:
            return team
        display_line = float(line) if selection == "HOME" else -float(line)
        return f"{team} {display_line:+g}"
    return selection.title() or market.replace("_", " ").title()


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    performance = football_paper_trading.paper_trading_performance(rows)
    settled = [row for row in rows if str(row.get("trade_state") or "") in FINAL_STATES]
    decisive = [
        row
        for row in settled
        if str(row.get("trade_state") or "") in {"WON", "LOST", "HALF_WIN", "HALF_LOSS"}
    ]
    weighted_wins = sum(
        1.0 if row.get("trade_state") == "WON" else 0.5 if row.get("trade_state") == "HALF_WIN" else 0.0
        for row in decisive
    )
    return {
        "bets": len(rows),
        "settled": len(settled),
        "open": sum(1 for row in rows if str(row.get("trade_state") or "PENDING") in ACTIVE_STATES),
        "wins": sum(1 for row in settled if row.get("trade_state") == "WON"),
        "losses": sum(1 for row in settled if row.get("trade_state") == "LOST"),
        "pushes": sum(1 for row in settled if row.get("trade_state") == "PUSH"),
        "half_wins": sum(1 for row in settled if row.get("trade_state") == "HALF_WIN"),
        "half_losses": sum(1 for row in settled if row.get("trade_state") == "HALF_LOSS"),
        "voids": sum(1 for row in settled if row.get("trade_state") == "VOID"),
        "net_units": performance.get("net_units"),
        "roi": performance.get("roi"),
        "strike_rate": None if not decisive else weighted_wins / len(decisive),
    }


def _display_row(
    row: dict[str, Any],
    *,
    fixture_map: dict[str, dict[str, Any]],
    timezone_name: str,
    canonical_fixture_id: str,
) -> dict[str, Any]:
    fixture_id = str(row.get("fixture_id") or "")
    fixture = fixture_map.get(fixture_id) or {}
    home = str(fixture.get("home_team_name") or "Home")
    away = str(fixture.get("away_team_name") or "Away")
    kickoff = row.get("kickoff") or fixture.get("kickoff_utc")
    market = str(row.get("market") or "")
    return {
        **row,
        "canonical_fixture_id": canonical_fixture_id,
        "fixture_label": f"{home} vs {away}" if fixture else fixture_id,
        "competition_name": fixture.get("competition_name") or "",
        "kickoff_local": _local_datetime_label(kickoff, timezone_name),
        "market_label": MARKET_LABELS.get(market, market.replace("_", " ").title()),
        "bet_label": _selection_label(row, fixture),
        "result_label": str(row.get("trade_state") or "PENDING").replace("_", " "),
    }


def build_client_history_payload(
    *,
    paper_db_path: Path,
    dashboard_db_path: Path,
    timezone_name: str = "Asia/Manila",
    recent_day_limit: int = 14,
    recent_bet_limit: int = 24,
) -> dict[str, Any]:
    """Return read-only normalized history for client presentation.

    Selection rule:
      1. Current Portfolio V1 SELECTED position when present for the group.
      2. Otherwise the qualified tracked row with the highest persisted entry edge.

    The immutable line-level ledger is never modified.
    """

    empty_payload = {
        "available": False,
        "reason": "Paper ledger unavailable",
        "all_time": _summary([]),
        "recent_days": [],
        "latest_completed_day": None,
        "recent_settled": [],
        "open_positions": [],
        "raw_line_count": 0,
        "normalized_count": 0,
        "collapsed_line_count": 0,
    }
    if not paper_db_path.exists():
        return empty_payload

    try:
        with _read_only_connect(paper_db_path) as conn:
            paper_rows = football_paper_trading.fetch_paper_trade_rows(conn, initialize_schema=False)
            excluded = football_canonical_fixture.fetch_excluded_paper_trade_ids(conn, initialize_schema=False)
            counted_rows = [
                row
                for row in paper_rows
                if str(row.get("paper_trade_id") or "") not in excluded
            ]
            fixture_ids = {
                str(row.get("fixture_id") or "")
                for row in counted_rows
                if row.get("fixture_id") not in {None, ""}
            }
            canonical_by_fixture = football_canonical_fixture.fetch_canonical_fixture_ids_for_fixture_ids(
                conn,
                fixture_ids,
                initialize_schema=False,
            )
            try:
                portfolio_rows = football_portfolio_selection.fetch_current_portfolio_selections(
                    conn,
                    policy_id=PORTFOLIO_POLICY_ID_V1,
                    policy_version=PORTFOLIO_POLICY_VERSION_V1,
                    initialize_schema=False,
                )
            except sqlite3.Error:
                portfolio_rows = []
    except sqlite3.Error as exc:
        return {**empty_payload, "reason": f"Client history unavailable: {type(exc).__name__}"}

    portfolio_selected_rank = {
        str(row.get("paper_trade_id") or ""): int(row.get("selection_rank") or 0)
        for row in portfolio_rows
        if str(row.get("selection_state") or "") == PORTFOLIO_STATE_SELECTED
    }

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in counted_rows:
        fixture_id = str(row.get("fixture_id") or "")
        canonical_fixture_id = canonical_by_fixture.get(fixture_id) or fixture_id
        market = str(row.get("market") or "")
        if canonical_fixture_id and market:
            grouped[(canonical_fixture_id, market)].append(row)

    normalized_rows: list[dict[str, Any]] = []
    for (canonical_fixture_id, _market), rows in grouped.items():
        portfolio_selected = [
            row
            for row in rows
            if str(row.get("paper_trade_id") or "") in portfolio_selected_rank
        ]
        if portfolio_selected:
            chosen = sorted(
                portfolio_selected,
                key=lambda row: (
                    portfolio_selected_rank.get(str(row.get("paper_trade_id") or ""), 999),
                    -_safe_float(row.get("edge"), 0.0),
                    str(row.get("paper_trade_id") or ""),
                ),
            )[0]
        else:
            chosen = max(rows, key=_representative_sort_key)
        normalized_rows.append({**chosen, "_canonical_fixture_id": canonical_fixture_id})

    normalized_rows.sort(
        key=lambda row: (
            str(row.get("kickoff") or ""),
            MARKET_ORDER.get(str(row.get("market") or ""), 99),
            str(row.get("paper_trade_id") or ""),
        )
    )

    fixture_map = _fixture_map(
        dashboard_db_path,
        {
            str(row.get("fixture_id") or "")
            for row in normalized_rows
            if row.get("fixture_id") not in {None, ""}
        },
    )

    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in normalized_rows:
        local_day = _local_date(row.get("kickoff") or row.get("decision_timestamp"), timezone_name)
        if local_day is not None:
            by_day[local_day].append(row)

    recent_days: list[dict[str, Any]] = []
    for day in sorted(by_day, reverse=True):
        day_rows = by_day[day]
        summary = _summary(day_rows)
        canonical_matches = {
            str(row.get("_canonical_fixture_id") or "")
            for row in day_rows
            if row.get("_canonical_fixture_id") not in {None, ""}
        }
        market_rows = []
        for market in sorted(MARKET_ORDER, key=MARKET_ORDER.get):
            scoped = [row for row in day_rows if str(row.get("market") or "") == market]
            if scoped:
                market_rows.append({"market": market, "label": MARKET_LABELS[market], **_summary(scoped)})
        recent_days.append(
            {
                "date": day.isoformat(),
                "date_label": day.strftime("%b %d, %Y"),
                "matches": len(canonical_matches),
                **summary,
                "by_market": market_rows,
            }
        )
        if len(recent_days) >= max(1, recent_day_limit):
            break

    settled_rows = [row for row in normalized_rows if str(row.get("trade_state") or "") in FINAL_STATES]
    settled_rows.sort(
        key=lambda row: (
            str(row.get("updated_at") or row.get("decision_timestamp") or ""),
            str(row.get("paper_trade_id") or ""),
        ),
        reverse=True,
    )
    open_rows = [row for row in normalized_rows if str(row.get("trade_state") or "PENDING") in ACTIVE_STATES]
    open_rows.sort(key=lambda row: str(row.get("kickoff") or ""))

    recent_settled = [
        _display_row(
            row,
            fixture_map=fixture_map,
            timezone_name=timezone_name,
            canonical_fixture_id=str(row.get("_canonical_fixture_id") or ""),
        )
        for row in settled_rows[: max(1, recent_bet_limit)]
    ]
    open_positions = [
        _display_row(
            row,
            fixture_map=fixture_map,
            timezone_name=timezone_name,
            canonical_fixture_id=str(row.get("_canonical_fixture_id") or ""),
        )
        for row in open_rows[: max(1, recent_bet_limit)]
    ]

    latest_completed_day = next(
        (day for day in recent_days if day["open"] == 0 and day["settled"] > 0),
        None,
    )

    return {
        "available": True,
        "reason": None,
        "all_time": _summary(normalized_rows),
        "recent_days": recent_days,
        "latest_completed_day": latest_completed_day,
        "recent_settled": recent_settled,
        "open_positions": open_positions,
        "raw_line_count": len(counted_rows),
        "normalized_count": len(normalized_rows),
        "collapsed_line_count": max(0, len(counted_rows) - len(normalized_rows)),
    }
