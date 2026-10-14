"""Automatic, forward-only LIVE_ISSUED Model Pick issuance.

The CLI ``model_pick_maintenance --apply-live-issued`` was the only writer of
the Model Pick ledger, so fixtures discovered after a manual run never received
picks, and a late manual run labelled picks for already-kicked-off fixtures as
``LIVE_ISSUED``. This module is the scheduler-safe writer:

* Forward only: a pick is written only when the issuance instant is strictly
  before kickoff, from the first READY prediction generated before kickoff
  (``resolve_first_issued_prediction``). Fixtures whose kickoff has passed are
  reported, never issued.
* Frozen on first write: a ``(policy_id, fixture_id, market)`` that already has
  a pick is skipped without recomputation, so later prediction versions can
  never change or conflict with an issued pick.
* No identity merging: when another provider fixture id already holds a pick
  for the same canonical match and market, the new id is skipped and reported
  as a canonical duplicate. Neither row is changed. Both checks are repeated
  under the SQLite write lock, so overlapping issuers cannot both write.
* DB-only: reads persisted predictions/fixtures and writes only the Model Pick
  ledger. Zero provider requests, zero paper-ledger writes, zero Value Bets.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..storage import football_model_pick_ledger as ledger
from .model_pick_reconciliation import build_live_issued_ledger

ISSUANCE_SCHEDULER_ENV_VAR = "SPORTS_AI_ENABLE_MODEL_PICK_ISSUANCE_SCHEDULER"
ISSUANCE_INTERVAL_ENV_VAR = "SPORTS_AI_MODEL_PICK_ISSUANCE_INTERVAL_SECONDS"
DEFAULT_ISSUANCE_INTERVAL_SECONDS = 600
MIN_ISSUANCE_INTERVAL_SECONDS = 60

# Fixture + prediction refresh without the market-sync step. The dedicated
# market collector already owns market requests, so this never duplicates them.
PREDICTION_REFRESH_SCHEDULER_ENV_VAR = "SPORTS_AI_ENABLE_PREDICTION_REFRESH_SCHEDULER"
PREDICTION_REFRESH_INTERVAL_ENV_VAR = "SPORTS_AI_PREDICTION_REFRESH_INTERVAL_SECONDS"
PREDICTION_REFRESH_SYNC_KIND = "scheduled_prediction_refresh"
DEFAULT_PREDICTION_REFRESH_INTERVAL_SECONDS = 3600
MIN_PREDICTION_REFRESH_INTERVAL_SECONDS = 900

SKIP_ALREADY_ISSUED = "ALREADY_ISSUED"
SKIP_CANONICAL_DUPLICATE = "CANONICAL_DUPLICATE"
SKIP_KICKOFF_PASSED = "KICKOFF_PASSED"
SKIP_IMMUTABLE_CONFLICT = "IMMUTABLE_CONFLICT"


def resolve_issuance_interval_seconds(configured: int | None) -> int:
    value = int(configured or 0)
    if value <= 0:
        raw = (os.environ.get(ISSUANCE_INTERVAL_ENV_VAR) or "").strip()
        try:
            value = int(raw) if raw else DEFAULT_ISSUANCE_INTERVAL_SECONDS
        except ValueError:
            value = DEFAULT_ISSUANCE_INTERVAL_SECONDS
    return max(MIN_ISSUANCE_INTERVAL_SECONDS, value)


def resolve_prediction_refresh_interval_seconds(configured: int | None) -> int:
    value = int(configured or 0)
    if value <= 0:
        value = DEFAULT_PREDICTION_REFRESH_INTERVAL_SECONDS
    return max(MIN_PREDICTION_REFRESH_INTERVAL_SECONDS, value)


def _parse(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def issue_forward_model_picks(
    dashboard_conn: sqlite3.Connection,
    ledger_conn: sqlite3.Connection,
    *,
    now: str | None = None,
) -> dict[str, Any]:
    """Write forward LIVE_ISSUED picks for future fixtures. Idempotent.

    Each pick is committed in its own transaction so one conflicting row can
    never block or roll back the others.
    """
    issued_at = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    issued_at_dt = _parse(issued_at)
    manifest = build_live_issued_ledger(dashboard_conn, dry_run=True, now=issued_at)
    ledger.ensure_schema(ledger_conn)
    ledger_conn.commit()

    existing_ids: set[str] = set()
    canonical_holders: dict[tuple[str, str, str], str] = {}
    for row in ledger.fetch_model_pick_rows(ledger_conn, initialize_schema=False):
        existing_ids.add(str(row["model_pick_id"]))
        if row.get("canonical_fixture_id"):
            key = (str(row["policy_id"]), str(row["canonical_fixture_id"]), str(row["market"]))
            canonical_holders.setdefault(key, str(row["fixture_id"]))

    written: dict[str, str] = {}
    skipped: dict[str, int] = {}
    canonical_duplicates: list[dict[str, Any]] = []
    conflicts: list[str] = []

    def _skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for pick in manifest["proposed_picks"]:
        pick_id = ledger.model_pick_id(
            policy_id=str(pick["policy_id"]), fixture_id=str(pick["fixture_id"]), market=str(pick["market"])
        )
        if pick_id in existing_ids:
            _skip(SKIP_ALREADY_ISSUED)
            continue
        kickoff = _parse(pick.get("kickoff_utc"))
        # Re-checked at write time: never issue at or after kickoff.
        if kickoff is None or issued_at_dt is None or issued_at_dt >= kickoff:
            _skip(SKIP_KICKOFF_PASSED)
            continue
        canonical = pick.get("canonical_fixture_id")
        holder = None
        if canonical:
            key = (str(pick["policy_id"]), str(canonical), str(pick["market"]))
            holder = canonical_holders.get(key)
            if holder == str(pick["fixture_id"]):
                holder = None
        concurrent_issue = False
        if holder is None:
            try:
                with ledger_conn:
                    # Re-check under the write lock: another issuer (a second
                    # process or a manual CLI run) may have written since the
                    # snapshot above was read.
                    ledger.begin_immediate(ledger_conn)
                    if ledger_conn.execute(
                        "SELECT 1 FROM football_model_pick_entries WHERE model_pick_id = ?", (pick_id,)
                    ).fetchone():
                        concurrent_issue = True
                    elif canonical:
                        holder = ledger.canonical_pick_holder(
                            ledger_conn,
                            policy_id=str(pick["policy_id"]),
                            canonical_fixture_id=str(canonical),
                            market=str(pick["market"]),
                            fixture_id=str(pick["fixture_id"]),
                        )
                    if not concurrent_issue and holder is None:
                        ledger.record_model_pick(ledger_conn, pick, now=issued_at, initialize_schema=False)
            except sqlite3.IntegrityError as exc:
                _skip(SKIP_IMMUTABLE_CONFLICT)
                conflicts.append(str(exc))
                continue
        if concurrent_issue:
            existing_ids.add(pick_id)
            _skip(SKIP_ALREADY_ISSUED)
            continue
        if holder is not None:
            _skip(SKIP_CANONICAL_DUPLICATE)
            canonical_duplicates.append(
                {
                    "canonical_fixture_id": canonical,
                    "market": pick["market"],
                    "issued_fixture_id": holder,
                    "skipped_fixture_id": str(pick["fixture_id"]),
                }
            )
            continue
        existing_ids.add(pick_id)
        if canonical:
            canonical_holders.setdefault((str(pick["policy_id"]), str(canonical), str(pick["market"])), str(pick["fixture_id"]))
        written[pick_id] = str(pick["market"])

    return {
        "issued_at": issued_at,
        "written": len(written),
        "written_ah": sum(1 for market in written.values() if market == "asian_handicap"),
        "written_corners": sum(1 for market in written.values() if market == "corner_total"),
        "proposed": len(manifest["proposed_picks"]),
        "skipped": skipped,
        "kickoff_passed_not_issued": manifest.get("kickoff_passed_not_issued", 0),
        "latest_not_ready_not_issued": manifest.get("latest_not_ready_not_issued", 0),
        "canonical_duplicates": canonical_duplicates,
        "conflicts": conflicts,
        "missing_paired_lines": len(manifest.get("missing_paired_lines") or []),
    }


__all__ = [
    "ISSUANCE_INTERVAL_ENV_VAR",
    "ISSUANCE_SCHEDULER_ENV_VAR",
    "PREDICTION_REFRESH_INTERVAL_ENV_VAR",
    "PREDICTION_REFRESH_SCHEDULER_ENV_VAR",
    "PREDICTION_REFRESH_SYNC_KIND",
    "issue_forward_model_picks",
    "resolve_issuance_interval_seconds",
    "resolve_prediction_refresh_interval_seconds",
]
