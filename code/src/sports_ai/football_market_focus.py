"""Production market-focus policy for Football.

The model continues to compute every supported market family. This module only
controls which markets the live/current-odds layer prioritizes and publishes.
That separation keeps research evidence intact while allowing a cost-efficient
production focus.
"""

from __future__ import annotations

import os

LIVE_MARKET_FOCUS_ENV_VAR = "SPORTS_AI_FOOTBALL_LIVE_MARKET_FOCUS"

ALL_MARKETS = (
    "1x2",
    "btts",
    "goals_total",
    "asian_handicap",
    "corner_total",
)

PREFERRED_AH_CORNERS_FOCUS = (
    "asian_handicap",
    "corner_total",
)


def configured_live_market_focus() -> tuple[str, ...]:
    raw = os.environ.get(LIVE_MARKET_FOCUS_ENV_VAR, "").strip()
    if not raw:
        return ALL_MARKETS
    requested: list[str] = []
    for item in raw.split(","):
        market = item.strip().lower()
        if market in ALL_MARKETS and market not in requested:
            requested.append(market)
    return tuple(requested) if requested else ALL_MARKETS


def is_focused_live_mode() -> bool:
    return configured_live_market_focus() != ALL_MARKETS
