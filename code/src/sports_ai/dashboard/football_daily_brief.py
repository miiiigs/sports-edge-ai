"""Read-only Football daily brief and live validation payloads."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from ..storage import football_canonical_fixture, football_paper_trading
from .client_history import ACTIVE_STATES, FINAL_STATES, MARKET_LABELS, MARKET_ORDER, _read_only_connect
from .football_intelligence import _calibration, _clv, _load_normalized_rows, _metrics
from .models import PREDICTION_STATUS_READY, parse_iso_datetime, utc_now_iso
from .portfolio_selection import PORTFOLIO_POLICY_ID_V1, PORTFOLIO_POLICY_VERSION_V1, PORTFOLIO_STATE_SELECTED

FOOTBALL_DAY_CUTOFF_HOURS = 3
FOOTBALL_DAY_CUTOFF_LABEL = "03:00 UTC"
DAILY_SAMPLE_MINIMUM = 30
OUTLOOK_MARKETS: tuple[tuple[str, str], ...] = (
    ("1x2", "1X2"),
    ("btts", "BTTS"),
    ("goals_total", "Goals"),
    ("corner_total", "Corners"),
    ("asian_handicap", "Asian Handicap"),
)


def football_day_for_timestamp(value: Any) -> date | None:
    parsed = _parse_utc_datetime(value)
    if parsed is None:
        return None
    return (parsed - timedelta(hours=FOOTBALL_DAY_CUTOFF_HOURS)).date()


def football_day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(hour=FOOTBALL_DAY_CUTOFF_HOURS), tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def football_day_context(now_utc: datetime | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc) if now_utc is None else _ensure_utc(now_utc)
    current_day = football_day_for_timestamp(now)
    if current_day is None:
        current_day = now.date()
    previous_day = current_day - timedelta(days=1)
    previous_start, previous_end = football_day_window(previous_day)
    return {
        "current_football_day": current_day.isoformat(),
        "previous_football_day": previous_day.isoformat(),
        "previous_day_start_utc": previous_start.isoformat(),
        "previous_day_end_utc": previous_end.isoformat(),
    }


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_utc_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return _ensure_utc(parse_iso_datetime(str(value)))
    except Exception:
        return None


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _stake_units(row: dict[str, Any]) -> float:
    payload = row.get("latest_update_payload") or {}
    try:
        return 1.0 if payload.get("stake") is None else float(payload["stake"])
    except (TypeError, ValueError):
        return 1.0


def _clv_value(row: dict[str, Any]) -> float | None:
    entry = _safe_float(row.get("entry_odds"))
    closing = _safe_float(row.get("closing_odds"))
    if entry is None or closing is None or entry <= 1.0 or closing <= 1.0:
        return None
    if row.get("closing_snapshot_id") in {None, ""}:
        return None
    return (1.0 / closing) - (1.0 / entry)


def _selected_history_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT
                s.paper_trade_id,
                s.canonical_fixture_id,
                s.market,
                s.selection,
                s.line,
                s.edge_at_selection,
                s.evaluated_at,
                s.selection_rank,
                s.selection_reason,
                s.evaluation_id
            FROM football_portfolio_selections s
            JOIN football_portfolio_evaluations e ON e.evaluation_id = s.evaluation_id
            WHERE s.policy_id = ?
              AND s.policy_version = ?
              AND s.selection_state = ?
            ORDER BY s.paper_trade_id, s.evaluated_at, s.created_at, s.portfolio_selection_id
            """,
            (PORTFOLIO_POLICY_ID_V1, PORTFOLIO_POLICY_VERSION_V1, PORTFOLIO_STATE_SELECTED),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _dedup_selected_pre_kickoff(
    selected_rows: list[dict[str, Any]],
    paper_rows_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    first_by_trade_id: dict[str, dict[str, Any]] = {}
    for selected in selected_rows:
        trade_id = str(selected.get("paper_trade_id") or "")
        paper_row = paper_rows_by_id.get(trade_id)
        selected_at = _parse_utc_datetime(selected.get("evaluated_at"))
        kickoff = _parse_utc_datetime((paper_row or {}).get("kickoff"))
        if not trade_id or paper_row is None or selected_at is None or kickoff is None:
            continue
        if selected_at >= kickoff:
            continue
        current = first_by_trade_id.get(trade_id)
        if current is None or str(selected["evaluated_at"]) < str(current["evaluated_at"]):
            first_by_trade_id[trade_id] = selected
    return first_by_trade_id


def _fixture_map(dashboard_db_path: Path, fixture_ids: set[str]) -> dict[str, dict[str, Any]]:
    if not fixture_ids or not dashboard_db_path.exists():
        return {}
    placeholders = ",".join("?" for _ in fixture_ids)
    try:
        with _read_only_connect(dashboard_db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT fixture_id, competition_name, home_team_name, away_team_name, kickoff_utc
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
        return {"HOME": f"{home} to Win", "AWAY": f"{away} to Win", "DRAW": "Match Draw"}.get(selection, selection)
    if market == "btts":
        return f"BTTS - {'Yes' if selection == 'YES' else 'No' if selection == 'NO' else selection.title()}"
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


def _selected_display_row(
    row: dict[str, Any],
    selected: dict[str, Any],
    *,
    fixture_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    fixture = fixture_map.get(str(row.get("fixture_id") or "")) or {}
    market = str(row.get("market") or "")
    clv = _clv_value(row)
    fixture_label = (
        f"{fixture.get('home_team_name')} vs {fixture.get('away_team_name')}"
        if fixture.get("home_team_name") and fixture.get("away_team_name")
        else str(row.get("fixture_id") or "Unknown fixture")
    )
    return {
        "paper_trade_id": str(row.get("paper_trade_id") or ""),
        "fixture_id": str(row.get("fixture_id") or ""),
        "canonical_fixture_id": str(selected.get("canonical_fixture_id") or ""),
        "fixture": fixture_label,
        "competition": fixture.get("competition_name") or row.get("competition_name") or "",
        "kickoff_utc": row.get("kickoff") or fixture.get("kickoff_utc"),
        "market": market,
        "market_label": MARKET_LABELS.get(market, market.replace("_", " ").title()),
        "selection": str(row.get("selection") or ""),
        "bet": _selection_label(row, fixture),
        "line": row.get("line"),
        "model_probability": row.get("model_probability"),
        "fair_price": row.get("model_fair_price"),
        "entry_odds": row.get("entry_odds") if row.get("entry_odds") is not None else row.get("available_market_price"),
        "entry_bookmaker": row.get("bookmaker"),
        "decision_timestamp": row.get("decision_timestamp"),
        "first_selected_timestamp": selected.get("evaluated_at"),
        "edge_at_selection": selected.get("edge_at_selection"),
        "stake_units": _stake_units(row),
        "trade_state": str(row.get("trade_state") or "PENDING"),
        "result": str(row.get("trade_state") or "PENDING"),
        "net_pnl": row.get("net_pnl"),
        "closing_odds": row.get("closing_odds") if row.get("closing_snapshot_id") not in {None, ""} else None,
        "closing_timestamp": row.get("closing_captured_at") if row.get("closing_snapshot_id") not in {None, ""} else None,
        "clv": clv,
    }


def _selected_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [row for row in rows if str(row.get("trade_state") or "") in FINAL_STATES]
    pending = [row for row in rows if str(row.get("trade_state") or "PENDING") in ACTIVE_STATES]
    settled_stake = sum(_stake_units(row) for row in settled)
    net_units = sum(float(row.get("net_pnl") or 0.0) for row in settled)
    decisive = [row for row in settled if str(row.get("trade_state") or "") in {"WON", "LOST", "HALF_WIN", "HALF_LOSS"}]
    weighted_wins = sum(1.0 if row.get("trade_state") == "WON" else 0.5 if row.get("trade_state") == "HALF_WIN" else 0.0 for row in decisive)
    entry_odds = [_safe_float(row.get("entry_odds") if row.get("entry_odds") is not None else row.get("available_market_price")) for row in rows]
    entry_odds = [value for value in entry_odds if value is not None]
    edges = [_safe_float(row.get("_selected_edge_at_selection")) for row in rows]
    edges = [value for value in edges if value is not None]
    clv_values = [value for row in settled if (value := _clv_value(row)) is not None]
    return {
        "matches_with_selected_bets": len({str(row.get("_canonical_fixture_id") or row.get("fixture_id") or "") for row in rows}),
        "selected_bets": len(rows),
        "settled_bets": len(settled),
        "pending_bets": len(pending),
        "wins": sum(1 for row in settled if row.get("trade_state") == "WON"),
        "losses": sum(1 for row in settled if row.get("trade_state") == "LOST"),
        "pushes": sum(1 for row in settled if row.get("trade_state") == "PUSH"),
        "half_wins": sum(1 for row in settled if row.get("trade_state") == "HALF_WIN"),
        "half_losses": sum(1 for row in settled if row.get("trade_state") == "HALF_LOSS"),
        "voids": sum(1 for row in settled if row.get("trade_state") == "VOID"),
        "settled_stake_units": settled_stake,
        "net_units": net_units,
        "roi": None if settled_stake <= 0 else net_units / settled_stake,
        "strike_rate": None if not decisive else weighted_wins / len(decisive),
        "average_entry_odds": None if not entry_odds else sum(entry_odds) / len(entry_odds),
        "average_edge": None if not edges else sum(edges) / len(edges),
        "clv_sample": len(clv_values),
        "average_clv": None if not clv_values else sum(clv_values) / len(clv_values),
        "positive_clv_rate": None if not clv_values else sum(1 for value in clv_values if value > 0.0) / len(clv_values),
        "clv_coverage": None if not settled else len(clv_values) / len(settled),
        "sample_state": "SUFFICIENT" if len(settled) >= DAILY_SAMPLE_MINIMUM else ("TRACKING" if settled else "INSUFFICIENT_SAMPLE"),
        "minimum_sample": DAILY_SAMPLE_MINIMUM,
    }


def _selected_performance(
    *,
    paper_db_path: Path,
    dashboard_db_path: Path,
    target_football_day: date,
) -> dict[str, Any]:
    empty = {**_selected_summary([]), "rows": [], "available": False, "reason": "Paper ledger unavailable"}
    if not paper_db_path.exists():
        return empty
    try:
        with _read_only_connect(paper_db_path) as conn:
            paper_rows = football_paper_trading.fetch_paper_trade_rows(conn, initialize_schema=False)
            excluded = football_canonical_fixture.fetch_excluded_paper_trade_ids(conn, initialize_schema=False)
            counted_rows = [row for row in paper_rows if str(row.get("paper_trade_id") or "") not in excluded]
            selected_by_trade_id = _dedup_selected_pre_kickoff(
                _selected_history_rows(conn),
                {str(row.get("paper_trade_id") or ""): row for row in counted_rows},
            )
    except sqlite3.Error as exc:
        return {**empty, "reason": f"Selected performance unavailable: {type(exc).__name__}"}

    rows: list[dict[str, Any]] = []
    for row in counted_rows:
        trade_id = str(row.get("paper_trade_id") or "")
        selected = selected_by_trade_id.get(trade_id)
        if selected is None:
            continue
        if football_day_for_timestamp(row.get("kickoff") or row.get("decision_timestamp")) != target_football_day:
            continue
        rows.append(
            {
                **row,
                "_selected_edge_at_selection": selected.get("edge_at_selection"),
                "_first_selected_timestamp": selected.get("evaluated_at"),
                "_canonical_fixture_id": selected.get("canonical_fixture_id"),
            }
        )

    rows.sort(key=lambda row: (str(row.get("kickoff") or ""), str(row.get("market") or ""), str(row.get("paper_trade_id") or "")))
    fixture_map = _fixture_map(dashboard_db_path, {str(row.get("fixture_id") or "") for row in rows})
    selected_lookup = {trade_id: selected for trade_id, selected in selected_by_trade_id.items()}
    return {
        **_selected_summary(rows),
        "available": True,
        "reason": None,
        "rows": [
            _selected_display_row(row, selected_lookup[str(row["paper_trade_id"])], fixture_map=fixture_map)
            for row in rows
        ],
        "method": (
            "Earliest pre-kickoff SELECTED portfolio lifecycle row per paper_trade_id, "
            "joined to immutable paper_trade_entries and latest paper_trade_updates settlement."
        ),
    }


def _model_performance(*, paper_db_path: Path, dashboard_db_path: Path) -> dict[str, Any]:
    if not paper_db_path.exists():
        selected = _metrics([])
        return {"available": False, "summary": selected, "calibration": _calibration([]), "clv": _clv([])}
    try:
        normalized_rows, raw_count, collapsed_count = _load_normalized_rows(
            paper_db_path=paper_db_path,
            dashboard_db_path=dashboard_db_path,
        )
    except sqlite3.Error:
        normalized_rows = []
        raw_count = 0
        collapsed_count = 0
    settled_rows = [row for row in normalized_rows if str(row.get("trade_state") or "") in FINAL_STATES]
    return {
        "available": bool(normalized_rows),
        "summary": _metrics(normalized_rows),
        "settled_model_opinions": len(settled_rows),
        "normalized_model_opinions": len(normalized_rows),
        "raw_line_count": raw_count,
        "collapsed_line_count": collapsed_count,
        "calibration": _calibration(normalized_rows),
        "clv": _clv(normalized_rows),
        "method": "Normalized settled model market opinions are diagnostic only and are not mixed into selected recommendation ROI.",
    }


def _duplicate_count(rows: list[dict[str, Any]]) -> int:
    counts = Counter(_selection_key(row) for row in rows)
    return sum(max(0, count - 1) for count in counts.values())


def _selection_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("fixture_id") or ""),
        str(row.get("market_family") or row.get("market") or ""),
        str(row.get("selection") or row.get("selection_label") or row.get("bet_label") or ""),
        "" if row.get("line") is None else str(float(row["line"])),
    )


def _dedupe_selection_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        key = _selection_key(row)
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)
    return selected


