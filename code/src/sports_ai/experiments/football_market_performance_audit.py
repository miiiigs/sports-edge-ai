"""Read-only audit of football market performance at raw and client-normalized grain.

The purpose is to verify which market is genuinely carrying performance before
narrowing production scope. In particular, it independently reports Asian
Handicap results from the immutable paper ledger and from the client-normalized
one-position-per-canonical-fixture-and-market view used by the dashboard.

No trading, selection, pricing, settlement, or production state is modified.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Any

from sports_ai.dashboard.client_history import _representative_sort_key
from sports_ai.dashboard.portfolio_selection import (
    PORTFOLIO_POLICY_ID_V1,
    PORTFOLIO_POLICY_VERSION_V1,
    PORTFOLIO_STATE_SELECTED,
)
from sports_ai.storage import (
    football_canonical_fixture,
    football_paper_trading,
    football_portfolio_selection,
)


MARKET_ORDER = ("1x2", "btts", "goals_total", "corner_total", "asian_handicap")


def _load_counted_rows(paper_db_path: Path) -> tuple[list[dict[str, Any]], Any]:
    conn = football_paper_trading.connect(paper_db_path)
    rows = football_paper_trading.fetch_paper_trade_rows(conn, initialize_schema=False)
    excluded = football_canonical_fixture.fetch_excluded_paper_trade_ids(
        conn,
        initialize_schema=False,
    )
    counted = [
        row
        for row in rows
        if str(row.get("paper_trade_id") or "") not in excluded
    ]
    return counted, conn


def _client_normalize(
    conn,
    counted_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
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
    except Exception:
        portfolio_rows = []

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

    normalized: list[dict[str, Any]] = []
    for (_canonical_fixture_id, _market), rows in grouped.items():
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
                    -float(row.get("edge") or 0.0),
                    str(row.get("paper_trade_id") or ""),
                ),
            )[0]
        else:
            chosen = max(rows, key=_representative_sort_key)
        normalized.append(chosen)
    return normalized


def _render_market_summary(label: str, performance: dict[str, Any]) -> None:
    print(f"FOOTBALL_MARKET_AUDIT_VIEW view={label}", flush=True)
    by_market = performance.get("by_market") or performance.get("market_summary") or {}
    for market in MARKET_ORDER:
        row = by_market.get(market)
        if not row:
            continue
        print(
            "FOOTBALL_MARKET_AUDIT "
            f"view={label} market={market} "
            f"positions={row.get('positions')} settled={row.get('settled_positions')} "
            f"wins={row.get('wins')} losses={row.get('losses')} "
            f"pushes={row.get('pushes')} half_wins={row.get('half_wins')} "
            f"half_losses={row.get('half_losses')} voids={row.get('voids')} "
            f"hit_rate={row.get('hit_rate')} net_units={row.get('net_units')} "
            f"roi={row.get('roi')} average_odds={row.get('average_odds')} "
            f"break_even_rate={row.get('break_even_rate')}",
            flush=True,
        )


def audit_market_performance(paper_db_path: Path) -> dict[str, Any]:
    counted_rows, conn = _load_counted_rows(paper_db_path)
    try:
        normalized_rows = _client_normalize(conn, counted_rows)
    finally:
        conn.close()

    raw = football_paper_trading.paper_trading_performance(counted_rows)
    normalized = football_paper_trading.paper_trading_performance(normalized_rows)
    return {
        "raw_counted_rows": len(counted_rows),
        "normalized_positions": len(normalized_rows),
        "collapsed_rows": max(0, len(counted_rows) - len(normalized_rows)),
        "raw": raw,
        "normalized": normalized,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only football market performance audit")
    parser.add_argument("paper_db_path", type=Path)
    args = parser.parse_args(argv)

    if not args.paper_db_path.exists():
        print(
            f"FOOTBALL_MARKET_AUDIT_BLOCKED missing_paper_db path={args.paper_db_path}",
            flush=True,
        )
        return 2

    result = audit_market_performance(args.paper_db_path)
    print(
        "FOOTBALL_MARKET_AUDIT_START "
        f"raw_counted_rows={result['raw_counted_rows']} "
        f"normalized_positions={result['normalized_positions']} "
        f"collapsed_rows={result['collapsed_rows']}",
        flush=True,
    )
    _render_market_summary("raw", result["raw"])
    _render_market_summary("client_normalized", result["normalized"])

    normalized_markets = (
        result["normalized"].get("by_market")
        or result["normalized"].get("market_summary")
        or {}
    )
    asian = normalized_markets.get("asian_handicap")
    if asian:
        print(
            "FOOTBALL_ASIAN_HANDICAP_VERDICT "
            f"positions={asian.get('positions')} settled={asian.get('settled_positions')} "
            f"hit_rate={asian.get('hit_rate')} net_units={asian.get('net_units')} "
            f"roi={asian.get('roi')}",
            flush=True,
        )
    else:
        print("FOOTBALL_ASIAN_HANDICAP_VERDICT no_positions", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
