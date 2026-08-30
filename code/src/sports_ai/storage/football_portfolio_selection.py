"""Append-only storage for versioned football portfolio selection evaluations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..dashboard.portfolio_selection import PortfolioDisposition, PortfolioPolicy
from ..locking import canonical_hash

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS football_portfolio_evaluations (
        evaluation_id TEXT PRIMARY KEY,
        canonical_fixture_id TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        effective_from TEXT NOT NULL,
        lifecycle_stage TEXT NOT NULL,
        evaluated_at TEXT NOT NULL,
        candidate_snapshot_at TEXT NOT NULL,
        candidate_count INTEGER NOT NULL,
        selected_count INTEGER NOT NULL,
        evaluation_signature TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_football_portfolio_evaluations_fixture_policy
    ON football_portfolio_evaluations(
        canonical_fixture_id,
        policy_id,
        policy_version,
        candidate_snapshot_at,
        candidate_count,
        created_at
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_football_portfolio_evaluations_fixture_policy_recency
    ON football_portfolio_evaluations(
        canonical_fixture_id,
        policy_id,
        policy_version,
        evaluated_at,
        created_at,
        evaluation_id
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS football_portfolio_selections (
        portfolio_selection_id TEXT PRIMARY KEY,
        evaluation_id TEXT NOT NULL,
        paper_trade_id TEXT NOT NULL,
        canonical_fixture_id TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        effective_from TEXT NOT NULL,
        lifecycle_stage TEXT NOT NULL,
        evaluated_at TEXT NOT NULL,
        selection_state TEXT NOT NULL,
        selection_rank INTEGER,
        selection_reason TEXT NOT NULL,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        line REAL,
        edge_at_selection REAL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(evaluation_id) REFERENCES football_portfolio_evaluations(evaluation_id),
        UNIQUE(evaluation_id, paper_trade_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_football_portfolio_selections_trade_policy
    ON football_portfolio_selections(paper_trade_id, policy_id, policy_version)
    """,
)


@dataclass(frozen=True)
class PortfolioEvaluationWriteResult:
    evaluation_id: str
    inserted: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)


def _ensure_schema_if_requested(conn: sqlite3.Connection, *, initialize_schema: bool) -> None:
    if initialize_schema:
        ensure_schema(conn)


CURRENT_EVALUATION_IS_NEWER_CLAUSE = """
                AND (
                    newer.evaluated_at > e.evaluated_at
                    OR (
                        newer.evaluated_at = e.evaluated_at
                        AND newer.created_at > e.created_at
                    )
                    OR (
                        newer.evaluated_at = e.evaluated_at
                        AND newer.created_at = e.created_at
                        AND newer.evaluation_id > e.evaluation_id
                    )
                )
"""


def portfolio_evaluation_signature(
    *,
    policy: PortfolioPolicy,
    canonical_fixture_id: str,
    candidates: list[dict[str, Any]],
) -> str:
    normalized_candidates = [
        {
            "paper_trade_id": str(candidate["paper_trade_id"]),
            "market": str(candidate["market"]),
            "selection": str(candidate["selection"]),
            "line": None if candidate.get("line") is None else float(candidate["line"]),
            "edge": None if candidate.get("edge_at_selection") is None else float(candidate["edge_at_selection"]),
            "decision_timestamp": str(candidate["decision_timestamp"]),
            "created_at": str(candidate["created_at"]),
            "safeguard_state": str(candidate.get("safeguard_state") or ""),
        }
        for candidate in sorted(candidates, key=lambda item: str(item["paper_trade_id"]))
    ]
    return canonical_hash(
        {
            "canonical_fixture_id": canonical_fixture_id,
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "effective_from": policy.effective_from,
            "lifecycle_stage": policy.lifecycle_stage,
            "candidates": normalized_candidates,
        }
    )


