import pytest

from sports_ai.firewall import (
    FirewallViolation,
    build_payload,
    scan_for_market_contamination,
    scrub_text,
)


def test_clean_payload_passes():
    scan_for_market_contamination(
        {
            "event_id": "evt_x",
            "team_a": {"xg": 1.4, "recent": [{"goals": 2}]},
        }
    )


def test_forbidden_key_rejected():
    with pytest.raises(FirewallViolation):
        scan_for_market_contamination({"team_a": {"odds": 1.5}})

    with pytest.raises(FirewallViolation):
        scan_for_market_contamination({"opening_line": 2.1})

    with pytest.raises(FirewallViolation):
        scan_for_market_contamination({"nested": [{"bookmaker_view": "x"}]})


def test_odds_text_rejected():
    with pytest.raises(FirewallViolation):
        scan_for_market_contamination({"news": "City are strong favourites at 1.40 with bet365"})

    with pytest.raises(FirewallViolation):
        scan_for_market_contamination({"news": "priced at 2.10 after the news"})

    with pytest.raises(FirewallViolation):
        scan_for_market_contamination({"news": "an 11/4 shot before the injury"})


def test_plain_numbers_ok():
    # xG values, scores etc. must NOT be false positives.
    scan_for_market_contamination({"report": "won 2-1 with xG 1.85 vs 0.92, possession 61%"})


def test_scrub_text():
    text = scrub_text("Arsenal are favourites at 1.45 with bet365 after the win")
    assert "1.45" not in text and "[REDACTED-MARKET]" in text


def test_build_payload_allowlist():
    features = {"xg_for": 1.5, "odds_snapshot": {"home": 1.9}, "form": [1, 0, 1]}
    payload = build_payload(
        {"event_id": "evt_x"},
        ["xg_for", "form"],
        features,
        as_of="2026-08-11T00:00:00Z",
    )
    assert "odds_snapshot" not in payload and payload["xg_for"] == 1.5
