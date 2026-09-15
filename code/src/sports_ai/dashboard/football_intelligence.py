"""Read-only Football intelligence and validation metrics.

This module measures the existing immutable paper-trading record. It does not
change predictions, qualification, thresholds, Portfolio selection, settlement,
or provider behaviour. Client positions are normalized with the same rule used
by the History view: one representative qualified position per canonical
fixture and market family.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from ..storage import (
    football_canonical_fixture,
    football_paper_trading,
    football_portfolio_selection,
)
from .client_history import (
    FINAL_STATES,
    MARKET_LABELS,
    MARKET_ORDER,
    _local_date,
    _read_only_connect,
    _representative_sort_key,
    _safe_float,
)
from .portfolio_selection import (
    PORTFOLIO_POLICY_ID_V1,
    PORTFOLIO_POLICY_VERSION_V1,
    PORTFOLIO_STATE_SELECTED,
)

MIN_GLOBAL_SAMPLE = 100
MIN_SEGMENT_SAMPLE = 30
MIN_CALIBRATION_SAMPLE = 75
MIN_CALIBRATION_BUCKET_SAMPLE = 20
MIN_CLV_SAMPLE = 30

WINDOWS: tuple[tuple[str, str, int | None], ...] = (
    ("7", "7D", 7),
    ("30", "30D", 30),
    ("90", "90D", 90),
    ("all", "All Time", None),
)

CALIBRATION_BINS: tuple[tuple[str, float, float], ...] = (
    ("0-40%", 0.0, 0.40),
    ("40-50%", 0.40, 0.50),
    ("50-55%", 0.50, 0.55),
    ("55-60%", 0.55, 0.60),
    ("60-65%", 0.60, 0.65),
    ("65-70%", 0.65, 0.70),
    ("70-75%", 0.70, 0.75),
    ("75%+", 0.75, 1.0000001),
)


def _entry_odds(row: dict[str, Any]) -> float | None:
    value = row.get("entry_odds")
    if value is None:
        value = row.get("available_market_price")
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return None
    return odds if odds > 1.0 else None


def _finite_probability(value: Any) -> float | None:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(probability) or probability < 0.0 or probability > 1.0:
        return None
    return probability


def _chunked(values: Iterable[str], size: int = 800) -> Iterable[list[str]]:
    batch: list[str] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _competition_map_from_dashboard(
    dashboard_db_path: Path,
    fixture_ids: set[str],
) -> dict[str, str]:
    if not fixture_ids or not dashboard_db_path.exists():
        return {}
    result: dict[str, str] = {}
    try:
        with _read_only_connect(dashboard_db_path) as conn:
            for batch in _chunked(sorted(fixture_ids)):
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""
                    SELECT fixture_id, competition_name
                    FROM dashboard_fixtures
                    WHERE fixture_id IN ({placeholders})
                    """,
                    tuple(batch),
                ).fetchall()
                for row in rows:
                    competition = str(row["competition_name"] or "").strip()
                    if competition:
                        result[str(row["fixture_id"])] = competition
    except sqlite3.Error:
        return {}
    return result


