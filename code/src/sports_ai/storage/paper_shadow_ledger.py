"""Append-only paper/shadow ledger for trial decisions and later grading.

This module gives the project a concrete place to record:
1. the original pre-event decision
2. later close/settlement updates
3. the current merged view used for evaluation

It is intentionally local and lightweight for the current phase. The final
production design should move the same concepts into the secured Postgres
architecture.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..evaluation.clv import clv_record

DECISION_VALUES = {"PASS", "WATCH", "BET_CANDIDATE", "PAPER_SHADOW_CANDIDATE", "RESEARCH_SIGNAL"}
RESULT_STATUS_VALUES = {"PENDING", "SETTLED", "VOID", "CANCELLED"}
LOCK_STAGE_VALUES = {"prediction", "market"}

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS paper_shadow_decisions (
        decision_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        run_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        sport TEXT NOT NULL,
        competition TEXT NOT NULL,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        line TEXT,
        available_price REAL NOT NULL,
        bookmaker TEXT NOT NULL,
        fair_probability REAL NOT NULL,
        fair_odds REAL NOT NULL,
        market_probability REAL,
        edge REAL,
        data_quality REAL,
        uncertainty REAL,
        decision TEXT NOT NULL,
        decision_reason TEXT NOT NULL,
        model_version TEXT NOT NULL,
        strategy_version TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        result_status TEXT NOT NULL,
        notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_shadow_outcome_updates (
        outcome_update_id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        result_status TEXT NOT NULL,
        closing_price REAL,
        settlement TEXT,
        clv_pct REAL,
        notes TEXT,
        FOREIGN KEY(decision_id) REFERENCES paper_shadow_decisions(decision_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_shadow_lock_rows (
        lock_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        stage TEXT NOT NULL,
        event_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        prev_hash TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_shadow_market_snapshots (
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at TEXT NOT NULL,
        run_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        bookmaker TEXT NOT NULL,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        line TEXT,
        decimal_odds REAL NOT NULL,
        source_kind TEXT NOT NULL,
        source_ref TEXT NOT NULL
    )
    """,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class PaperDecision:
    run_id: str
    event_id: str
    sport: str
    competition: str
    market: str
    selection: str
    available_price: float
    bookmaker: str
    fair_probability: float
    fair_odds: float
    decision: str
    decision_reason: str
    model_version: str
    strategy_version: str
    policy_version: str
    line: str | None = None
    market_probability: float | None = None
    edge: float | None = None
    data_quality: float | None = None
    uncertainty: float | None = None
    notes: str | None = None
    result_status: str = "PENDING"
    decision_id: str = field(default_factory=lambda: f"dec_{uuid4().hex}")
    created_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if self.decision not in DECISION_VALUES:
            raise ValueError(f"invalid decision {self.decision!r}")
        if self.result_status not in RESULT_STATUS_VALUES:
            raise ValueError(f"invalid result_status {self.result_status!r}")
        if not (0 < self.fair_probability < 1):
            raise ValueError("fair_probability must be between 0 and 1")
        if self.market_probability is not None and not (0 < self.market_probability < 1):
            raise ValueError("market_probability must be between 0 and 1")
        if self.available_price <= 1:
            raise ValueError("available_price must be > 1")
        if self.fair_odds <= 1:
            raise ValueError("fair_odds must be > 1")
        if self.uncertainty is not None and not (0 <= self.uncertainty <= 1):
            raise ValueError("uncertainty must be between 0 and 1")


@dataclass(frozen=True)
class OutcomeUpdate:
    decision_id: str
    result_status: str
    closing_price: float | None = None
    settlement: str | None = None
    notes: str | None = None
    updated_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if self.result_status not in RESULT_STATUS_VALUES:
            raise ValueError(f"invalid result_status {self.result_status!r}")
        if self.closing_price is not None and self.closing_price <= 1:
            raise ValueError("closing_price must be > 1")


@dataclass(frozen=True)
class LockRow:
    stage: str
    event_id: str
    run_id: str
    payload_json: str
    payload_hash: str
    prev_hash: str | None = None
    created_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if self.stage not in LOCK_STAGE_VALUES:
            raise ValueError(f"invalid lock stage {self.stage!r}")
        if not self.payload_hash:
            raise ValueError("payload_hash is required")
        json.loads(self.payload_json)


@dataclass(frozen=True)
class MarketSnapshot:
    run_id: str
    event_id: str
    bookmaker: str
    market: str
    selection: str
    decimal_odds: float
    source_kind: str
    source_ref: str
    line: str | None = None
    captured_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if self.decimal_odds <= 1:
            raise ValueError("decimal_odds must be > 1")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the local paper/shadow ledger."""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create required tables."""

    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)