def _current_selected_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kickoff_utc": row.get("kickoff_utc"),
        "competition": row.get("competition_name"),
        "fixture": f"{row.get('home_team_name')} vs {row.get('away_team_name')}",
        "market": row.get("market_family") or row.get("market_label"),
        "market_label": row.get("market_label"),
        "selection": row.get("selection_label") or row.get("bet_label"),
        "model_probability": row.get("model_probability"),
        "fair_odds": row.get("fair_odds") or row.get("entry_fair_odds"),
        "current_odds": row.get("market_odds") or row.get("current_market_odds") or row.get("best_offer_odds"),
        "bookmaker": row.get("bookmaker") or row.get("current_market_bookmaker") or row.get("best_offer_bookmaker"),
        "current_edge": row.get("edge") or row.get("current_market_edge") or row.get("settlement_aware_edge"),
        "minimum_acceptable_odds": row.get("minimum_acceptable_odds"),
        "freshness": row.get("freshness") or row.get("market_freshness"),
        "safeguard_state": row.get("safeguard") or row.get("risk_status") or row.get("confidence_label"),
        "portfolio_state": row.get("portfolio_selection_state") or ("SELECTED" if row.get("portfolio_selected") else None),
        "paper_trade_id": row.get("paper_trade_id"),
    }


