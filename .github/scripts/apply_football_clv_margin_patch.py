from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one match in {path}: found {count}\n{old[:160]}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_between(path: Path, start_marker: str, end_marker: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    path.write_text(text[:start] + replacement + text[end:], encoding="utf-8")


storage = ROOT / "code/src/sports_ai/storage/football_paper_trading.py"
intelligence = ROOT / "code/src/sports_ai/dashboard/football_intelligence.py"
intelligence_test = ROOT / "code/tests/test_football_intelligence.py"
clv_test = ROOT / "code/tests/test_football_clv_margin.py"

replace_once(storage, "import json\nimport sqlite3\n", "import json\nimport math\nimport sqlite3\n")
replace_once(
    storage,
    "        closing_odds REAL,\n        closing_captured_at TEXT,\n        closing_snapshot_id INTEGER,\n        staking_rule_version TEXT,\n",
    "        closing_odds REAL,\n        closing_captured_at TEXT,\n        closing_snapshot_id INTEGER,\n        closing_margin REAL,\n        clv_pct REAL,\n        staking_rule_version TEXT,\n",
)
replace_once(
    storage,
    '            "canonical_business_key_signature": "TEXT",\n            "staking_rule_version": "TEXT",\n',
    '            "canonical_business_key_signature": "TEXT",\n            "closing_margin": "REAL",\n            "clv_pct": "REAL",\n            "staking_rule_version": "TEXT",\n',
)
replace_once(
    storage,
    "    closing_odds: float | None = None\n    closing_captured_at: str | None = None\n    closing_snapshot_id: int | None = None\n    staking_rule_version: str | None = None\n",
    "    closing_odds: float | None = None\n    closing_captured_at: str | None = None\n    closing_snapshot_id: int | None = None\n    closing_margin: float | None = None\n    clv_pct: float | None = None\n    staking_rule_version: str | None = None\n",
)

helper = '''_EXPECTED_SELECTIONS_BY_MARKET = {
    "1x2": frozenset({"HOME", "DRAW", "AWAY"}),
    "btts": frozenset({"YES", "NO"}),
    "goals_total": frozenset({"OVER", "UNDER"}),
    "corner_total": frozenset({"OVER", "UNDER"}),
    "asian_handicap": frozenset({"HOME", "AWAY"}),
}


def margin_adjusted_clv(*, entry_odds: float, closing_odds: float, closing_margin: float) -> float:
    """Return spec CLV: entry / close / (1 + closing margin) - 1."""
    values = (float(entry_odds), float(closing_odds), float(closing_margin))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("CLV inputs must be finite")
    if values[0] <= 1.0 or values[1] <= 1.0:
        raise ValueError("entry and closing odds must be > 1")
    if values[2] < 0.0:
        raise ValueError("closing margin must be >= 0")
    return values[0] / values[1] / (1.0 + values[2]) - 1.0


def closing_market_margin_for_snapshot(
    conn: sqlite3.Connection,
    closing_snapshot_id: int,
    *,
    initialize_schema: bool = True,
) -> float | None:
    """Derive overround from the exact persisted bookmaker closing book.

    The calculation intentionally fails closed unless every expected outcome for
    the anchor snapshot's exact fixture/provider/bookmaker/market/line/capture
    batch is present and valid. No external price reconstruction is performed.
    """
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    anchor = conn.execute(
        """
        SELECT fixture_id, provider, bookmaker, market, line, captured_at, is_pre_match
        FROM market_snapshots
        WHERE snapshot_id = ?
        """,
        (int(closing_snapshot_id),),
    ).fetchone()
    if anchor is None or not bool(anchor["is_pre_match"]):
        return None
    market = str(anchor["market"]).lower()
    expected = _EXPECTED_SELECTIONS_BY_MARKET.get(market)
    if expected is None:
        return None
    rows = conn.execute(
        """
        SELECT selection, odds, snapshot_id
        FROM market_snapshots
        WHERE fixture_id = ?
          AND provider = ?
          AND bookmaker = ?
          AND market = ?
          AND line IS ?
          AND captured_at = ?
          AND is_pre_match = ?
        ORDER BY snapshot_id
        """,
        (
            anchor["fixture_id"],
            anchor["provider"],
            anchor["bookmaker"],
            anchor["market"],
            anchor["line"],
            anchor["captured_at"],
            anchor["is_pre_match"],
        ),
    ).fetchall()
    prices: dict[str, float] = {}
    for row in rows:
        selection = str(row["selection"]).upper()
        try:
            odds = float(row["odds"])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(odds) or odds <= 1.0:
            return None
        if selection in expected:
            prices[selection] = odds
    if set(prices) != set(expected):
        return None
    booksum = sum(1.0 / prices[selection] for selection in expected)
    margin = booksum - 1.0
    if not math.isfinite(margin) or margin < 0.0:
        return None
    return margin


'''
text = storage.read_text(encoding="utf-8")
marker = "def record_paper_trade_entry(\n"
if marker not in text:
    raise RuntimeError("record_paper_trade_entry marker missing")
storage.write_text(text.replace(marker, helper + marker, 1), encoding="utf-8")

replace_once(
    storage,
    "                closing_odds, closing_captured_at, closing_snapshot_id, business_key_signature,\n                canonical_business_key_signature, staking_rule_version, staking_probability,\n",
    "                closing_odds, closing_captured_at, closing_snapshot_id, closing_margin, clv_pct,\n                business_key_signature, canonical_business_key_signature, staking_rule_version, staking_probability,\n",
)
replace_once(
    storage,
    "            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n",
    "            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n",
)
replace_once(
    storage,
    "                entry.closing_odds,\n                entry.closing_captured_at,\n                entry.closing_snapshot_id,\n                entry.business_key_signature,\n",
    "                entry.closing_odds,\n                entry.closing_captured_at,\n                entry.closing_snapshot_id,\n                entry.closing_margin,\n                entry.clv_pct,\n                entry.business_key_signature,\n",
)

new_attach = '''def attach_closing_snapshot(
    conn: sqlite3.Connection,
    *,
    paper_trade_id: str,
    closing_odds: float,
    closing_captured_at: str,
    closing_snapshot_id: int | None,
    initialize_schema: bool = True,
) -> None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    trade = conn.execute(
        """
        SELECT fixture_id, market, selection, line, entry_odds
        FROM paper_trade_entries
        WHERE paper_trade_id = ?
        """,
        (paper_trade_id,),
    ).fetchone()
    closing_margin: float | None = None
    clv_pct: float | None = None
    if trade is not None and closing_snapshot_id is not None:
        anchor = conn.execute(
            """
            SELECT fixture_id, market, selection, line, odds
            FROM market_snapshots
            WHERE snapshot_id = ?
            """,
            (int(closing_snapshot_id),),
        ).fetchone()
        same_identity = bool(
            anchor is not None
            and str(anchor["fixture_id"]) == str(trade["fixture_id"])
            and str(anchor["market"]).lower() == str(trade["market"]).lower()
            and str(anchor["selection"]).upper() == str(trade["selection"]).upper()
            and anchor["line"] == trade["line"]
            and math.isclose(float(anchor["odds"]), float(closing_odds), rel_tol=0.0, abs_tol=1e-12)
        )
        if same_identity:
            closing_margin = closing_market_margin_for_snapshot(
                conn,
                int(closing_snapshot_id),
                initialize_schema=False,
            )
            entry_odds = trade["entry_odds"]
            if entry_odds is not None and closing_margin is not None:
                try:
                    clv_pct = margin_adjusted_clv(
                        entry_odds=float(entry_odds),
                        closing_odds=float(closing_odds),
                        closing_margin=closing_margin,
                    )
                except (TypeError, ValueError):
                    clv_pct = None
    with conn:
        conn.execute(
            """
            UPDATE paper_trade_entries
            SET closing_odds = ?,
                closing_captured_at = ?,
                closing_snapshot_id = ?,
                closing_margin = ?,
                clv_pct = ?
            WHERE paper_trade_id = ?
            """,
            (
                closing_odds,
                closing_captured_at,
                closing_snapshot_id,
                closing_margin,
                clv_pct,
                paper_trade_id,
            ),
        )


'''
replace_between(storage, "def attach_closing_snapshot(\n", "def append_trade_update(\n", new_attach)

new_clv = '''def _clv(rows: list[dict[str, Any]]) -> dict[str, Any]:
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


'''
replace_between(intelligence, "def _clv(rows: list[dict[str, Any]]) -> dict[str, Any]:\n", "def _health(", new_clv)

replace_once(
    intelligence_test,
    '        "closing_odds": 1.80,\n        "closing_snapshot_id": 10,\n',
    '        "closing_odds": 1.80,\n        "closing_snapshot_id": 10,\n        "closing_margin": 0.04,\n        "clv_pct": (2.0 / 1.80 / 1.04) - 1.0,\n',
)
replace_between(
    intelligence_test,
    "def test_clv_requires_persisted_closing_snapshot_and_rewards_better_entry_price():\n",
    "def test_buckets_are_deterministic():\n",
    '''def test_clv_requires_margin_adjusted_persisted_clv_and_complete_evidence():
    positive_clv = (2.50 / 2.00 / 1.05) - 1.0
    negative_clv = (1.80 / 2.00 / 1.05) - 1.0
    rows = [
        _row(
            paper_trade_id="positive",
            entry_odds=2.50,
            closing_odds=2.00,
            closing_snapshot_id=1,
            closing_margin=0.05,
            clv_pct=positive_clv,
        ),
        _row(
            paper_trade_id="negative",
            fixture_id="fixture-2",
            entry_odds=1.80,
            closing_odds=2.00,
            closing_snapshot_id=2,
            closing_margin=0.05,
            clv_pct=negative_clv,
        ),
        _row(
            paper_trade_id="missing-margin",
            fixture_id="fixture-3",
            entry_odds=3.00,
            closing_odds=2.00,
            closing_snapshot_id=3,
            closing_margin=None,
            clv_pct=None,
        ),
        _row(
            paper_trade_id="missing-snapshot",
            fixture_id="fixture-4",
            closing_snapshot_id=None,
            closing_margin=0.05,
            clv_pct=0.10,
        ),
    ]

    result = _clv(rows)

    assert result["sample"] == 2
    assert result["average_clv"] == pytest.approx((positive_clv + negative_clv) / 2)
    assert result["positive_rate"] == pytest.approx(0.5)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert "closing_margin" in result["method_note"]


''',
)

clv_test.write_text(
    '''from __future__ import annotations

import pytest

from sports_ai.storage.football_paper_trading import (
    PaperTradeEntry,
    StoredMarketSnapshot,
    attach_closing_snapshot,
    closing_market_margin_for_snapshot,
    connect,
    fetch_paper_trade_rows,
    initialize_database,
    margin_adjusted_clv,
    record_market_snapshot,
    record_paper_trade_entry,
)


def _snapshot(*, selection: str, odds: float, captured_at: str = "2026-09-15T10:00:00+00:00") -> StoredMarketSnapshot:
    return StoredMarketSnapshot(
        fixture_id="fixture-clv-1",
        provider="the-odds-api",
        bookmaker="pinnacle",
        market="1x2",
        selection=selection,
        odds=odds,
        line=None,
        ingestion_version="test-v1",
        provider_timestamp="2026-09-15T09:59:30+00:00",
        captured_at=captured_at,
        is_pre_match=True,
    )


def _entry() -> PaperTradeEntry:
    return PaperTradeEntry(
        prediction_id="prediction-clv-1",
        fixture_id="fixture-clv-1",
        model_snapshot_id="model-1",
        model_version="v1",
        market="1x2",
        selection="HOME",
        model_fair_price=2.00,
        available_market_price=2.20,
        decision="PAPER_CANDIDATE",
        decision_timestamp="2026-09-15T09:00:00+00:00",
        kickoff="2026-09-15T10:01:00+00:00",
        safeguard_state="PASS",
        entry_eligibility="ELIGIBLE",
        model_probability=0.50,
        bookmaker="pinnacle",
        provider="the-odds-api",
        entry_odds=2.20,
    )


def test_closing_snapshot_persists_complete_book_margin_and_spec_clv(tmp_path):
    db_path = tmp_path / "paper.sqlite"
    with connect(db_path) as conn:
        initialize_database(conn)
        home_id = record_market_snapshot(conn, _snapshot(selection="HOME", odds=2.00), initialize_schema=False)
        record_market_snapshot(conn, _snapshot(selection="DRAW", odds=3.50), initialize_schema=False)
        record_market_snapshot(conn, _snapshot(selection="AWAY", odds=4.00), initialize_schema=False)
        entry = _entry()
        record_paper_trade_entry(conn, entry, initialize_schema=False)

        expected_margin = (1 / 2.00) + (1 / 3.50) + (1 / 4.00) - 1.0
        assert closing_market_margin_for_snapshot(conn, home_id, initialize_schema=False) == pytest.approx(expected_margin)

        attach_closing_snapshot(
            conn,
            paper_trade_id=entry.paper_trade_id,
            closing_odds=2.00,
            closing_captured_at="2026-09-15T10:00:00+00:00",
            closing_snapshot_id=home_id,
            initialize_schema=False,
        )
        row = fetch_paper_trade_rows(conn, initialize_schema=False)[0]

    expected_clv = 2.20 / 2.00 / (1.0 + expected_margin) - 1.0
    assert row["closing_margin"] == pytest.approx(expected_margin)
    assert row["clv_pct"] == pytest.approx(expected_clv)
    assert margin_adjusted_clv(entry_odds=2.20, closing_odds=2.00, closing_margin=expected_margin) == pytest.approx(expected_clv)


def test_incomplete_closing_book_fails_closed_without_clv(tmp_path):
    db_path = tmp_path / "paper.sqlite"
    with connect(db_path) as conn:
        initialize_database(conn)
        home_id = record_market_snapshot(conn, _snapshot(selection="HOME", odds=2.00), initialize_schema=False)
        entry = _entry()
        record_paper_trade_entry(conn, entry, initialize_schema=False)

        assert closing_market_margin_for_snapshot(conn, home_id, initialize_schema=False) is None
        attach_closing_snapshot(
            conn,
            paper_trade_id=entry.paper_trade_id,
            closing_odds=2.00,
            closing_captured_at="2026-09-15T10:00:00+00:00",
            closing_snapshot_id=home_id,
            initialize_schema=False,
        )
        row = fetch_paper_trade_rows(conn, initialize_schema=False)[0]

    assert row["closing_odds"] == pytest.approx(2.00)
    assert row["closing_margin"] is None
    assert row["clv_pct"] is None
''',
    encoding="utf-8",
)
