"""Prediction/market locks with a tamper-evident hash chain."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_hash(payload: Any) -> str:
    blob = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(blob).hexdigest()


def make_lock_row(
    event_id: str,
    run_id: str,
    zone_outputs: list[dict],
    prev_hash: str | None,
) -> dict:
    return {
        "event_id": event_id,
        "run_id": run_id,
        "payload_hash": canonical_hash(zone_outputs),
        "prev_hash": prev_hash,
    }


def verify_chain(lock_rows: list[dict]) -> bool:
    """Each prev_hash must equal the previous row's payload_hash."""

    prev = None
    for row in lock_rows:
        if row.get("prev_hash") != prev:
            return False
        prev = row["payload_hash"]
    return True
