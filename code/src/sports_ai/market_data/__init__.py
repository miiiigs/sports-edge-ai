"""Provider-independent current market data contracts."""

from .api_football import ApiFootballOddsProvider, ApiFootballOddsProviderConfig
from .providers import MarketProvider, MarketProviderError, NormalizedMarketSnapshot, NotConfiguredMarketProvider
from .the_odds_api import (
    COMPETITION_TO_SPORT,
    TheOddsApiProvider,
    TheOddsApiProviderConfig,
    sanitize_url,
)

__all__ = [
    "ApiFootballOddsProvider",
    "ApiFootballOddsProviderConfig",
    "COMPETITION_TO_SPORT",
    "MarketProvider",
    "MarketProviderError",
    "NormalizedMarketSnapshot",
    "NotConfiguredMarketProvider",
    "TheOddsApiProvider",
    "TheOddsApiProviderConfig",
    "sanitize_url",
]
