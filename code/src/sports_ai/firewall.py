"""The information firewall.

Two jobs:
1. build_payload: construct a Zone A agent's input from an ALLOW-LIST only.
2. scan_for_market_contamination: deep-scan any payload for odds-like content
   (keys AND free text) and raise FirewallViolation if found.

This module is the ONLY path by which agent inputs are constructed in
production and in backtests alike.
"""

from __future__ import annotations

import re
from typing import Any

FORBIDDEN_KEY_PATTERNS = [
    r"odds",
    r"price",
    r"line",
    r"handicap_price",
    r"vig",
    r"juice",
    r"bookmaker",
    r"sportsbook",
    r"market",
    r"consensus",
    r"implied",
    r"ticket",
    r"money_pct",
    r"steam",
    r"closing",
    r"opening_line",
    r"bet(ting)?_pct",
    r"favou?rite_price",
]
_KEY_RE = re.compile("|".join(FORBIDDEN_KEY_PATTERNS), re.IGNORECASE)

_TEXT_RES = [
    re.compile(
        r"\b\d+\.\d{2}\b.{0,40}\b(odds|price[ds]?|favou?rite|bet365|"
        r"pinnacle|betfair|william ?hill|ladbrokes|paddy ?power|"
        r"skybet|unibet|bwin|888)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(odds|price[ds]?|favou?rite)\b.{0,40}\b\d+\.\d{2}\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\b\d{1,3}/\d{1,2}\b.{0,25}\b(odds|shot|favou?rite|chance)\b", re.IGNORECASE),
    re.compile(r"\b(drifted|shortened|steamed?|backed into)\b.{0,30}\b\d", re.IGNORECASE),
    re.compile(r"\bimplied probabilit", re.IGNORECASE),
]


class FirewallViolation(Exception):
    """A forbidden field or odds-like text reached a blind-zone payload."""

    def __init__(self, path: str, sample: str):
        self.path = path
        self.sample = sample
        super().__init__(f"market contamination at '{path}': {sample!r}")


def scan_for_market_contamination(obj: Any, path: str = "$") -> None:
    """Deep scan. Raises FirewallViolation on the first hit."""

    if isinstance(obj, dict):
        for key, value in obj.items():
            if _KEY_RE.search(str(key)):
                raise FirewallViolation(f"{path}.{key}", f"forbidden key '{key}'")
            scan_for_market_contamination(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for idx, value in enumerate(obj):
            scan_for_market_contamination(value, f"{path}[{idx}]")
    elif isinstance(obj, str):
        for regex in _TEXT_RES:
            match = regex.search(obj)
            if match:
                raise FirewallViolation(path, match.group(0)[:80])


def scrub_text(text: str) -> str:
    """Redact odds-like fragments from ingested news text BEFORE storage."""

    out = text
    for regex in _TEXT_RES:
        out = regex.sub("[REDACTED-MARKET]", out)
    return out


def build_payload(
    event: dict,
    agent_allowed: list[str],
    feature_rows: dict[str, Any],
    as_of: str,
) -> dict:
    """Build a Zone A payload from an ALLOW-LIST of feature keys."""

    payload = {
        "event_id": event["event_id"],
        "as_of": as_of,
        **{key: value for key, value in feature_rows.items() if key in set(agent_allowed)},
    }
    scan_for_market_contamination(payload)
    return payload
