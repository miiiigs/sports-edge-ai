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
        evaluation_signature TEXT NOT NULL,
        predecessor_evaluation_id TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(predecessor_evaluation_id) REFERENCES football_portfolio_evaluations(evaluation_id)
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
    CREATE UNIQUE INDEX IF NOT EXISTS idx_football_portfolio_evaluations_lifecycle_event
    ON football_portfolio_evaluations(
        canonical_fixture_id,
        policy_id,
        policy_version,
        ifnull(predecessor_evaluation_id, ''),
        evaluation_signature
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
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def ensure_schema(conn: sqlite3.Connection) -> None:
    if _needs_schema_rebuild(conn):
        _migrate_schema(conn)
    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _table_sql(conn: sqlite3.Connection, table_name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return "" if row is None or row["sql"] is None else str(row["sql"])


def _needs_schema_rebuild(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "football_portfolio_evaluations"):
        return False
    columns = _table_columns(conn, "football_portfolio_evaluations")
    table_sql = _table_sql(conn, "football_portfolio_evaluations")
    return "predecessor_evaluation_id" not in columns or "evaluation_signature TEXT NOT NULL UNIQUE" in table_sql


def _migrate_schema(conn: sqlite3.Connection) -> None:
    legacy_evaluations = "football_portfolio_evaluations__portfolio_lifecycle_legacy"
    legacy_selections = "football_portfolio_selections__portfolio_lifecycle_legacy"
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            if _table_exists(conn, "football_portfolio_selections"):
                conn.execute(f"ALTER TABLE football_portfolio_selections RENAME TO {legacy_selections}")
            if _table_exists(conn, "football_portfolio_evaluations"):
                conn.execute(f"ALTER TABLE football_portfolio_evaluations RENAME TO {legacy_evaluations}")
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            _copy_legacy_evaluations(conn, table_name=legacy_evaluations)
            _copy_legacy_selections(conn, table_name=legacy_selections)
            if _table_exists(conn, legacy_selections):
                conn.execute(f"DROP TABLE {legacy_selections}")
            if _table_exists(conn, legacy_evaluations):
                conn.execute(f"DROP TABLE {legacy_evaluations}")
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        conn.execute("PRAGMA foreign_keys = ON")
        raise


def _copy_legacy_evaluations(conn: sqlite3.Connection, *, table_name: str) -> None:
    if not _table_exists(conn, table_name):
        return
    rows = conn.execute(
        f"""
        SELECT
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
        FROM {table_name}
        ORDER BY
            canonical_fixture_id,
            policy_id,
            policy_version,
            evaluated_at,
            created_at,
            evaluation_id
        """
    ).fetchall()
    predecessor_by_scope: dict[tuple[str, str, str], str | None] = {}
    for row in rows:
        scope = (
            str(row["canonical_fixture_id"]),
            str(row["policy_id"]),
            str(row["policy_version"]),
        )
        predecessor_evaluation_id = predecessor_by_scope.get(scope)
        conn.execute(
            """
            INSERT INTO football_portfolio_evaluations (
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
                predecessor_evaluation_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(row["evaluation_id"]),
                str(row["canonical_fixture_id"]),
                str(row["policy_id"]),
                str(row["policy_version"]),
                str(row["effective_from"]),
                str(row["lifecycle_stage"]),
                str(row["evaluated_at"]),
                str(row["candidate_snapshot_at"]),
                int(row["candidate_count"]),
                int(row["selected_count"]),
                str(row["evaluation_signature"]),
                predecessor_evaluation_id,
                str(row["created_at"]),
            ),
        )
        predecessor_by_scope[scope] = str(row["evaluation_id"])


def _copy_legacy_selections(conn: sqlite3.Connection, *, table_name: str) -> None:
    if not _table_exists(conn, table_name):
        return
    conn.execute(
        f"""
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
        )
        SELECT
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
        FROM {table_name}
        ORDER BY created_at, portfolio_selection_id
        """
    )


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


def _latest_portfolio_evaluation(
    conn: sqlite3.Connection,
    *,
    canonical_fixture_id: str,
    policy_id: str,
    policy_version: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM football_portfolio_evaluations
        WHERE canonical_fixture_id = ?
          AND policy_id = ?
          AND policy_version = ?
        ORDER BY evaluated_at DESC, created_at DESC, evaluation_id DESC
        LIMIT 1
        """,
        (canonical_fixture_id, policy_id, policy_version),
    ).fetchone()


def _portfolio_evaluation_id(
    *,
    policy: PortfolioPolicy,
    canonical_fixture_id: str,
    predecessor_evaluation_id: str | None,
    evaluation_signature: str,
) -> str:
    return "portfolio_eval_" + canonical_hash(
        {
            "canonical_fixture_id": canonical_fixture_id,
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "effective_from": policy.effective_from,
            "predecessor_evaluation_id": predecessor_evaluation_id or "",
            "evaluation_signature": evaluation_signature,
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
    latest_evaluation_id: str | None = None,
    latest_evaluation_signature: str | None = None,
) -> PortfolioEvaluationWriteResult:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    created_at = _utc_now()
    selected_count = sum(1 for item in dispositions if item.selection_state == "SELECTED")
    with conn:
        latest_row = None
        expected_latest_evaluation_id = latest_evaluation_id
        expected_latest_evaluation_signature = latest_evaluation_signature
        for _ in range(2):
            if latest_row is None:
                if expected_latest_evaluation_id is None and expected_latest_evaluation_signature is None:
                    latest_row = _latest_portfolio_evaluation(
                        conn,
                        canonical_fixture_id=canonical_fixture_id,
                        policy_id=policy.policy_id,
                        policy_version=policy.policy_version,
                    )
                    expected_latest_evaluation_id = None if latest_row is None else str(latest_row["evaluation_id"])
                    expected_latest_evaluation_signature = None if latest_row is None else str(latest_row["evaluation_signature"])
                else:
                    latest_row = None
            if expected_latest_evaluation_signature == evaluation_signature:
                return PortfolioEvaluationWriteResult(
                    evaluation_id=str(expected_latest_evaluation_id),
                    inserted=False,
                )
            predecessor_evaluation_id = expected_latest_evaluation_id
            evaluation_id = _portfolio_evaluation_id(
                policy=policy,
                canonical_fixture_id=canonical_fixture_id,
                predecessor_evaluation_id=predecessor_evaluation_id,
                evaluation_signature=evaluation_signature,
            )
            result = conn.execute(
                """
                WITH latest AS (
                    SELECT evaluation_id, evaluation_signature
                    FROM football_portfolio_evaluations
                    WHERE canonical_fixture_id = ?
                      AND policy_id = ?
                      AND policy_version = ?
                    ORDER BY evaluated_at DESC, created_at DESC, evaluation_id DESC
                    LIMIT 1
                )
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
                    predecessor_evaluation_id,
                    created_at
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE coalesce((SELECT evaluation_id FROM latest), '') = ?
                  AND coalesce((SELECT evaluation_signature FROM latest), '') = ?
                  AND ? <> coalesce((SELECT evaluation_signature FROM latest), '')
                """,
                (
                    canonical_fixture_id,
                    policy.policy_id,
                    policy.policy_version,
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
                    predecessor_evaluation_id,
                    created_at,
                    predecessor_evaluation_id or "",
                    expected_latest_evaluation_signature or "",
                    evaluation_signature,
                ),
            )
            inserted = int(conn.execute("SELECT changes()").fetchone()[0]) > 0
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
                return PortfolioEvaluationWriteResult(evaluation_id=evaluation_id, inserted=True)
            latest_row = _latest_portfolio_evaluation(
                conn,
                canonical_fixture_id=canonical_fixture_id,
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
            )
            expected_latest_evaluation_id = None if latest_row is None else str(latest_row["evaluation_id"])
            expected_latest_evaluation_signature = None if latest_row is None else str(latest_row["evaluation_signature"])
            if expected_latest_evaluation_signature == evaluation_signature:
                return PortfolioEvaluationWriteResult(
                    evaluation_id=str(expected_latest_evaluation_id),
                    inserted=False,
                )
        raise sqlite3.IntegrityError("PORTFOLIO_EVALUATION_LIFECYCLE_WRITE_CONFLICT")
    raise AssertionError("unreachable")


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