def _outlook_reason(row: dict[str, Any] | None, *, prediction_ready: bool) -> str:
    if row is None:
        return "MISSING_MARKET" if prediction_ready else "PREDICTION_BLOCKED"
    state = str(row.get("current_decision_state") or row.get("desk_decision") or row.get("decision_state") or "")
    if state in {"NO_BET_STALE_MARKET", "STALE_MARKET"} or str(row.get("freshness") or row.get("market_freshness") or "") == "STALE":
        return "STALE_MARKET"
    if state in {"NO_BET_SAFEGUARD"}:
        return "SAFEGUARD_BLOCKED"
    if state in {"NO_BET_VALUE"}:
        return "BELOW_EDGE_THRESHOLD"
    if state in {"NO_BET_PORTFOLIO_NOT_SELECTED"}:
        return "NOT_SELECTED_PORTFOLIO"
    if state in {"NO_BET_MISSING_PRICE", "NO_BET_UNAVAILABLE"}:
        return "MISSING_MARKET"
    if state and state != "BET":
        return state
    if not bool(row.get("portfolio_selected")):
        return "NOT_SELECTED_PORTFOLIO"
    return "SELECTED"


def _match_outlook(dashboard_payload: dict[str, Any]) -> list[dict[str, Any]]:
    cards = dashboard_payload.get("client_match_cards") or []
    outlook: list[dict[str, Any]] = []
    for card in cards:
        prediction_ready = True
        market_rows_by_family = {str(row.get("market_family") or ""): row for row in card.get("market_rows") or []}
        rows = []
        for market_family, market_label in OUTLOOK_MARKETS:
            source = market_rows_by_family.get(market_family)
            decision = "BLOCKED"
            if source is not None:
                if source.get("is_bet") and source.get("portfolio_selected"):
                    decision = "BET"
                else:
                    decision = "NO_BET"
            elif prediction_ready:
                decision = "NO_BET"
            rows.append(
                {
                    "market": market_family,
                    "market_label": market_label,
                    "selection": None if source is None else source.get("selection_label"),
                    "model_probability": None if source is None else source.get("model_probability"),
                    "fair_odds": None if source is None else source.get("fair_odds"),
                    "current_best_valid_odds": None if source is None else source.get("odds"),
                    "current_edge": None if source is None else source.get("edge"),
                    "decision": decision,
                    "reason": _outlook_reason(source, prediction_ready=prediction_ready),
                }
            )
        outlook.append(
            {
                "fixture_id": card.get("fixture_id"),
                "kickoff_utc": card.get("kickoff_utc"),
                "competition": card.get("competition_name"),
                "fixture": f"{card.get('home_team_name')} vs {card.get('away_team_name')}",
                "markets": rows,
            }
        )
    return outlook