def _competition_map_from_market_snapshots(
    conn: sqlite3.Connection,
    fixture_ids: set[str],
) -> dict[str, str]:
    """Fallback competition labels from already-persisted price snapshots."""
    result: dict[str, str] = {}
    if not fixture_ids:
        return result
    try:
        for batch in _chunked(sorted(fixture_ids)):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT fixture_id, competition_name, snapshot_id
                FROM market_snapshots
                WHERE fixture_id IN ({placeholders})
                  AND competition_name IS NOT NULL
                  AND competition_name != ''
                ORDER BY snapshot_id DESC
                """,
                tuple(batch),
            ).fetchall()
            for row in rows:
                result.setdefault(str(row["fixture_id"]), str(row["competition_name"]))
    except sqlite3.Error:
        return {}
    return result


def _load_normalized_rows(
    *,
    paper_db_path: Path,
    dashboard_db_path: Path,
) -> tuple[list[dict[str, Any]], int, int]:
    """Load client-normalized positions without mutating any schema or record."""
    if not paper_db_path.exists():
        return [], 0, 0

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
        snapshot_competitions = _competition_map_from_market_snapshots(conn, fixture_ids)

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

    dashboard_competitions = _competition_map_from_dashboard(dashboard_db_path, fixture_ids)
    for row in normalized_rows:
        fixture_id = str(row.get("fixture_id") or "")
        row["_competition_name"] = (
            dashboard_competitions.get(fixture_id)
            or snapshot_competitions.get(fixture_id)
            or "Unknown competition"
        )

    normalized_rows.sort(
        key=lambda row: (
            str(row.get("kickoff") or row.get("decision_timestamp") or ""),
            MARKET_ORDER.get(str(row.get("market") or ""), 99),
            str(row.get("paper_trade_id") or ""),
        )
    )
    return normalized_rows, len(counted_rows), max(0, len(counted_rows) - len(normalized_rows))


def _settled(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if str(row.get("trade_state") or "") in FINAL_STATES]


def _metrics(rows: list[dict[str, Any]], *, minimum_sample: int = MIN_SEGMENT_SAMPLE) -> dict[str, Any]:
    settled = _settled(rows)
    performance = football_paper_trading.paper_trading_performance(settled)
    decisive = [
        row
        for row in settled
        if str(row.get("trade_state") or "") in {"WON", "LOST", "HALF_WIN", "HALF_LOSS"}
    ]
    weighted_wins = sum(
        1.0 if row.get("trade_state") == "WON" else 0.5 if row.get("trade_state") == "HALF_WIN" else 0.0
        for row in decisive
    )
    odds_values = [value for row in settled if (value := _entry_odds(row)) is not None]
    edge_values = [
        value
        for row in settled
        if math.isfinite(value := _safe_float(row.get("edge"), float("nan")))
    ]
    canonical_matches = {
        str(row.get("_canonical_fixture_id") or row.get("fixture_id") or "")
        for row in settled
        if row.get("_canonical_fixture_id") or row.get("fixture_id")
    }
    return {
        "bets": len(settled),
        "matches": len(canonical_matches),
        "wins": sum(1 for row in settled if row.get("trade_state") == "WON"),
        "losses": sum(1 for row in settled if row.get("trade_state") == "LOST"),
        "pushes": sum(1 for row in settled if row.get("trade_state") == "PUSH"),
        "half_wins": sum(1 for row in settled if row.get("trade_state") == "HALF_WIN"),
        "half_losses": sum(1 for row in settled if row.get("trade_state") == "HALF_LOSS"),
        "voids": sum(1 for row in settled if row.get("trade_state") == "VOID"),
        "net_units": performance.get("net_units"),
        "roi": performance.get("roi"),
        "strike_rate": None if not decisive else weighted_wins / len(decisive),
        "average_odds": None if not odds_values else sum(odds_values) / len(odds_values),
        "average_edge": None if not edge_values else sum(edge_values) / len(edge_values),
        "sample_state": "SUFFICIENT" if len(settled) >= minimum_sample else "INSUFFICIENT_SAMPLE",
        "minimum_sample": minimum_sample,
    }


def _window_rows(
    rows: list[dict[str, Any]],
    *,
    days: int | None,
    as_of: date,
    timezone_name: str,
) -> list[dict[str, Any]]:
    if days is None:
        return list(rows)
    start = as_of - timedelta(days=max(1, days) - 1)
    selected: list[dict[str, Any]] = []
    for row in rows:
        local_day = _local_date(row.get("kickoff") or row.get("decision_timestamp"), timezone_name)
        if local_day is not None and start <= local_day <= as_of:
            selected.append(row)
    return selected


def _group_metrics(
    rows: list[dict[str, Any]],
    *,
    key_fn,
    label_fn=None,
    order: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = str(key_fn(row))
        grouped[key].append(row)
    keys = list(grouped)
    if order is not None:
        keys.sort(key=lambda value: (order.get(value, 999), value))
    else:
        keys.sort(key=lambda value: (-len(grouped[value]), value.lower()))
    return [
        {
            "key": key,
            "label": label_fn(key) if label_fn else key,
            **_metrics(grouped[key]),
        }
        for key in keys
    ]


def _edge_bucket(row: dict[str, Any]) -> str:
    edge = _safe_float(row.get("edge"), float("nan"))
    if not math.isfinite(edge):
        return "Unknown"
    if edge < 0.0:
        return "<0%"
    if edge < 0.02:
        return "0-2%"
    if edge < 0.05:
        return "2-5%"
    if edge < 0.10:
        return "5-10%"
    return "10%+"


def _odds_bucket(row: dict[str, Any]) -> str:
    odds = _entry_odds(row)
    if odds is None:
        return "Unknown"
    if odds < 1.50:
        return "<1.50"
    if odds < 2.00:
        return "1.50-2.00"
    if odds < 3.00:
        return "2.00-3.00"
    return "3.00+"


def _calibration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    samples: list[tuple[float, float, dict[str, Any]]] = []
    for row in rows:
        state = str(row.get("trade_state") or "")
        if state not in {"WON", "LOST"}:
            continue
        probability = _finite_probability(row.get("model_probability"))
        if probability is None:
            continue
        samples.append((probability, 1.0 if state == "WON" else 0.0, row))

    if not samples:
        return {
            "sample": 0,
            "brier_score": None,
            "log_loss": None,
            "expected_calibration_error": None,
            "status": "INSUFFICIENT_SAMPLE",
            "minimum_sample": MIN_CALIBRATION_SAMPLE,
            "buckets": [],
        }

    brier = sum((probability - outcome) ** 2 for probability, outcome, _ in samples) / len(samples)
    epsilon = 1e-9
    log_loss = -sum(
        outcome * math.log(min(max(probability, epsilon), 1.0 - epsilon))
        + (1.0 - outcome) * math.log(min(max(1.0 - probability, epsilon), 1.0 - epsilon))
        for probability, outcome, _ in samples
    ) / len(samples)

    buckets: list[dict[str, Any]] = []
    weighted_gap = 0.0
    for label, lower, upper in CALIBRATION_BINS:
        bucket_samples = [sample for sample in samples if lower <= sample[0] < upper]
        if not bucket_samples:
            continue
        avg_probability = sum(sample[0] for sample in bucket_samples) / len(bucket_samples)
        actual_rate = sum(sample[1] for sample in bucket_samples) / len(bucket_samples)
        gap = actual_rate - avg_probability
        weighted_gap += abs(gap) * len(bucket_samples)
        buckets.append(
            {
                "label": label,
                "sample": len(bucket_samples),
                "average_probability": avg_probability,
                "actual_rate": actual_rate,
                "gap": gap,
                "sample_state": (
                    "SUFFICIENT" if len(bucket_samples) >= MIN_CALIBRATION_BUCKET_SAMPLE else "INSUFFICIENT_SAMPLE"
                ),
                "minimum_sample": MIN_CALIBRATION_BUCKET_SAMPLE,
            }
        )

    return {
        "sample": len(samples),
        "brier_score": brier,
        "log_loss": log_loss,
        "expected_calibration_error": weighted_gap / len(samples),
        "status": "SUFFICIENT" if len(samples) >= MIN_CALIBRATION_SAMPLE else "INSUFFICIENT_SAMPLE",
        "minimum_sample": MIN_CALIBRATION_SAMPLE,
        "buckets": buckets,
        "method_note": "WON/LOST normalized positions only; pushes, half-results and voids are excluded from binary calibration.",
    }


def _clv(rows: list[dict[str, Any]]) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for row in rows:
        closing_snapshot_id = row.get("closing_snapshot_id")
        closing_margin = row.get("closing_margin")
        clv_pct = row.get("clv_pct")
        if closing_snapshot_id in {None, ""} or closing_margin is None or clv_pct is None:
            continue
        try:
            margin = float(closing_margin)
            clv = float(clv_pct)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(margin) or margin < 0.0 or not math.isfinite(clv):
            continue
        samples.append({"row": row, "clv": clv})

    by_market: list[dict[str, Any]] = []
    grouped: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        grouped[str(sample["row"].get("market") or "Unknown")].append(float(sample["clv"]))
    for market in sorted(grouped, key=lambda value: (MARKET_ORDER.get(value, 999), value)):
        values = grouped[market]
        by_market.append(
            {
                "market": market,
                "label": MARKET_LABELS.get(market, market.replace("_", " ").title()),
                "sample": len(values),
                "average_clv": sum(values) / len(values),
                "positive_rate": sum(1 for value in values if value > 0.0) / len(values),
                "sample_state": "SUFFICIENT" if len(values) >= MIN_SEGMENT_SAMPLE else "INSUFFICIENT_SAMPLE",
            }
        )

    values = [float(sample["clv"]) for sample in samples]
    settled_count = len(_settled(rows))
    return {
        "sample": len(values),
        "coverage": None if settled_count == 0 else len(values) / settled_count,
        "average_clv": None if not values else sum(values) / len(values),
        "positive_rate": None if not values else sum(1 for value in values if value > 0.0) / len(values),
        "status": "SUFFICIENT" if len(values) >= MIN_CLV_SAMPLE else "INSUFFICIENT_DATA",
        "minimum_sample": MIN_CLV_SAMPLE,
        "by_market": by_market,
        "method_note": "CLV uses the Football build-spec definition: entry_odds / closing_odds / (1 + closing_margin) - 1. Only persisted complete closing books are counted; missing closing margin evidence fails closed.",
    }


def _health(summary: dict[str, Any], calibration: dict[str, Any], clv: dict[str, Any]) -> dict[str, Any]:
    requirements = {
        "settled_bets": int(summary.get("bets") or 0),
        "calibration_sample": int(calibration.get("sample") or 0),
        "clv_sample": int(clv.get("sample") or 0),
    }
    if (
        requirements["settled_bets"] < MIN_GLOBAL_SAMPLE
        or requirements["calibration_sample"] < MIN_CALIBRATION_SAMPLE
        or requirements["clv_sample"] < MIN_CLV_SAMPLE
    ):
        return {
            "state": "INSUFFICIENT_SAMPLE",
            "label": "INSUFFICIENT SAMPLE",
            "reason": "More normalized settled bets and genuine closing-price observations are required before judging model health.",
            "requirements": requirements,
        }

    concerns: list[str] = []
    ece = calibration.get("expected_calibration_error")
    average_clv = clv.get("average_clv")
    roi = summary.get("roi")
    if ece is not None and float(ece) > 0.07:
        concerns.append("calibration error above 7%")
    if average_clv is not None and float(average_clv) <= 0.0:
        concerns.append("average CLV is not positive")
    if roi is not None and float(roi) < 0.0:
        concerns.append("window ROI is negative")

    if concerns:
        return {
            "state": "WATCH",
            "label": "WATCH",
            "reason": "; ".join(concerns),
            "requirements": requirements,
        }
    return {
        "state": "HEALTHY",
        "label": "HEALTHY",
        "reason": "Sample thresholds are met with acceptable calibration, positive CLV and non-negative ROI in this window.",
        "requirements": requirements,
    }


def _market_leaders(by_market: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    eligible = [row for row in by_market if row.get("sample_state") == "SUFFICIENT" and row.get("roi") is not None]
    if not eligible:
        return None, None
    best = max(eligible, key=lambda row: float(row["roi"]))
    weakest = min(eligible, key=lambda row: float(row["roi"]))
    return str(best["label"]), str(weakest["label"])


def build_football_intelligence_payload(
    *,
    paper_db_path: Path,
    dashboard_db_path: Path,
    timezone_name: str = "Asia/Manila",
    selected_window: str = "30",
    as_of: date | None = None,
) -> dict[str, Any]:
    """Build a read-only evidence layer over normalized Football paper positions."""
    selected_window = selected_window if selected_window in {item[0] for item in WINDOWS} else "30"
    current_day = as_of or datetime.now(ZoneInfo(timezone_name)).date()

    empty = {
        "available": False,
        "reason": "Paper ledger unavailable",
        "selected_window": selected_window,
        "window_options": [{"key": key, "label": label} for key, label, _ in WINDOWS],
        "selected": _metrics([]),
        "by_market": [],
        "by_league": [],
        "by_edge": [],
        "by_odds": [],
        "calibration": _calibration([]),
        "clv": _clv([]),
        "health": _health(_metrics([]), _calibration([]), _clv([])),
        "raw_line_count": 0,
        "normalized_count": 0,
        "collapsed_line_count": 0,
        "as_of": current_day.isoformat(),
    }
    if not paper_db_path.exists():
        return empty

    try:
        normalized_rows, raw_line_count, collapsed_line_count = _load_normalized_rows(
            paper_db_path=paper_db_path,
            dashboard_db_path=dashboard_db_path,
        )
    except sqlite3.Error as exc:
        return {**empty, "reason": f"Intelligence unavailable: {type(exc).__name__}"}

    windows: dict[str, dict[str, Any]] = {}
    selected_rows: list[dict[str, Any]] = []
    for key, label, days in WINDOWS:
        window_rows = _window_rows(normalized_rows, days=days, as_of=current_day, timezone_name=timezone_name)
        summary = _metrics(window_rows, minimum_sample=MIN_GLOBAL_SAMPLE)
        windows[key] = {"key": key, "label": label, **summary}
        if key == selected_window:
            selected_rows = window_rows

    selected_summary = windows[selected_window]
    by_market = _group_metrics(
        selected_rows,
        key_fn=lambda row: row.get("market") or "Unknown",
        label_fn=lambda key: MARKET_LABELS.get(key, key.replace("_", " ").title()),
        order=MARKET_ORDER,
    )
    by_league = _group_metrics(
        selected_rows,
        key_fn=lambda row: row.get("_competition_name") or "Unknown competition",
    )
    edge_order = {"<0%": 0, "0-2%": 1, "2-5%": 2, "5-10%": 3, "10%+": 4, "Unknown": 5}
    odds_order = {"<1.50": 0, "1.50-2.00": 1, "2.00-3.00": 2, "3.00+": 3, "Unknown": 4}
    by_edge = _group_metrics(selected_rows, key_fn=_edge_bucket, order=edge_order)
    by_odds = _group_metrics(selected_rows, key_fn=_odds_bucket, order=odds_order)
    calibration = _calibration(selected_rows)
    clv = _clv(selected_rows)
    health = _health(selected_summary, calibration, clv)
    best_market, weakest_market = _market_leaders(by_market)

    model_versions = Counter(
        str(row.get("model_version") or "Unknown")
        for row in selected_rows
        if row.get("model_version") not in {None, ""}
    )
    dominant_model_version = model_versions.most_common(1)[0][0] if model_versions else None

    return {
        "available": True,
        "reason": None,
        "model_label": "Route B Composite",
        "model_version": dominant_model_version,
        "selected_window": selected_window,
        "window_options": [{"key": key, "label": label} for key, label, _ in WINDOWS],
        "windows": windows,
        "selected": selected_summary,
        "by_market": by_market,
        "by_league": by_league,
        "by_edge": by_edge,
        "by_odds": by_odds,
        "calibration": calibration,
        "clv": clv,
        "health": health,
        "best_market": best_market,
        "weakest_market": weakest_market,
        "raw_line_count": raw_line_count,
        "normalized_count": len(normalized_rows),
        "collapsed_line_count": collapsed_line_count,
        "as_of": current_day.isoformat(),
        "sample_thresholds": {
            "global": MIN_GLOBAL_SAMPLE,
            "segment": MIN_SEGMENT_SAMPLE,
            "calibration": MIN_CALIBRATION_SAMPLE,
            "calibration_bucket": MIN_CALIBRATION_BUCKET_SAMPLE,
            "clv": MIN_CLV_SAMPLE,
        },
        "methodology": (
            "Read-only normalized client positions. No model, threshold, provider, qualification, Portfolio, "
            "settlement or historical ledger mutations are performed by this analysis."
        ),
    }
