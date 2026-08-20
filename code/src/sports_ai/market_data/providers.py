"""Provider-independent current football market data contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, Sequence


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MarketProviderError(RuntimeError):
    """Raised when a market provider cannot satisfy a request."""


@dataclass(frozen=True)
class NormalizedMarketSnapshot:
    provider: str
    fixture_id: str
    captured_at: str
    market: str
    selection: str
    decimal_odds: float
    bookmaker: str
    ingestion_version: str
    line: float | None = None
    provider_market_id: str | None = None
    source_timestamp: str | None = None
    is_pre_match: bool = True

    def __post_init__(self) -> None:
        if self.decimal_odds <= 1.0:
            raise ValueError("decimal_odds must be > 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MarketProvider(Protocol):
    """Normalized current market provider contract."""

    def provider_name(self) -> str:
        ...

    def configured(self) -> bool:
        ...

    def get_fixture_markets(self, fixture_id: str) -> Sequence[NormalizedMarketSnapshot]:
        ...

    def get_best_prices(self, fixture_id: str) -> Sequence[NormalizedMarketSnapshot]:
        ...

    def get_market_snapshot(self, fixture_id: str) -> Sequence[NormalizedMarketSnapshot]:
        ...

    def diagnostics(self) -> dict[str, Any]:
        ...


class NotConfiguredMarketProvider:
    """Explicit no-provider implementation for blocked current market workflows."""

    def __init__(self, *, provider_label: str = "not-configured") -> None:
        self._provider_label = provider_label

    def provider_name(self) -> str:
        return self._provider_label

    def configured(self) -> bool:
        return False

    def get_fixture_markets(self, fixture_id: str) -> Sequence[NormalizedMarketSnapshot]:
        raise MarketProviderError("CURRENT_ODDS_PROVIDER = NOT_CONFIGURED")

    def get_best_prices(self, fixture_id: str) -> Sequence[NormalizedMarketSnapshot]:
        raise MarketProviderError("CURRENT_ODDS_PROVIDER = NOT_CONFIGURED")

    def get_market_snapshot(self, fixture_id: str) -> Sequence[NormalizedMarketSnapshot]:
        raise MarketProviderError("CURRENT_ODDS_PROVIDER = NOT_CONFIGURED")

    def diagnostics(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name(),
            "configured": False,
            "state": "NOT_CONFIGURED",
            "current_market_decisions": "BLOCKED",
            "captured_at": _utc_now(),
            "reason": "No current odds provider is configured for live market snapshots.",
        }