def _current_slate(dashboard_payload: dict[str, Any], *, now_utc: datetime | None) -> dict[str, Any]:
    now = datetime.now(timezone.utc) if now_utc is None else _ensure_utc(now_utc)
    rows = list(dashboard_payload.get("current_slate_rows") or [])
    upcoming_fixtures = list(dashboard_payload.get("upcoming") or dashboard_payload.get("next_matches") or [])
    future_rows = [row for row in rows if (kickoff := _parse_utc_datetime(row.get("kickoff_utc"))) is not None and kickoff > now]
    selected_candidates = [
        row
        for row in future_rows
        if bool(row.get("portfolio_selected"))
        and str(row.get("current_decision_state") or row.get("actual_recommendation_state") or "") == "BET"
        and str(row.get("freshness") or row.get("market_freshness") or "FRESH") == "FRESH"
        and str(row.get("safeguard") or row.get("risk_status") or row.get("confidence_label") or "PASS") in {"PASS", "PASSED"}
    ]
    duplicate_selected_keys = _duplicate_count(selected_candidates)
    selected_rows = _dedupe_selection_rows(selected_candidates)
    stale_excluded = sum(1 for row in rows if bool(row.get("portfolio_selected")) and str(row.get("freshness") or row.get("market_freshness") or "") == "STALE")
    past_excluded = sum(1 for row in rows if bool(row.get("portfolio_selected")) and (kickoff := _parse_utc_datetime(row.get("kickoff_utc"))) is not None and kickoff <= now)
    prediction_ready_fixtures = {
        str(row.get("fixture_id") or "")
        for row in upcoming_fixtures
        if str(((row.get("prediction") or {}).get("prediction_status") or row.get("prediction_eligibility_status") or "")) in {PREDICTION_STATUS_READY, "PREDICTION_READY"}
    }
    blocked_fixtures = {
        str(row.get("fixture_id") or "")
        for row in upcoming_fixtures
        if str(row.get("fixture_id") or "") not in prediction_ready_fixtures
    }
    return {
        "total_upcoming_fixtures": int(dashboard_payload.get("upcoming_count") or len(upcoming_fixtures)),
        "prediction_ready_fixtures": len(prediction_ready_fixtures),
        "blocked_fixtures": len(blocked_fixtures),
        "fixtures_with_selected_bets": len({str(row.get("fixture_id") or "") for row in selected_rows}),
        "selected_bets": len(selected_rows),
        "stale_selections_excluded": stale_excluded,
        "past_selections_excluded": past_excluded,
        "duplicate_selected_keys": duplicate_selected_keys,
        "rows": [_current_selected_row(row) for row in selected_rows],
        "empty_message": "No current qualified bets. Sports Edge is waiting for sufficient verified value.",
        "match_outlook": _match_outlook(dashboard_payload),
    }


