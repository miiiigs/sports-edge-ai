"""Shared market freshness helpers for current odds providers."""

from __future__ import annotations

import os

from ..dashboard.models import parse_iso_datetime

FRESHNESS_STATE_FRESH = "FRESH"
FRESHNESS_STATE_STALE = "STALE"
FRESHNESS_STATE_UNKNOWN = "UNKNOWN"

_DEFAULT_MARKET_MAX_AGE_ENV_VAR = "SPORTS_AI_MARKET_MAX_AGE_MINUTES"
_PROVIDER_MAX_AGE_ENV_VARS = {
    "api-football": "SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES",
}
_PROVIDER_DEFAULT_THRESHOLDS = {
    "api-football": 240,
}


def resolve_market_max_age_minutes(
    env_var: str = _DEFAULT_MARKET_MAX_AGE_ENV_VAR,
    *,
    default_when_unset: int | None = None,
) -> int | None:
    """Return the configured market max-age threshold in minutes, or None.

    Unset returns ``default_when_unset``. Non-numeric, zero, or negative values
    fail closed to ``None`` so callers never classify an unsafe snapshot as
    fresh.
    """

    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default_when_unset
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def resolve_market_max_age_minutes_for_provider(provider: str | None) -> int | None:
    """Return the max-age threshold for the given provider."""

    provider_key = str(provider or "").strip().lower()
    env_var = _PROVIDER_MAX_AGE_ENV_VARS.get(provider_key, _DEFAULT_MARKET_MAX_AGE_ENV_VAR)
    default_when_unset = _PROVIDER_DEFAULT_THRESHOLDS.get(provider_key)
    return resolve_market_max_age_minutes(env_var, default_when_unset=default_when_unset)


def classify_freshness_state(
    *,
    source_timestamp: str | None,
    evaluated_at: str,
    threshold_minutes: int | None,
    kickoff_utc: str | None = None,
) -> str:
    """Deterministically classify market freshness."""

    if source_timestamp is None or threshold_minutes is None:
        return FRESHNESS_STATE_UNKNOWN
    try:
        source_dt = parse_iso_datetime(source_timestamp)
        evaluated_dt = parse_iso_datetime(evaluated_at)
    except ValueError:
        return FRESHNESS_STATE_UNKNOWN
    if source_dt > evaluated_dt:
        return FRESHNESS_STATE_UNKNOWN
    if kickoff_utc is not None:
        try:
            if source_dt > parse_iso_datetime(kickoff_utc):
                return FRESHNESS_STATE_STALE
        except ValueError:
            pass
    age_minutes = (evaluated_dt - source_dt).total_seconds() / 60.0
    return FRESHNESS_STATE_FRESH if age_minutes <= threshold_minutes else FRESHNESS_STATE_STALE
