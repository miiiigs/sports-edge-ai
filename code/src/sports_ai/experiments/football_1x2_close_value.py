"""Historical football 1X2 close-value lane.

This experiment is intentionally narrow and honest:

- It uses the local football research store.
- It treats Pinnacle close as the sharp reference.
- It treats one retail closing line as the available price.
- It logs only positive-EV candidate bets into the paper/shadow ledger.

Important limitation:
- The free source currently gives us closing snapshots, not entry-time price
  history, so this lane cannot prove CLV yet. It is a first historical
  discrepancy lane used to identify whether a retail-vs-sharp close gap would
  have produced sensible candidate bets and outcome behavior.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..agents.market.devig import devig_shin
from ..storage.paper_shadow_ledger import (
    OutcomeUpdate,
    PaperDecision,
    connect as connect_paper_shadow_ledger,
    record_decision,
    append_outcome_update,
)

SELECTION_ORDER = ("home", "draw", "away")
SELECTION_TO_RESULT = {
    "home": "H",
    "draw": "D",
    "away": "A",
}


@dataclass(frozen=True)
class HistoricalLaneConfig:
    research_db_path: str | Path
    ledger_db_path: str | Path
    competition_ids: tuple[str, ...]
    season_code: str
    retail_bookmaker: str = "Bet365"
    min_ev: float = 0.02
    min_edge: float = 0.0
    allowed_selections: tuple[str, ...] = SELECTION_ORDER
    max_fair_odds: float | None = None
    max_available_odds: float | None = None
    strategy_version: str = "historical_football_close_lane_v1"
    policy_version: str = "retail_vs_pinnacle_close_v1"


def _connect_research_store(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_market_pairs(
    conn: sqlite3.Connection,
    *,
    competition_ids: Sequence[str],
    season_code: str,
    retail_bookmaker: str,
) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in competition_ids)
    sql = f"""
        SELECT
            e.event_id,
            e.competition_id,
            e.competition_name,
            e.season_code,
            e.match_date,
            e.home_team,
            e.away_team,
            e.result_code,
            p.selection_prices_json AS pinnacle_prices_json,
            r.selection_prices_json AS retail_prices_json
        FROM football_events e
        JOIN football_market_quotes p
          ON p.event_id = e.event_id
         AND p.market_type = '1X2'
         AND p.bookmaker = 'Pinnacle'
        JOIN football_market_quotes r
          ON r.event_id = e.event_id
         AND r.market_type = '1X2'
         AND r.bookmaker = ?
        WHERE e.season_code = ?
          AND e.competition_id IN ({placeholders})
        ORDER BY e.match_date, e.event_id
    """
    params = [retail_bookmaker, season_code, *competition_ids]
    return list(conn.execute(sql, params).fetchall())


def _selection_settlement(selection: str, result_code: str | None) -> str | None:
    if not result_code:
        return None
    return "WIN" if SELECTION_TO_RESULT[selection] == result_code else "LOSS"


def _safe_data_quality(row: sqlite3.Row) -> float:
    score = 0.55
    if row["result_code"]:
        score += 0.15
    if row["competition_id"] in {"ENG1", "ESP1"}:
        score += 0.10
    return min(score, 0.95)


def _candidate_unit_return(settlement: str | None, available_price: float) -> float | None:
    if settlement == "WIN":
        return available_price - 1.0
    if settlement == "LOSS":
        return -1.0
    return None


def _build_candidates(
    market_rows: Sequence[sqlite3.Row],
    config: HistoricalLaneConfig,
) -> tuple[list[dict[str, Any]], int]:
    candidates: list[dict[str, Any]] = []
    selection_count = 0
    allowed = set(config.allowed_selections)

    for market_row in market_rows:
        pinnacle_prices = json.loads(market_row["pinnacle_prices_json"])
        retail_prices = json.loads(market_row["retail_prices_json"])
        pinn_odds = [pinnacle_prices.get(key) for key in SELECTION_ORDER]
        retail_odds = [retail_prices.get(key) for key in SELECTION_ORDER]
        if any(value in (None, 0) for value in pinn_odds + retail_odds):
            continue

        fair_probs = devig_shin([float(value) for value in pinn_odds])

        for idx, selection in enumerate(SELECTION_ORDER):
            selection_count += 1
            if selection not in allowed:
                continue

            fair_prob = fair_probs[idx]
            available_price = float(retail_odds[idx])
            implied_prob = 1.0 / available_price
            fair_odds = 1.0 / fair_prob
            edge = fair_prob - implied_prob
            ev = fair_prob * available_price - 1.0

            if ev < config.min_ev:
                continue
            if edge < config.min_edge:
                continue
            if config.max_fair_odds is not None and fair_odds > config.max_fair_odds:
                continue
            if config.max_available_odds is not None and available_price > config.max_available_odds:
                continue

            settlement = _selection_settlement(selection, market_row["result_code"])
            candidates.append(
                {
                    "event_id": market_row["event_id"],
                    "competition_id": market_row["competition_id"],
                    "competition_name": market_row["competition_name"],
                    "match_date": market_row["match_date"],
                    "home_team": market_row["home_team"],
                    "away_team": market_row["away_team"],
                    "selection_key": selection,
                    "selection_name": f"{selection}_{market_row['home_team']}_{market_row['away_team']}",
                    "available_price": available_price,
                    "fair_probability": fair_prob,
                    "fair_odds": fair_odds,
                    "market_probability": implied_prob,
                    "edge": edge,
                    "ev": ev,
                    "data_quality": _safe_data_quality(market_row),
                    "uncertainty": max(0.02, min(0.25, 0.20 - (ev / 3))),
                    "settlement": settlement,
                    "unit_return": _candidate_unit_return(settlement, available_price),
                }
            )

    return candidates, selection_count


def evaluate_historical_football_1x2_close_value(config: HistoricalLaneConfig) -> dict[str, Any]:
    """Evaluate the lane without writing to the paper/shadow ledger."""

    with _connect_research_store(config.research_db_path) as research_conn:
        market_rows = _fetch_market_pairs(
            research_conn,
            competition_ids=config.competition_ids,
            season_code=config.season_code,
            retail_bookmaker=config.retail_bookmaker,
        )

    candidates, selection_count = _build_candidates(market_rows, config)
    by_competition = Counter(candidate["competition_id"] for candidate in candidates)
    by_selection = Counter(candidate["selection_key"] for candidate in candidates)
    competition_returns: dict[str, float] = defaultdict(float)
    for candidate in candidates:
        if candidate["unit_return"] is not None:
            competition_returns[candidate["competition_id"]] += candidate["unit_return"]

    settled = [candidate for candidate in candidates if candidate["unit_return"] is not None]
    wins = sum(candidate["settlement"] == "WIN" for candidate in settled)
    total_units = sum(candidate["unit_return"] for candidate in settled if candidate["unit_return"] is not None)

    return {
        "season_code": config.season_code,
        "competition_ids": list(config.competition_ids),
        "retail_bookmaker": config.retail_bookmaker,
        "min_ev": config.min_ev,
        "min_edge": config.min_edge,
        "allowed_selections": list(config.allowed_selections),
        "max_fair_odds": config.max_fair_odds,
        "max_available_odds": config.max_available_odds,
        "matched_events": len(market_rows),
        "selection_count": selection_count,
        "candidate_count": len(candidates),
        "average_candidate_ev": (
            sum(candidate["ev"] for candidate in candidates) / len(candidates)
            if candidates
            else None
        ),
        "returns": {
            "n": len(settled),
            "total_units": total_units,
            "roi_per_bet": (total_units / len(settled)) if settled else None,
            "win_rate": (wins / len(settled)) if settled else None,
        },
        "breakdowns": {
            "by_competition": dict(by_competition),
            "by_selection": dict(by_selection),
            "competition_units": dict(competition_returns),
        },
        "sample_candidates": candidates[:5],
    }


def run_historical_football_1x2_close_value(config: HistoricalLaneConfig) -> dict[str, Any]:
    """Run the first real football validation lane and log candidates."""

    run_id = f"run_hist_close_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    inserted_ids: list[str] = []

    with _connect_research_store(config.research_db_path) as research_conn:
        market_rows = _fetch_market_pairs(
            research_conn,
            competition_ids=config.competition_ids,
            season_code=config.season_code,
            retail_bookmaker=config.retail_bookmaker,
        )
    candidates, selection_count = _build_candidates(market_rows, config)

    with connect_paper_shadow_ledger(config.ledger_db_path) as ledger_conn:
        for candidate in candidates:
            decision = PaperDecision(
                run_id=run_id,
                event_id=candidate["event_id"],
                sport="football",
                competition=candidate["competition_id"],
                market="1X2",
                selection=candidate["selection_name"],
                available_price=candidate["available_price"],
                bookmaker=config.retail_bookmaker,
                fair_probability=candidate["fair_probability"],
                fair_odds=candidate["fair_odds"],
                market_probability=candidate["market_probability"],
                edge=candidate["edge"],
                data_quality=candidate["data_quality"],
                uncertainty=candidate["uncertainty"],
                decision="RESEARCH_SIGNAL",
                decision_reason=(
                    f"historical close discrepancy: {config.retail_bookmaker} {candidate['available_price']:.2f} "
                    f"vs Pinnacle fair {candidate['fair_odds']:.2f}, EV {candidate['ev']:+.2%}"
                ),
                model_version="pinnacle_close_devig_shin_v1",
                strategy_version=config.strategy_version,
                policy_version=config.policy_version,
                notes=(
                    "historical_close_only=true; "
                    "clv_unavailable_until_entry_timestamps_exist; "
                    f"selection_filter={','.join(config.allowed_selections)}; "
                    f"min_edge={config.min_edge:.4f}; "
                    f"match_date={candidate['match_date']}; "
                    f"teams={candidate['home_team']} vs {candidate['away_team']}"
                ),
            )
            decision_id = record_decision(ledger_conn, decision)
            inserted_ids.append(decision_id)

            if candidate["settlement"] is not None:
                append_outcome_update(
                    ledger_conn,
                    OutcomeUpdate(
                        decision_id=decision_id,
                        result_status="SETTLED",
                        settlement=candidate["settlement"],
                        notes="historical settlement from football_events.result_code",
                    ),
                )

    evaluation = evaluate_historical_football_1x2_close_value(config)
    return {
        "run_id": run_id,
        "season_code": config.season_code,
        "competition_ids": list(config.competition_ids),
        "retail_bookmaker": config.retail_bookmaker,
        "min_ev": config.min_ev,
        "min_edge": config.min_edge,
        "allowed_selections": list(config.allowed_selections),
        "max_fair_odds": config.max_fair_odds,
        "max_available_odds": config.max_available_odds,
        "matched_events": len(market_rows),
        "selection_count": selection_count,
        "candidate_count": len(candidates),
        "average_candidate_ev": evaluation["average_candidate_ev"],
        "returns": evaluation["returns"],
        "breakdowns": evaluation["breakdowns"],
        "inserted_decision_ids": inserted_ids,
        "ledger_db_path": str(Path(config.ledger_db_path)),
        "historical_close_only": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI for the first historical football lane."""

    import argparse

    parser = argparse.ArgumentParser(description="Run the historical football 1X2 close-value lane.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument("--ledger-db", required=True, help="Path to paper/shadow ledger SQLite DB.")
    parser.add_argument("--season", required=True, help="Season code like 2526.")
    parser.add_argument(
        "--competition",
        action="append",
        dest="competitions",
        required=True,
        help="Competition id like ENG1 or ESP1. Repeat for multiple leagues.",
    )
    parser.add_argument("--retail-bookmaker", default="Bet365", help="Retail bookmaker to compare with Pinnacle.")
    parser.add_argument("--min-ev", type=float, default=0.02, help="Minimum EV threshold for candidates.")
    parser.add_argument("--min-edge", type=float, default=0.0, help="Minimum probability edge over retail implied.")
    parser.add_argument(
        "--selection",
        action="append",
        dest="selections",
        help="Allowed selection key like home, draw, or away. Repeat to narrow the lane.",
    )
    parser.add_argument("--max-fair-odds", type=float, help="Optional cap on Pinnacle-derived fair odds.")
    parser.add_argument("--max-available-odds", type=float, help="Optional cap on retail available odds.")
    args = parser.parse_args(argv)

    result = run_historical_football_1x2_close_value(
        HistoricalLaneConfig(
            research_db_path=args.research_db,
            ledger_db_path=args.ledger_db,
            competition_ids=tuple(args.competitions),
            season_code=args.season,
            retail_bookmaker=args.retail_bookmaker,
            min_ev=args.min_ev,
            min_edge=args.min_edge,
            allowed_selections=tuple(args.selections) if args.selections else SELECTION_ORDER,
            max_fair_odds=args.max_fair_odds,
            max_available_odds=args.max_available_odds,
        )
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