def _model_snapshot(snapshot: Any, *, now_utc: datetime | None) -> dict[str, Any]:
    now = datetime.now(timezone.utc) if now_utc is None else _ensure_utc(now_utc)
    model_as_of = None if snapshot is None else getattr(snapshot, "model_as_of", None)
    parsed = _parse_utc_datetime(model_as_of)
    return {
        "snapshot_id": None if snapshot is None else getattr(snapshot, "snapshot_id", None),
        "model_version": None if snapshot is None else getattr(snapshot, "model_version", None),
        "model_as_of": model_as_of,
        "age_days": None if parsed is None else max(0.0, (now - parsed).total_seconds() / 86400.0),
        "diagnostic_only": True,
    }


def build_daily_brief_payload(
    *,
    paper_db_path: Path,
    dashboard_db_path: Path,
    dashboard_payload: dict[str, Any] | None = None,
    model_snapshot: Any = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    context = football_day_context(now_utc)
    previous_day = date.fromisoformat(str(context["previous_football_day"]))
    dashboard_payload = dashboard_payload or {}
    selected = _selected_performance(
        paper_db_path=paper_db_path,
        dashboard_db_path=dashboard_db_path,
        target_football_day=previous_day,
    )
    previous_start, previous_end = football_day_window(previous_day)
    return {
        "generated_at": utc_now_iso() if now_utc is None else _ensure_utc(now_utc).isoformat(),
        "football_day_cutoff": FOOTBALL_DAY_CUTOFF_LABEL,
        "previous_day": {
            **context,
            "date": str(context["previous_football_day"]),
            "start_utc": previous_start.isoformat(),
            "end_utc": previous_end.isoformat(),
            "selected_recommendation_performance": selected,
        },
        "current_slate": _current_slate(dashboard_payload, now_utc=now_utc),
        "model_performance": _model_performance(
            paper_db_path=paper_db_path,
            dashboard_db_path=dashboard_db_path,
        ),
        "model_snapshot": _model_snapshot(model_snapshot, now_utc=now_utc),
        "methodology": (
            "Measurement and reporting only. Selected recommendation ROI uses historical SELECTED portfolio rows; "
            "model performance is diagnostic and separate."
        ),
    }
