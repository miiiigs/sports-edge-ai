"""Data-quality scoring (blueprint section 9) with per-competition floors."""

from __future__ import annotations

from .config import settings

# Deductions from 100. Tuned by backtest later.
DEDUCTIONS = {
    "missing_lineup": 8,
    "missing_injury_data": 8,
    "small_sample": 12,
    "newly_promoted_team": 6,
    "new_manager": 6,
    "returning_from_long_absence": 10,
    "sparse_challenger_history": 12,
    "missing_market_history": 4,
    "uncertain_player_identity": 15,
    "stale_news": 6,
    "single_source_only": 5,
    "missing_weather": 2,
}


def score(flags: list[str]) -> int:
    value = 100 - sum(DEDUCTIONS.get(flag, 0) for flag in flags)
    return max(0, min(100, value))


def passes_floor(dq: int, competition_id: str) -> bool:
    floor = settings.min_data_quality.get(competition_id, 75)
    return dq >= floor and dq >= 50


def band(dq: int) -> str:
    if dq >= 95:
        return "excellent"
    if dq >= 85:
        return "strong"
    if dq >= 70:
        return "acceptable"
    if dq >= 50:
        return "weak"
    return "automatic_pass"