def record_decision(conn: sqlite3.Connection, decision: PaperDecision) -> str:
    """Insert one immutable decision row."""

    ensure_schema(conn)
    with conn:
        conn.execute(
            """
            INSERT INTO paper_shadow_decisions (
                decision_id, created_at, run_id, event_id, sport, competition,
                market, selection, line, available_price, bookmaker,
                fair_probability, fair_odds, market_probability, edge,
                data_quality, uncertainty, decision, decision_reason,
                model_version, strategy_version, policy_version,
                result_status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.decision_id,
                decision.created_at,
                decision.run_id,
                decision.event_id,
                decision.sport,
                decision.competition,
                decision.market,
                decision.selection,
                decision.line,
                decision.available_price,
                decision.bookmaker,
                decision.fair_probability,
                decision.fair_odds,
                decision.market_probability,
                decision.edge,
                decision.data_quality,
                decision.uncertainty,
                decision.decision,
                decision.decision_reason,
                decision.model_version,
                decision.strategy_version,
                decision.policy_version,
                decision.result_status,
                decision.notes,
            ),
        )
    return decision.decision_id


def append_outcome_update(conn: sqlite3.Connection, update: OutcomeUpdate) -> dict[str, Any]:
    """Append a later close/settlement update without touching the original row."""

    ensure_schema(conn)
    base_row = conn.execute(
        """
        SELECT decision_id, available_price
        FROM paper_shadow_decisions
        WHERE decision_id = ?
        """,
        (update.decision_id,),
    ).fetchone()
    if base_row is None:
        raise KeyError(f"unknown decision_id {update.decision_id!r}")

    clv_pct = None
    if update.closing_price is not None:
        clv_pct = clv_record(base_row["available_price"], update.closing_price)["clv_pct"]

    with conn:
        result = conn.execute(
            """
            INSERT INTO paper_shadow_outcome_updates (
                decision_id, updated_at, result_status, closing_price,
                settlement, clv_pct, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                update.decision_id,
                update.updated_at,
                update.result_status,
                update.closing_price,
                update.settlement,
                clv_pct,
                update.notes,
            ),
        )

    return {
        "decision_id": update.decision_id,
        "outcome_update_id": result.lastrowid,
        "result_status": update.result_status,
        "clv_pct": clv_pct,
    }


def record_lock_row(conn: sqlite3.Connection, lock_row: LockRow) -> int:
    """Persist one prediction or market lock row."""

    ensure_schema(conn)
    with conn:
        result = conn.execute(
            """
            INSERT INTO paper_shadow_lock_rows (
                created_at, stage, event_id, run_id, payload_json, payload_hash, prev_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lock_row.created_at,
                lock_row.stage,
                lock_row.event_id,
                lock_row.run_id,
                lock_row.payload_json,
                lock_row.payload_hash,
                lock_row.prev_hash,
            ),
        )
    return int(result.lastrowid)


def fetch_lock_rows(
    conn: sqlite3.Connection,
    *,
    event_id: str | None = None,
    run_id: str | None = None,
    stage: str | None = None,
) -> list[dict[str, Any]]:
    """Return stored prediction/market lock rows."""

    ensure_schema(conn)
    where_parts: list[str] = []
    params: list[Any] = []
    if event_id is not None:
        where_parts.append("event_id = ?")
        params.append(event_id)
    if run_id is not None:
        where_parts.append("run_id = ?")
        params.append(run_id)
    if stage is not None:
        where_parts.append("stage = ?")
        params.append(stage)

    sql = """
        SELECT lock_row_id, created_at, stage, event_id, run_id, payload_json, payload_hash, prev_hash
        FROM paper_shadow_lock_rows
    """
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    sql += " ORDER BY created_at, lock_row_id"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def record_market_snapshot(conn: sqlite3.Connection, snapshot: MarketSnapshot) -> int:
    """Persist one append-only market price observation."""

    ensure_schema(conn)
    with conn:
        result = conn.execute(
            """
            INSERT INTO paper_shadow_market_snapshots (
                captured_at, run_id, event_id, bookmaker, market, selection, line,
                decimal_odds, source_kind, source_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.captured_at,
                snapshot.run_id,
                snapshot.event_id,
                snapshot.bookmaker,
                snapshot.market,
                snapshot.selection,
                snapshot.line,
                snapshot.decimal_odds,
                snapshot.source_kind,
                snapshot.source_ref,
            ),
        )
    return int(result.lastrowid)


def fetch_market_snapshots(
    conn: sqlite3.Connection,
    *,
    event_id: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return stored market snapshots."""

    ensure_schema(conn)
    where_parts: list[str] = []
    params: list[Any] = []
    if event_id is not None:
        where_parts.append("event_id = ?")
        params.append(event_id)
    if run_id is not None:
        where_parts.append("run_id = ?")
        params.append(run_id)

    sql = """
        SELECT snapshot_id, captured_at, run_id, event_id, bookmaker, market, selection, line,
               decimal_odds, source_kind, source_ref
        FROM paper_shadow_market_snapshots
    """
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    sql += " ORDER BY captured_at, snapshot_id"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def fetch_ledger_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return the current merged ledger view using the latest appended update."""

    ensure_schema(conn)
    decision_rows = conn.execute(
        """
        SELECT *
        FROM paper_shadow_decisions
        ORDER BY created_at, decision_id
        """
    ).fetchall()

    merged_rows: list[dict[str, Any]] = []
    for row in decision_rows:
        merged = dict(row)
        updates = conn.execute(
            """
            SELECT updated_at, result_status, closing_price, settlement, clv_pct, notes
            FROM paper_shadow_outcome_updates
            WHERE decision_id = ?
            ORDER BY updated_at ASC, outcome_update_id ASC
            """,
            (row["decision_id"],),
        ).fetchall()

        merged["closing_price"] = None
        merged["settlement"] = None
        merged["clv"] = None
        merged["outcome_updated_at"] = None
        merged["outcome_notes"] = None

        for update in updates:
            merged["result_status"] = update["result_status"]
            merged["outcome_updated_at"] = update["updated_at"]
            if update["closing_price"] is not None:
                merged["closing_price"] = update["closing_price"]
            if update["settlement"] is not None:
                merged["settlement"] = update["settlement"]
            if update["clv_pct"] is not None:
                merged["clv"] = update["clv_pct"]
            if update["notes"] is not None:
                merged["outcome_notes"] = update["notes"]

        merged_rows.append(merged)

    return merged_rows


def decision_as_dict(decision: PaperDecision) -> dict[str, Any]:
    """Expose a serializable shape for logging or debugging."""

    return asdict(decision)
