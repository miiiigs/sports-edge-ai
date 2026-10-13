"""Separate paper positions whose entry market evidence is quarantined.

PR #106 proved that API-Football Asian Handicap snapshots captured before the
HOME_HANDICAP correction (ingestion ``v1``, or entries recorded before entry
snapshots were linked) used an unsafe NAMED_TEAM sign-flip: AWAY positions were
graded against the opposite handicap at the opposite side's price. Those
snapshots were already excluded from Value eligibility; this module applies the
same rule to paper positions on the reporting surfaces.

Nothing is deleted, re-graded or rewritten. Quarantined positions stay in the
ledger, are marked per row, and are reported as an explicit count so headline
KPIs never silently blend them in. Corners and all other markets/providers are
unaffected.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .frozen_value_evaluation import snapshot_is_value_eligible

QUARANTINE_REASON_LEGACY_AH = "LEGACY_API_FOOTBALL_AH_NORMALIZATION"


def paper_row_market_evidence_quarantined(row: dict[str, Any]) -> bool:
    """Whether a paper position's entry evidence fails the PR #106 AH rule."""
    return not snapshot_is_value_eligible(
        {
            "market": row.get("market"),
            "provider": row.get("provider"),
            "ingestion_version": row.get("entry_ingestion_version"),
        }
    )


def partition_counted_paper_rows(
    rows: Iterable[dict[str, Any]],
    excluded_trade_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split non-reconciled rows into ``(counted, quarantined)``.

    Every non-reconciled row is marked with ``market_evidence_quarantined`` and,
    when quarantined, ``market_evidence_quarantine_reason``.
    """
    counted: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("paper_trade_id") or "") in excluded_trade_ids:
            continue
        is_quarantined = paper_row_market_evidence_quarantined(row)
        row["market_evidence_quarantined"] = is_quarantined
        if is_quarantined:
            row["market_evidence_quarantine_reason"] = QUARANTINE_REASON_LEGACY_AH
            quarantined.append(row)
        else:
            counted.append(row)
    return counted, quarantined


def quarantine_summary(quarantined: list[dict[str, Any]]) -> dict[str, Any]:
    by_market: dict[str, int] = {}
    for row in quarantined:
        market = str(row.get("market") or "")
        by_market[market] = by_market.get(market, 0) + 1
    return {
        "market_evidence_quarantined_positions": len(quarantined),
        "market_evidence_quarantined_by_market": dict(sorted(by_market.items())),
        "market_evidence_quarantine_applied": bool(quarantined),
        "market_evidence_quarantine_reason": QUARANTINE_REASON_LEGACY_AH if quarantined else None,
    }


__all__ = [
    "QUARANTINE_REASON_LEGACY_AH",
    "paper_row_market_evidence_quarantined",
    "partition_counted_paper_rows",
    "quarantine_summary",
]