def record_portfolio_evaluation(
    conn: sqlite3.Connection,
    *,
    policy: PortfolioPolicy,
    canonical_fixture_id: str,
    evaluated_at: str,
    candidate_snapshot_at: str,
    evaluation_signature: str,
    dispositions: list[PortfolioDisposition],
    initialize_schema: bool = True,
) -> PortfolioEvaluationWriteResult:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    created_at = _utc_now()
    evaluation_id = "portfolio_eval_" + canonical_hash(
        {
            "canonical_fixture_id": canonical_fixture_id,
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "effective_from": policy.effective_from,
            "evaluation_signature": evaluation_signature,
        }
    )
    selected_count = sum(1 for item in dispositions if item.selection_state == "SELECTED")
    with conn:
        result = conn.execute(
            """
            INSERT OR IGNORE INTO football_portfolio_evaluations (
                evaluation_id,
                canonical_fixture_id,
                policy_id,
                policy_version,
                effective_from,
                lifecycle_stage,
                evaluated_at,
                candidate_snapshot_at,
                candidate_count,
                selected_count,
                evaluation_signature,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation_id,
                canonical_fixture_id,
                policy.policy_id,
                policy.policy_version,
                policy.effective_from,
                policy.lifecycle_stage,
                evaluated_at,
                candidate_snapshot_at,
                len(dispositions),
                selected_count,
                evaluation_signature,
                created_at,
            ),
        )
        inserted = int(result.rowcount or 0) > 0
        if inserted:
            for disposition in dispositions:
                conn.execute(
                    """
                    INSERT INTO football_portfolio_selections (
                        portfolio_selection_id,
                        evaluation_id,
                        paper_trade_id,
                        canonical_fixture_id,
                        policy_id,
                        policy_version,
                        effective_from,
                        lifecycle_stage,
                        evaluated_at,
                        selection_state,
                        selection_rank,
                        selection_reason,
                        market,
                        selection,
                        line,
                        edge_at_selection,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "portfolio_sel_" + canonical_hash({"evaluation_id": evaluation_id, "paper_trade_id": disposition.paper_trade_id}),
                        evaluation_id,
                        disposition.paper_trade_id,
                        disposition.canonical_fixture_id,
                        policy.policy_id,
                        policy.policy_version,
                        policy.effective_from,
                        policy.lifecycle_stage,
                        evaluated_at,
                        disposition.selection_state,
                        disposition.selection_rank,
                        disposition.selection_reason,
                        disposition.market,
                        disposition.selection,
                        disposition.line,
                        disposition.edge_at_selection,
                        created_at,
                    ),
                )
    return PortfolioEvaluationWriteResult(evaluation_id=evaluation_id, inserted=inserted)


def fetch_current_portfolio_selections(
    conn: sqlite3.Connection,
    *,
    policy_id: str,
    policy_version: str,
    initialize_schema: bool = True,
) -> list[dict[str, Any]]:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    rows = conn.execute(
        f"""
        SELECT s.*, e.candidate_snapshot_at, e.candidate_count, e.evaluation_signature
        FROM football_portfolio_selections s
        JOIN football_portfolio_evaluations e ON e.evaluation_id = s.evaluation_id
        WHERE e.policy_id = ?
          AND e.policy_version = ?
          AND NOT EXISTS (
              SELECT 1
              FROM football_portfolio_evaluations newer
              WHERE newer.canonical_fixture_id = e.canonical_fixture_id
                AND newer.policy_id = e.policy_id
                AND newer.policy_version = e.policy_version
{CURRENT_EVALUATION_IS_NEWER_CLAUSE}
          )
        ORDER BY e.canonical_fixture_id, s.selection_rank, s.paper_trade_id
        """,
        (policy_id, policy_version),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_current_portfolio_evaluations(
    conn: sqlite3.Connection,
    *,
    policy_id: str,
    policy_version: str,
    initialize_schema: bool = True,
) -> list[dict[str, Any]]:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    rows = conn.execute(
        f"""
        SELECT e.*
        FROM football_portfolio_evaluations e
        WHERE e.policy_id = ?
          AND e.policy_version = ?
          AND NOT EXISTS (
              SELECT 1
              FROM football_portfolio_evaluations newer
              WHERE newer.canonical_fixture_id = e.canonical_fixture_id
                AND newer.policy_id = e.policy_id
                AND newer.policy_version = e.policy_version
{CURRENT_EVALUATION_IS_NEWER_CLAUSE}
          )
        ORDER BY e.canonical_fixture_id
        """,
        (policy_id, policy_version),
    ).fetchall()
    return [dict(row) for row in rows]
