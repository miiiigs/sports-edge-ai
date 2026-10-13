"""Separate paper positions whose entry market evidence is quarantined.

PR #106 proved that API-Football Asian Handicap snapshots captured before the
HOME_HANDICAP correction (ingestion ``v1``, or entries recorded before entry
snapshots were linked) used an unsafe NAMED_TEAM sign-flip: AWAY positions were
graded against the opposite handicap at the opposite side's price. Those
snapshots were already excluded from Value eligibility; this module applies the
same rule to paper positions on the reporting surfaces.

Fixtures whose final score is disputed between providers are quarantined the
same way for every score-dependent market until an authoritative result is
verified; corner totals come from one statistics feed and stay counted.

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
QUARANTINE_REASON_DISPUTED_RESULT = "DISPUTED_FINAL_SCORE"

# Markets graded from something other than the final score. Everything else,
# including unknown markets, is treated as score-dependent (fail closed).
SCORE_INDEPENDENT_MARKETS = frozenset({"corner_total"})

# Provider fixture ids whose final score is disputed. Remove an entry only after
# the result is verified against an authoritative source; settlements already in
# the ledger are never changed by this list.
DISPUTED_FINAL_SCORE_FIXTURES: dict[str, str] = {
    # Osasuna v Getafe, LaLiga, 2026-08-31 17:30 UTC (2026-10-12 duplicate-identity diagnostic).
    "football-data.org:564657": "ESP1 Osasuna v Getafe 2026-08-31: football-data.org 1-1 vs API-Football 1-0",
    "api-football:1570358": "ESP1 Osasuna v Getafe 2026-08-31: football-data.org 1-1 vs API-Football 1-0",
}


def paper_row_market_evidence_quarantined(row: dict[str, Any]) -> bool:
    """Whether a paper position's entry evidence fails the PR #106 AH rule."""
    return not snapshot_is_value_eligible(
        {
            "market": row.get("market"),
            "provider": row.get("provider"),
            "ingestion_version": row.get("entry_ingestion_version"),
        }
    )


def paper_row_result_disputed(row: dict[str, Any]) -> bool:
    """Whether a paper position is graded from a disputed final score."""
    if str(row.get("fixture_id") or "") not in DISPUTED_FINAL_SCORE_FIXTURES:
        return False
    return str(row.get("market") or "") not in SCORE_INDEPENDENT_MARKETS


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
        elif paper_row_result_disputed(row):
            row["market_evidence_quarantined"] = True
            row["market_evidence_quarantine_reason"] = QUARANTINE_REASON_DISPUTED_RESULT
            quarantined.append(row)
        else:
            counted.append(row)
    return counted, quarantined


def _by_market(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        market = str(row.get("market") or "")
        counts[market] = counts.get(market, 0) + 1
    return dict(sorted(counts.items()))


def quarantine_summary(quarantined: list[dict[str, Any]]) -> dict[str, Any]:
    disputed = [r for r in quarantined if r.get("market_evidence_quarantine_reason") == QUARANTINE_REASON_DISPUTED_RESULT]
    legacy = [r for r in quarantined if r.get("market_evidence_quarantine_reason") != QUARANTINE_REASON_DISPUTED_RESULT]
    return {
        "market_evidence_quarantined_positions": len(legacy),
        "market_evidence_quarantined_by_market": _by_market(legacy),
        "market_evidence_quarantine_applied": bool(legacy),
        "market_evidence_quarantine_reason": QUARANTINE_REASON_LEGACY_AH if legacy else None,
        "disputed_result_quarantined_positions": len(disputed),
        "disputed_result_quarantined_by_market": _by_market(disputed),
        "disputed_result_fixture_ids": sorted({str(r.get("fixture_id") or "") for r in disputed}),
        "disputed_result_quarantine_reason": QUARANTINE_REASON_DISPUTED_RESULT if disputed else None,
    }


__all__ = [
    "DISPUTED_FINAL_SCORE_FIXTURES",
    "QUARANTINE_REASON_DISPUTED_RESULT",
    "QUARANTINE_REASON_LEGACY_AH",
    "SCORE_INDEPENDENT_MARKETS",
    "paper_row_result_disputed",
    "paper_row_market_evidence_quarantined",
    "partition_counted_paper_rows",
    "quarantine_summary",
]
