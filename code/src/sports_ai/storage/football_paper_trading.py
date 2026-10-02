"""Append-oriented football paper-trading storage and settlement helpers."""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..experiments.football_day10_market_pricing import _aggregate_component_results, _component_lines, _component_result
from ..locking import canonical_hash
from ..staking import quarter_kelly_stake_fraction

PAPER_TRADE_STATE_VALUES = {
    "PENDING",
    "OPEN",
    "WON",
    "LOST",
    "PUSH",
    "HALF_WIN",
    "HALF_LOSS",
    "VOID",
    "BLOCKED",
    "UNSETTLED",
}
_TOTAL_MARKETS = {"goals_total", "corner_total"}
_FINAL_SETTLEMENT_STATES = {"WON", "LOST", "PUSH", "HALF_WIN", "HALF_LOSS", "VOID"}
_ACTIVE_SETTLEMENT_STATES = {"PENDING", "OPEN", "UNSETTLED"}
_STRIKE_RATE_STATES = {"WON", "LOST", "HALF_WIN", "HALF_LOSS"}

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS market_snapshots (
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        fixture_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        bookmaker TEXT NOT NULL,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        line REAL,
        odds REAL NOT NULL,
        provider_market_id TEXT,
        provider_timestamp TEXT,
        captured_at TEXT NOT NULL,
        is_pre_match INTEGER NOT NULL,
        ingestion_version TEXT NOT NULL,
        provider_fixture_id TEXT,
        home_team_name TEXT,
        away_team_name TEXT,
        competition_id TEXT,
        competition_name TEXT,
        kickoff_utc TEXT,
        freshness_state TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_trade_entries (
        paper_trade_id TEXT PRIMARY KEY,
        prediction_id TEXT NOT NULL,
        fixture_id TEXT NOT NULL,
        model_snapshot_id TEXT NOT NULL,
        model_version TEXT NOT NULL,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        line REAL,
        model_probability REAL,
        model_fair_price REAL NOT NULL,
        available_market_price REAL NOT NULL,
        edge REAL,
        minimum_acceptable_price REAL,
        decision TEXT NOT NULL,
        decision_timestamp TEXT NOT NULL,
        kickoff TEXT NOT NULL,
        bookmaker TEXT,
        provider TEXT,
        safeguard_state TEXT NOT NULL,
        entry_eligibility TEXT NOT NULL,
        entry_odds REAL,
        entry_captured_at TEXT,
        entry_snapshot_id INTEGER,
        closing_odds REAL,
        closing_captured_at TEXT,
        closing_snapshot_id INTEGER,
        closing_margin REAL,
        clv_pct REAL,
        staking_rule_version TEXT,
        staking_probability REAL,
        staking_decimal_price REAL,
        full_kelly_fraction REAL,
        quarter_kelly_fraction REAL,
        suggested_stake_fraction REAL,
        entry_signature TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_trade_updates (
        paper_trade_update_id INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_trade_id TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        trade_state TEXT NOT NULL,
        gross_return REAL,
        net_pnl REAL,
        notes TEXT,
        payload_json TEXT NOT NULL,
        FOREIGN KEY(paper_trade_id) REFERENCES paper_trade_entries(paper_trade_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_market_snapshots_identity_lookup
    ON market_snapshots(
        fixture_id,
        provider,
        bookmaker,
        market,
        selection,
        line,
        odds,
        provider_timestamp,
        provider_fixture_id,
        is_pre_match,
        snapshot_id DESC
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_market_snapshots_fixture_captured
    ON market_snapshots(fixture_id, captured_at, snapshot_id)
    """,
)

SQLITE_BUSY_TIMEOUT_MS = 5_000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_business_key_line(line: float | None) -> float | None:
    if line is None:
        return None
    normalized = float(line)
    return 0.0 if normalized == 0.0 else normalized


def paper_trade_business_key_signature(
    *,
    fixture_id: str,
    market: str,
    selection: str,
    line: float | None,
) -> str:
    return canonical_hash(
        {
            "fixture_id": fixture_id,
            "market": market.lower(),
            "selection": selection.upper(),
            "line": _normalize_business_key_line(line),
        }
    )


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def initialize_database(conn: sqlite3.Connection) -> None:
    journal_mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
    if journal_mode != "wal":
        raise sqlite3.OperationalError(f"expected WAL journal mode, got {journal_mode!r}")
    ensure_schema(conn)


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
    _ensure_columns(
        conn,
        table_name="market_snapshots",
        columns={
            "provider_fixture_id": "TEXT",
            "home_team_name": "TEXT",
            "away_team_name": "TEXT",
            "competition_id": "TEXT",
            "competition_name": "TEXT",
            "kickoff_utc": "TEXT",
            "freshness_state": "TEXT",
        },
    )
    _ensure_columns(
        conn,
        table_name="paper_trade_entries",
        columns={
            "business_key_signature": "TEXT",
            "canonical_business_key_signature": "TEXT",
            "closing_margin": "REAL",
            "clv_pct": "REAL",
            "staking_rule_version": "TEXT",
            "staking_probability": "REAL",
            "staking_decimal_price": "REAL",
            "full_kelly_fraction": "REAL",
            "quarter_kelly_fraction": "REAL",
            "suggested_stake_fraction": "REAL",
        },
    )
    _backfill_paper_trade_business_keys(conn)
    _ensure_paper_trade_business_key_uniqueness(conn)


def _ensure_schema_if_requested(conn: sqlite3.Connection, *, initialize_schema: bool) -> None:
    if initialize_schema:
        ensure_schema(conn)


def _ensure_columns(conn: sqlite3.Connection, *, table_name: str, columns: dict[str, str]) -> None:
    existing_columns = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    with conn:
        for column_name, column_type in columns.items():
            if column_name not in existing_columns:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _backfill_paper_trade_business_keys(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT rowid, fixture_id, market, selection, line
        FROM paper_trade_entries
        WHERE business_key_signature IS NULL
        """
    ).fetchall()
    if not rows:
        return
    with conn:
        for row in rows:
            conn.execute(
                """
                UPDATE paper_trade_entries
                SET business_key_signature = ?
                WHERE rowid = ?
                """,
                (
                    paper_trade_business_key_signature(
                        fixture_id=str(row["fixture_id"]),
                        market=str(row["market"]),
                        selection=str(row["selection"]),
                        line=None if row["line"] is None else float(row["line"]),
                    ),
                    int(row["rowid"]),
                ),
            )


def _ensure_paper_trade_business_key_uniqueness(conn: sqlite3.Connection) -> None:
    duplicate = conn.execute(
        """
        SELECT business_key_signature, COUNT(*) AS duplicate_count
        FROM paper_trade_entries
        WHERE business_key_signature IS NOT NULL
        GROUP BY business_key_signature
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate is not None:
        raise sqlite3.IntegrityError(
            "PAPER_TRADE_BUSINESS_KEY_DUPLICATES_PRESENT: reconcile duplicate paper_trade_entries before enabling the business-key uniqueness migration"
        )
    with conn:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trade_entries_business_key_signature
            ON paper_trade_entries(business_key_signature)
            WHERE business_key_signature IS NOT NULL
            """
        )


@dataclass(frozen=True)
class StoredMarketSnapshot:
    fixture_id: str
    provider: str
    bookmaker: str
    market: str
    selection: str
    odds: float
    ingestion_version: str
    line: float | None = None
    provider_market_id: str | None = None
    provider_timestamp: str | None = None
    captured_at: str = field(default_factory=_utc_now)
    is_pre_match: bool = True
    provider_fixture_id: str | None = None
    home_team_name: str | None = None
    away_team_name: str | None = None
    competition_id: str | None = None
    competition_name: str | None = None
    kickoff_utc: str | None = None
    freshness_state: str | None = None

    def __post_init__(self) -> None:
        if self.odds <= 1.0:
            raise ValueError("odds must be > 1")


@dataclass(frozen=True)
class PaperTradeEntry:
    prediction_id: str
    fixture_id: str
    model_snapshot_id: str
    model_version: str
    market: str
    selection: str
    model_fair_price: float
    available_market_price: float
    decision: str
    decision_timestamp: str
    kickoff: str
    safeguard_state: str
    entry_eligibility: str
    line: float | None = None
    model_probability: float | None = None
    edge: float | None = None
    minimum_acceptable_price: float | None = None
    bookmaker: str | None = None
    provider: str | None = None
    entry_odds: float | None = None
    entry_captured_at: str | None = None
    entry_snapshot_id: int | None = None
    closing_odds: float | None = None
    closing_captured_at: str | None = None
    closing_snapshot_id: int | None = None
    closing_margin: float | None = None
    clv_pct: float | None = None
    staking_rule_version: str | None = None
    staking_probability: float | None = None
    staking_decimal_price: float | None = None
    full_kelly_fraction: float | None = None
    quarter_kelly_fraction: float | None = None
    suggested_stake_fraction: float | None = None
    paper_trade_id: str = field(init=False)
    business_key_signature: str = field(init=False)
    entry_signature: str = field(init=False)
    canonical_business_key_signature: str | None = None
    created_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if self.model_fair_price <= 1.0:
            raise ValueError("model_fair_price must be > 1")
        if self.available_market_price <= 1.0:
            raise ValueError("available_market_price must be > 1")
        business_key_signature = paper_trade_business_key_signature(
            fixture_id=self.fixture_id,
            market=self.market,
            selection=self.selection,
            line=self.line,
        )
        signature_payload = {
            "fixture_id": self.fixture_id,
            "market": self.market.lower(),
            "selection": self.selection.upper(),
            "line": _normalize_business_key_line(self.line),
            "business_key_signature": business_key_signature,
            "prediction_id": self.prediction_id,
            "decision_timestamp": self.decision_timestamp,
        }
        signature = canonical_hash(signature_payload)
        object.__setattr__(self, "business_key_signature", business_key_signature)
        object.__setattr__(self, "entry_signature", signature)
        object.__setattr__(self, "paper_trade_id", f"paper_{business_key_signature}")
        if (
            self.staking_rule_version is None
            and self.decision == "PAPER_CANDIDATE"
            and self.entry_eligibility == "ELIGIBLE"
            and self.model_probability is not None
        ):
            stake = quarter_kelly_stake_fraction(
                probability=Decimal(str(self.model_probability)),
                decimal_price=Decimal(str(self.available_market_price)),
            )
            object.__setattr__(self, "staking_rule_version", stake.staking_rule_version)
            object.__setattr__(self, "staking_probability", float(stake.staking_probability))
            object.__setattr__(self, "staking_decimal_price", float(stake.staking_decimal_price))
            object.__setattr__(self, "full_kelly_fraction", float(stake.full_kelly_fraction))
            object.__setattr__(self, "quarter_kelly_fraction", float(stake.quarter_kelly_fraction))
            object.__setattr__(self, "suggested_stake_fraction", float(stake.suggested_stake_fraction))
        if (
            (self.decision != "PAPER_CANDIDATE" or self.entry_eligibility != "ELIGIBLE")
            and self.suggested_stake_fraction is not None
            and self.suggested_stake_fraction > 0
        ):
            raise ValueError("non-eligible paper entries cannot carry a positive suggested stake")


@dataclass(frozen=True)
class PaperTradeUpdate:
    paper_trade_id: str
    trade_state: str
    payload_json: str
    gross_return: float | None = None
    net_pnl: float | None = None
    notes: str | None = None
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if self.trade_state not in PAPER_TRADE_STATE_VALUES:
            raise ValueError(f"invalid trade_state {self.trade_state!r}")
        json.loads(self.payload_json)


def find_identical_market_snapshot(
    conn: sqlite3.Connection,
    snapshot: StoredMarketSnapshot,
    *,
    initialize_schema: bool = True,
) -> int | None:
    """Return the snapshot_id of an identical stored snapshot, or None.

    Identical means the same immutable price state for the same provider event
    and bookmaker selection: same fixture, provider, provider event id,
    bookmaker, market, selection, line, odds, and provider update timestamp.

    When ``snapshot.provider_market_id`` is provided it is ALSO part of the
    identity. Some providers (5DollarFootballAPI) encode a market *phase*
    (opening/closing/inplay) only inside ``provider_market_id`` — the
    ``market_snapshots`` table has no phase column — so without it an opening and
    a closing quote that happen to share line/odds/timestamp would wrongly
    collapse into one row. When ``provider_market_id`` is NULL the previous
    behaviour is preserved (it is not used as a filter).
    """
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    sql = """
        SELECT snapshot_id
        FROM market_snapshots
        WHERE fixture_id = ?
          AND provider = ?
          AND bookmaker = ?
          AND market = ?
          AND selection = ?
          AND line IS ?
          AND odds = ?
          AND provider_timestamp IS ?
          AND provider_fixture_id IS ?
          AND is_pre_match = ?
    """
    params: list[Any] = [
        snapshot.fixture_id,
        snapshot.provider,
        snapshot.bookmaker,
        snapshot.market,
        snapshot.selection,
        snapshot.line,
        snapshot.odds,
        snapshot.provider_timestamp,
        snapshot.provider_fixture_id,
        1 if snapshot.is_pre_match else 0,
    ]
    if snapshot.provider_market_id is not None:
        sql += " AND provider_market_id IS ?"
        params.append(snapshot.provider_market_id)
    sql += " ORDER BY snapshot_id DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return None if row is None else int(row["snapshot_id"])


def record_market_snapshot(
    conn: sqlite3.Connection,
    snapshot: StoredMarketSnapshot,
    *,
    initialize_schema: bool = True,
) -> int:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    existing = find_identical_market_snapshot(conn, snapshot, initialize_schema=False)
    if existing is not None:
        return existing
    with conn:
        result = conn.execute(
            """
            INSERT INTO market_snapshots (
                fixture_id, provider, bookmaker, market, selection, line, odds,
                provider_market_id, provider_timestamp, captured_at, is_pre_match, ingestion_version,
                provider_fixture_id, home_team_name, away_team_name, competition_id, competition_name,
                kickoff_utc, freshness_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.fixture_id,
                snapshot.provider,
                snapshot.bookmaker,
                snapshot.market,
                snapshot.selection,
                snapshot.line,
                snapshot.odds,
                snapshot.provider_market_id,
                snapshot.provider_timestamp,
                snapshot.captured_at,
                1 if snapshot.is_pre_match else 0,
                snapshot.ingestion_version,
                snapshot.provider_fixture_id,
                snapshot.home_team_name,
                snapshot.away_team_name,
                snapshot.competition_id,
                snapshot.competition_name,
                snapshot.kickoff_utc,
                snapshot.freshness_state,
            ),
        )
    return int(result.lastrowid)


def fetch_market_snapshots(
    conn: sqlite3.Connection,
    *,
    fixture_id: str | None = None,
    initialize_schema: bool = True,
) -> list[dict[str, Any]]:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    sql = """
        SELECT snapshot_id, fixture_id, provider, bookmaker, market, selection, line, odds,
               provider_market_id, provider_timestamp, captured_at, is_pre_match, ingestion_version,
               provider_fixture_id, home_team_name, away_team_name, competition_id, competition_name,
               kickoff_utc, freshness_state
        FROM market_snapshots
    """
    params: list[Any] = []
    if fixture_id is not None:
        sql += " WHERE fixture_id = ?"
        params.append(fixture_id)
    sql += " ORDER BY captured_at, snapshot_id"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def persisted_snapshot_reader(db_path: str | Path):
    """Return a lazy reader for persisted market_snapshots rows by fixture_id.

    The reader fails closed to an empty list (for example when the database or
    table has not been initialized yet) so a caller can always fall through to
    the live provider path instead of raising on a cold store.
    """

    def reader(fixture_id: str) -> list[dict[str, Any]]:
        try:
            with connect(db_path) as conn:
                return fetch_market_snapshots(
                    conn,
                    fixture_id=fixture_id,
                    initialize_schema=False,
                )
        except sqlite3.OperationalError:
            return []

    return reader


_EXPECTED_SELECTIONS_BY_MARKET = {
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


def record_paper_trade_entry(
    conn: sqlite3.Connection,
    entry: PaperTradeEntry,
    *,
    initialize_schema: bool = True,
) -> str:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    with conn:
        conn.execute(
            """
            INSERT INTO paper_trade_entries (
                paper_trade_id, prediction_id, fixture_id, model_snapshot_id, model_version, market,
                selection, line, model_probability, model_fair_price, available_market_price, edge,
                minimum_acceptable_price, decision, decision_timestamp, kickoff, bookmaker, provider,
                safeguard_state, entry_eligibility, entry_odds, entry_captured_at, entry_snapshot_id,
                closing_odds, closing_captured_at, closing_snapshot_id, closing_margin, clv_pct,
                business_key_signature, canonical_business_key_signature, staking_rule_version, staking_probability,
                staking_decimal_price, full_kelly_fraction, quarter_kelly_fraction,
                suggested_stake_fraction, entry_signature, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.paper_trade_id,
                entry.prediction_id,
                entry.fixture_id,
                entry.model_snapshot_id,
                entry.model_version,
                entry.market,
                entry.selection,
                entry.line,
                entry.model_probability,
                entry.model_fair_price,
                entry.available_market_price,
                entry.edge,
                entry.minimum_acceptable_price,
                entry.decision,
                entry.decision_timestamp,
                entry.kickoff,
                entry.bookmaker,
                entry.provider,
                entry.safeguard_state,
                entry.entry_eligibility,
                entry.entry_odds,
                entry.entry_captured_at,
                entry.entry_snapshot_id,
                entry.closing_odds,
                entry.closing_captured_at,
                entry.closing_snapshot_id,
                entry.closing_margin,
                entry.clv_pct,
                entry.business_key_signature,
                entry.canonical_business_key_signature,
                entry.staking_rule_version,
                entry.staking_probability,
                entry.staking_decimal_price,
                entry.full_kelly_fraction,
                entry.quarter_kelly_fraction,
                entry.suggested_stake_fraction,
                entry.entry_signature,
                entry.created_at,
            ),
        )
    return entry.paper_trade_id


def find_existing_paper_trade_entry(
    conn: sqlite3.Connection,
    *,
    prediction_id: str | None,
    fixture_id: str,
    market: str,
    selection: str,
    line: float | None,
    initialize_schema: bool = True,
) -> dict[str, Any] | None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    business_key_signature = paper_trade_business_key_signature(
        fixture_id=fixture_id,
        market=market,
        selection=selection,
        line=line,
    )
    row = conn.execute(
        """
        SELECT *
        FROM paper_trade_entries
        WHERE business_key_signature = ?
        ORDER BY created_at, paper_trade_id
        LIMIT 1
        """,
        (business_key_signature,),
    ).fetchone()
    return None if row is None else dict(row)


def attach_closing_snapshot(
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


def append_trade_update(
    conn: sqlite3.Connection,
    update: PaperTradeUpdate,
    *,
    initialize_schema: bool = True,
) -> int:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    with conn:
        result = conn.execute(
            """
            INSERT INTO paper_trade_updates (
                paper_trade_id, updated_at, trade_state, gross_return, net_pnl, notes, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                update.paper_trade_id,
                update.updated_at,
                update.trade_state,
                update.gross_return,
                update.net_pnl,
                update.notes,
                update.payload_json,
            ),
        )
    return int(result.lastrowid)


def fetch_paper_trade_rows(conn: sqlite3.Connection, *, initialize_schema: bool = True) -> list[dict[str, Any]]:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    entries = conn.execute(
        """
        SELECT *
        FROM paper_trade_entries
        ORDER BY decision_timestamp, paper_trade_id
        """
    ).fetchall()
    updates_by_trade_id: dict[str, list[sqlite3.Row]] = {}
    updates = conn.execute(
        """
        SELECT paper_trade_id, updated_at, trade_state, gross_return, net_pnl, notes, payload_json, paper_trade_update_id
        FROM paper_trade_updates
        ORDER BY paper_trade_id, updated_at, paper_trade_update_id
        """
    ).fetchall()
    for update in updates:
        updates_by_trade_id.setdefault(str(update["paper_trade_id"]), []).append(update)
    merged_rows: list[dict[str, Any]] = []
    for entry in entries:
        merged = dict(entry)
        merged["trade_state"] = "PENDING"
        merged["gross_return"] = None
        merged["net_pnl"] = None
        merged["update_notes"] = None
        merged["latest_update_payload"] = None
        for update in updates_by_trade_id.get(str(entry["paper_trade_id"]), []):
            merged["trade_state"] = update["trade_state"]
            merged["gross_return"] = update["gross_return"]
            merged["net_pnl"] = update["net_pnl"]
            merged["update_notes"] = update["notes"]
            merged["latest_update_payload"] = json.loads(str(update["payload_json"]))
            merged["updated_at"] = update["updated_at"]
        merged["price_clv"] = None
        merged["implied_probability_clv"] = None
        entry_odds = merged.get("entry_odds")
        closing_odds = merged.get("closing_odds")
        if entry_odds and closing_odds and float(entry_odds) > 1.0 and float(closing_odds) > 1.0:
            merged["price_clv"] = float(entry_odds) - float(closing_odds)
            merged["implied_probability_clv"] = (1.0 / float(closing_odds)) - (1.0 / float(entry_odds))
        merged_rows.append(merged)
    return merged_rows


def settlement_state_for_market(
    *,
    market: str,
    selection: str,
    line: float | None,
    actual_home_goals: int,
    actual_away_goals: int,
    actual_total_corners: int | None = None,
) -> str:
    market_key = market.lower()
    selection_key = selection.upper()
    if market_key == "1x2":
        if actual_home_goals > actual_away_goals:
            winner = "HOME"
        elif actual_home_goals < actual_away_goals:
            winner = "AWAY"
        else:
            winner = "DRAW"
        return "WON" if selection_key == winner else "LOST"
    if market_key == "btts":
        btts = actual_home_goals >= 1 and actual_away_goals >= 1
        if (selection_key == "YES" and btts) or (selection_key == "NO" and not btts):
            return "WON"
        return "LOST"
    if market_key in _TOTAL_MARKETS:
        if line is None:
            raise ValueError("totals settlement requires a line")
        observed_total = (
            actual_home_goals + actual_away_goals
            if market_key == "goals_total"
            else actual_total_corners
        )
        if observed_total is None:
            raise ValueError("corner total settlement requires actual_total_corners")
        return _split_line_state(
            observed_total=float(observed_total),
            selection=selection_key,
            line=line,
        )
    if market_key == "asian_handicap":
        if line is None:
            raise ValueError("asian handicap settlement requires a line")
        return _split_line_state(
            observed_total=float(actual_home_goals - actual_away_goals),
            selection=selection_key,
            line=line,
            handicap_mode=True,
        )
    raise ValueError(f"unsupported market {market!r}")


def _split_line_state(
    *,
    observed_total: float,
    selection: str,
    line: float,
    handicap_mode: bool = False,
) -> str:
    selection_multiplier = 1.0
    if handicap_mode and selection == "AWAY":
        selection_multiplier = -1.0
    elif not handicap_mode and selection == "UNDER":
        selection_multiplier = -1.0
    component_results: list[str] = []
    for component_line in _component_lines(line):
        if handicap_mode:
            adjusted = (observed_total * selection_multiplier) + (float(component_line) * selection_multiplier)
        else:
            adjusted = (observed_total - float(component_line)) * selection_multiplier
        component_results.append(_component_result(adjusted))
    aggregated = _aggregate_component_results(component_results)
    return {
        "full_win": "WON",
        "half_win": "HALF_WIN",
        "push": "PUSH",
        "half_loss": "HALF_LOSS",
        "full_loss": "LOST",
    }[aggregated]


def profit_for_state(*, state: str, odds: float, stake: float = 1.0) -> tuple[float, float]:
    if odds <= 1.0:
        raise ValueError("odds must be > 1")
    if state == "WON":
        gross_return = stake * odds
        return gross_return, gross_return - stake
    if state == "LOST":
        return 0.0, -stake
    if state == "PUSH":
        return stake, 0.0
    if state == "HALF_WIN":
        gross_return = (stake / 2.0) * odds + (stake / 2.0)
        return gross_return, gross_return - stake
    if state == "HALF_LOSS":
        return stake / 2.0, -stake / 2.0
    if state in {"VOID", "BLOCKED", "PENDING", "OPEN", "UNSETTLED"}:
        return 0.0, 0.0
    raise ValueError(f"unsupported state {state!r}")


def build_trade_update_payload(
    *,
    paper_trade_id: str,
    trade_state: str,
    settlement_context: dict[str, Any],
    odds: float,
    stake: float = 1.0,
    notes: str | None = None,
) -> PaperTradeUpdate:
    gross_return, net_pnl = profit_for_state(state=trade_state, odds=odds, stake=stake)
    payload = {
        "paper_trade_id": paper_trade_id,
        "trade_state": trade_state,
        "stake": stake,
        "gross_return": gross_return,
        "net_pnl": net_pnl,
        "settlement_context": settlement_context,
    }
    return PaperTradeUpdate(
        paper_trade_id=paper_trade_id,
        trade_state=trade_state,
        gross_return=gross_return,
        net_pnl=net_pnl,
        notes=notes,
        payload_json=json.dumps(payload, sort_keys=True),
    )


def _stake_units_for_row(row: dict[str, Any]) -> float:
    payload = row.get("latest_update_payload") or {}
    stake = payload.get("stake")
    try:
        return 1.0 if stake is None else float(stake)
    except (TypeError, ValueError):
        return 1.0


def _strike_rate(rows: list[dict[str, Any]]) -> float | None:
    decision_rows = [row for row in rows if row.get("trade_state") in _STRIKE_RATE_STATES]
    if not decision_rows:
        return None
    weighted_wins = sum(
        1.0 if row.get("trade_state") == "WON" else 0.5 if row.get("trade_state") == "HALF_WIN" else 0.0
        for row in decision_rows
    )
    return weighted_wins / len(decision_rows)


def paper_trading_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    performance = paper_trading_performance(rows)
    return {
        "paper_bets": len(rows),
        "open_positions": performance["open_positions"],
        "settled_positions": performance["settled_positions"],
        "cumulative_pnl": performance["net_units"],
        "roi": performance["roi"],
        "clv_state": performance["clv_state"],
        "ledger_state": "READY",
        "performance": performance,
    }


def paper_trading_performance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    open_rows = [row for row in rows if row.get("trade_state") in _ACTIVE_SETTLEMENT_STATES]
    settled_rows = [row for row in rows if row.get("trade_state") in _FINAL_SETTLEMENT_STATES]
    settled_stake_units = sum(_stake_units_for_row(row) for row in settled_rows)
    net_units = sum(float(row.get("net_pnl") or 0.0) for row in settled_rows)
    average_odds = (
        sum(float(row["entry_odds"]) for row in rows if row.get("entry_odds")) / len([row for row in rows if row.get("entry_odds")])
        if any(row.get("entry_odds") for row in rows)
        else None
    )
    break_even_rate = (
        sum(1.0 / float(row["entry_odds"]) for row in rows if row.get("entry_odds")) / len([row for row in rows if row.get("entry_odds")])
        if any(row.get("entry_odds") for row in rows)
        else None
    )
    weighted_wins = sum(
        1.0 if row.get("trade_state") == "WON" else 0.5 if row.get("trade_state") == "HALF_WIN" else 0.0
        for row in settled_rows
    )
    clv_rows = [row for row in rows if row.get("implied_probability_clv") is not None]
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in settled_rows:
        cumulative += float(row.get("net_pnl") or 0.0)
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    by_market: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_market.setdefault(str(row["market"]), []).append(row)
    market_summary = {
        market: {
            "positions": len(market_rows),
            "settled_positions": len([row for row in market_rows if row.get("trade_state") in _FINAL_SETTLEMENT_STATES]),
            "open_positions": len([row for row in market_rows if row.get("trade_state") in _ACTIVE_SETTLEMENT_STATES]),
            "pending_positions": len([row for row in market_rows if row.get("trade_state") in _ACTIVE_SETTLEMENT_STATES]),
            "wins": sum(1 for row in market_rows if row.get("trade_state") == "WON"),
            "losses": sum(1 for row in market_rows if row.get("trade_state") == "LOST"),
            "pushes": sum(1 for row in market_rows if row.get("trade_state") == "PUSH"),
            "half_wins": sum(1 for row in market_rows if row.get("trade_state") == "HALF_WIN"),
            "half_losses": sum(1 for row in market_rows if row.get("trade_state") == "HALF_LOSS"),
            "voids": sum(1 for row in market_rows if row.get("trade_state") == "VOID"),
            "hit_rate": None
            if not [row for row in market_rows if row.get("trade_state") in _FINAL_SETTLEMENT_STATES]
            else sum(
                1.0 if row.get("trade_state") == "WON" else 0.5 if row.get("trade_state") == "HALF_WIN" else 0.0
                for row in market_rows
                if row.get("trade_state") in _FINAL_SETTLEMENT_STATES
            )
            / len([row for row in market_rows if row.get("trade_state") in _FINAL_SETTLEMENT_STATES]),
            "average_odds": (
                sum(float(row["entry_odds"]) for row in market_rows if row.get("entry_odds"))
                / len([row for row in market_rows if row.get("entry_odds")])
                if any(row.get("entry_odds") for row in market_rows)
                else None
            ),
            "break_even_rate": (
                sum(1.0 / float(row["entry_odds"]) for row in market_rows if row.get("entry_odds"))
                / len([row for row in market_rows if row.get("entry_odds")])
                if any(row.get("entry_odds") for row in market_rows)
                else None
            ),
            "net_units": sum(float(row.get("net_pnl") or 0.0) for row in market_rows if row.get("trade_state") in _FINAL_SETTLEMENT_STATES),
            "roi": (
                sum(float(row.get("net_pnl") or 0.0) for row in market_rows if row.get("trade_state") in _FINAL_SETTLEMENT_STATES)
                / sum(
                    _stake_units_for_row(row)
                    for row in market_rows
                    if row.get("trade_state") in _FINAL_SETTLEMENT_STATES
                )
                if sum(
                    _stake_units_for_row(row)
                    for row in market_rows
                    if row.get("trade_state") in _FINAL_SETTLEMENT_STATES
                )
                else None
            ),
            "strike_rate": _strike_rate(market_rows),
            "settled_stake_units": sum(
                _stake_units_for_row(row)
                for row in market_rows
                if row.get("trade_state") in _FINAL_SETTLEMENT_STATES
            ),
            "average_clv": (
                sum(float(row["implied_probability_clv"]) for row in market_rows if row.get("implied_probability_clv") is not None)
                / len([row for row in market_rows if row.get("implied_probability_clv") is not None])
                if any(row.get("implied_probability_clv") is not None for row in market_rows)
                else None
            ),
        }
        for market, market_rows in sorted(by_market.items())
    }
    if not rows:
        sample_status = "NO_CURRENT_PAPER_POSITIONS_YET"
    elif len(settled_rows) < 5:
        sample_status = "INSUFFICIENT_SAMPLE"
    else:
        sample_status = "TRACKING"
    return {
        "positions": len(rows),
        "open_positions": len(open_rows),
        "pending_positions": len(open_rows),
        "settled_positions": len(settled_rows),
        "wins": sum(1 for row in settled_rows if row.get("trade_state") == "WON"),
        "losses": sum(1 for row in settled_rows if row.get("trade_state") == "LOST"),
        "pushes": sum(1 for row in settled_rows if row.get("trade_state") == "PUSH"),
        "half_wins": sum(1 for row in settled_rows if row.get("trade_state") == "HALF_WIN"),
        "half_losses": sum(1 for row in settled_rows if row.get("trade_state") == "HALF_LOSS"),
        "voids": sum(1 for row in settled_rows if row.get("trade_state") == "VOID"),
        "hit_rate": None if not settled_rows else (weighted_wins / len(settled_rows)),
        "strike_rate": _strike_rate(rows),
        "average_odds": average_odds,
        "break_even_rate": break_even_rate,
        "settled_stake_units": settled_stake_units,
        "net_units": net_units,
        "roi": None if settled_stake_units <= 0 else (net_units / settled_stake_units),
        "yield": None if settled_stake_units <= 0 else (net_units / settled_stake_units),
        "max_drawdown": max_drawdown,
        "clv_state": "PENDING" if rows and not clv_rows else ("AVAILABLE" if clv_rows else "UNAVAILABLE"),
        "average_clv": None
        if not clv_rows
        else sum(float(row["implied_probability_clv"]) for row in clv_rows) / len(clv_rows),
        "sample_status": sample_status,
        "by_market": market_summary,
    }


def entry_as_dict(entry: PaperTradeEntry) -> dict[str, Any]:
    return asdict(entry)
