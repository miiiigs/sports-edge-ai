"""Production presentation policy for downstream Football safeguards.

The legacy dashboard helpers pre-date the external context layer. These helpers
make context BLOCK/VETO/FLAG states first-class so an xG PASS cannot visually
hide a later external veto.
"""

from __future__ import annotations

from typing import Any


def current_decision_state(row: dict[str, Any]) -> str:
    decision = str(row.get("decision") or "")
    day12_state = str(row.get("day12_state") or "")
    context_status = str(row.get("context_status") or "NOT_CONNECTED")
    reason_codes = {str(code) for code in (row.get("reason_codes") or [])}
    market_freshness = str(row.get("market_freshness") or row.get("freshness_state") or "")

    if row.get("paper_entry_eligible"):
        return "BET"
    if decision == "PRELIMINARY_CANDIDATE":
        if market_freshness != "FRESH":
            return "NO_BET_STALE_MARKET"
        if context_status == "VETO":
            return "NO_BET_CONTEXT_VETO"
        if context_status == "BLOCKED":
            return "BLOCKED_CONTEXT_EVIDENCE"
        if row.get("context_threshold_gate_passed") is False:
            return "NO_BET_CONTEXT_THRESHOLD"
        if day12_state == "BLOCKED_MISSING_XG_DATA":
            return "BLOCKED"
        if day12_state in {"DAY12_REVIEW_REQUIRED", "REJECTED_BY_XG_VETO", "REJECTED_BY_MULTIPLE_VETOES"}:
            return "NO_BET_SAFEGUARD"
        return "BLOCKED"
    if decision == "NO_BET":
        return "NO_BET_VALUE"
    if decision == "STALE_MARKET":
        return "NO_BET_STALE_MARKET"
    if decision == "MISSING_REQUIRED_MARKET_DATA":
        missing_price_reasons = {
            "INCOMPLETE_REFERENCE_MARKET",
            "MISSING_AVAILABLE_PRICE",
            "MISSING_REFERENCE_MARKET",
        }
        return "NO_BET_MISSING_PRICE" if reason_codes & missing_price_reasons else "BLOCKED"
    if decision == "REJECTED_RULE":
        unsupported = {"REJECT_CUP_MARKET_NOT_ALLOWED", "REJECT_TEAM_LEVEL_OUT_OF_SCOPE"}
        return "UNSUPPORTED" if reason_codes & unsupported else "BLOCKED"
    return "BLOCKED"


def safeguard_label(row: dict[str, Any]) -> str:
    context_status = str(row.get("context_status") or "NOT_CONNECTED")
    if context_status == "VETO":
        return "FAIL"
    if context_status == "BLOCKED":
        return "BLOCKED"
    if context_status == "FLAG" and row.get("context_threshold_gate_passed") is False:
        return "FAIL"

    day12_state = str(row.get("day12_state") or "")
    if day12_state in {"PASS", "PASSED"}:
        return "PASS" if context_status in {"PASS", "NOT_CONNECTED", ""} else context_status
    if day12_state in {"REJECTED_BY_XG_VETO", "REJECTED_BY_MULTIPLE_VETOES"}:
        return "FAIL"
    if day12_state == "DAY12_REVIEW_REQUIRED":
        return "REVIEW"
    if day12_state == "BLOCKED_MISSING_XG_DATA":
        return "BLOCKED"
    market_freshness = str(row.get("market_freshness") or "")
    if market_freshness and market_freshness != "FRESH":
        return market_freshness
    if context_status == "FLAG":
        return "FLAG"
    return "N/A"


def safeguard_reason(row: dict[str, Any]) -> str:
    context_status = str(row.get("context_status") or "NOT_CONNECTED")
    context_reasons = [
        str(code).replace("_", " ").title()
        for code in (row.get("context_reason_codes") or [])[:3]
    ]
    if context_status in {"VETO", "BLOCKED", "FLAG"} and context_reasons:
        return " / ".join(context_reasons)

    day12_state = str(row.get("day12_state") or "")
    if day12_state == "BLOCKED_MISSING_XG_DATA":
        return "POINT-IN-TIME XG NOT AVAILABLE"
    if day12_state in {"REJECTED_BY_XG_VETO", "REJECTED_BY_MULTIPLE_VETOES"}:
        return "POINT-IN-TIME XG VETO"
    if day12_state == "DAY12_REVIEW_REQUIRED":
        return "POINT-IN-TIME XG REVIEW REQUIRED"
    market_freshness = str(row.get("market_freshness") or "")
    if market_freshness and market_freshness != "FRESH":
        return market_freshness.replace("_", " ").title()
    if row.get("paper_entry_eligible"):
        return "PASSED"
    reasons = [str(code).replace("_", " ").title() for code in (row.get("reason_codes") or [])[:2]]
    return " / ".join(reasons) if reasons else "N/A"


def humanize_pick_status(row: dict[str, Any]) -> tuple[str, str]:
    state = current_decision_state(row)
    status = "BET" if state == "BET" else "NO BET"
    details: list[str] = []
    market_freshness = str(row.get("market_freshness") or "").strip()
    if market_freshness:
        details.append(f"{market_freshness.replace('_', ' ').title()} market")
    guard_reason = safeguard_reason(row)
    if guard_reason not in {"N/A", "PASSED"}:
        details.append(guard_reason)
    elif row.get("paper_entry_eligible"):
        details.append("Passed safeguards")
    return status, " · ".join(details)
