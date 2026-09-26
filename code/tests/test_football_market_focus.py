from sports_ai.football_market_focus import (
    ALL_MARKETS,
    PREFERRED_AH_CORNERS_FOCUS,
    configured_live_market_focus,
    is_focused_live_mode,
)


def test_market_focus_defaults_to_all_markets(monkeypatch):
    monkeypatch.delenv("SPORTS_AI_FOOTBALL_LIVE_MARKET_FOCUS", raising=False)
    assert configured_live_market_focus() == ALL_MARKETS
    assert is_focused_live_mode() is False


def test_market_focus_normalizes_and_deduplicates(monkeypatch):
    monkeypatch.setenv(
        "SPORTS_AI_FOOTBALL_LIVE_MARKET_FOCUS",
        "asian_handicap, corner_total,asian_handicap,unknown",
    )
    assert configured_live_market_focus() == PREFERRED_AH_CORNERS_FOCUS
    assert is_focused_live_mode() is True


def test_invalid_only_focus_falls_back_to_all_markets(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_FOOTBALL_LIVE_MARKET_FOCUS", "unknown")
    assert configured_live_market_focus() == ALL_MARKETS
