"""Closing-snapshot (CLV) eligibility: kickoff window + trusted AH evidence.

The restriction applies ONLY to new closing selections. Already stored
closing_odds / clv_pct values in paper_trade_entries are never rewritten.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import sports_ai.dashboard.services as services_mod
from sports_ai.dashboard.services import DashboardConfig, DashboardServices
from sports_ai.market_data.providers import NotConfiguredMarketProvider

KICKOFF = "2026-10-10T14:00:00+00:00"
V1 = "api_football_current_market_provider_v1"
V2 = "api_football_current_market_provider_v2"


def _at(minutes_before: int) -> str:
    base = datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)
    return (base - timedelta(minutes=minutes_before)).isoformat(timespec="seconds")


def _snap(snapshot_id, minutes_before, *, market="asian_handicap", selection="HOME", line=-0.5,
          odds=2.0, version=V2, provider="api-football", is_pre_match=True):
    return {
        "snapshot_id": snapshot_id, "fixture_id": "api-football:1", "provider": provider,
        "bookmaker": "Pinnacle", "market": market, "selection": selection, "line": line,
        "odds": odds, "captured_at": _at(minutes_before), "is_pre_match": is_pre_match,
        "ingestion_version": version,
    }


class _NoFixtures:
    def provider_name(self):
        return "none"

    def configured(self):
        return False


def _services(tmp_path):
    config = replace(
        DashboardConfig.default(),
        dashboard_db_path=tmp_path / "d.sqlite",
        paper_trading_db_path=tmp_path / "p.sqlite",
        model_pick_db_path=tmp_path / "m.sqlite",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        enable_scheduler=False,
    )
    return DashboardServices(config, fixture_provider=_NoFixtures(), market_provider=NotConfiguredMarketProvider())


def _closing(services, rows, **kw):
    params = {"market": "asian_handicap", "selection": "HOME", "line": -0.5, "kickoff_utc": KICKOFF}
    params.update(kw)
    return services._latest_closing_snapshot(rows, **params)


def test_default_window_is_60_and_fails_safe(monkeypatch):
    monkeypatch.delenv(services_mod.CLOSING_ODDS_MAX_MINUTES_BEFORE_KICKOFF_ENV_VAR, raising=False)
    assert services_mod.resolve_closing_odds_max_minutes_before_kickoff() == 60
    for bad in ("0", "-5", "abc"):
        monkeypatch.setenv(services_mod.CLOSING_ODDS_MAX_MINUTES_BEFORE_KICKOFF_ENV_VAR, bad)
        assert services_mod.resolve_closing_odds_max_minutes_before_kickoff() == 60
    monkeypatch.setenv(services_mod.CLOSING_ODDS_MAX_MINUTES_BEFORE_KICKOFF_ENV_VAR, "100000")
    assert services_mod.resolve_closing_odds_max_minutes_before_kickoff() == 360


def test_stale_closing_rejected_without_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv(services_mod.CLOSING_ODDS_MAX_MINUTES_BEFORE_KICKOFF_ENV_VAR, raising=False)
    services = _services(tmp_path)
    # Only captures 414 / 2713 minutes before kickoff exist -> closing unavailable.
    assert _closing(services, [_snap(1, 414), _snap(2, 2713)]) is None
    assert _closing(services, [_snap(1, 61)]) is None


def test_in_window_v2_accepted_and_latest_wins(tmp_path, monkeypatch):
    monkeypatch.delenv(services_mod.CLOSING_ODDS_MAX_MINUTES_BEFORE_KICKOFF_ENV_VAR, raising=False)
    services = _services(tmp_path)
    rows = [_snap(1, 300, odds=1.9), _snap(2, 60, odds=2.0), _snap(3, 5, odds=2.1), _snap(4, 0, odds=2.2)]
    assert _closing(services, rows)["snapshot_id"] == 4  # captured_at == kickoff allowed
    assert _closing(services, rows[:2])["snapshot_id"] == 2  # exactly 60m boundary


def test_post_kickoff_and_in_play_rejected(tmp_path):
    services = _services(tmp_path)
    assert _closing(services, [_snap(1, -1)]) is None
    assert _closing(services, [_snap(1, 5, is_pre_match=False)]) is None


def test_v1_and_unknown_ah_rejected_as_closing(tmp_path):
    services = _services(tmp_path)
    assert _closing(services, [_snap(1, 5, version=V1)]) is None
    assert _closing(services, [_snap(1, 5, version="")]) is None
    # v1 in window is skipped; an in-window v2 row is still used.
    assert _closing(services, [_snap(1, 3, version=V1), _snap(2, 10, version=V2)])["snapshot_id"] == 2


def test_corners_and_identity_matching_unchanged(tmp_path):
    services = _services(tmp_path)
    corner = _snap(1, 5, market="corner_total", selection="OVER", line=9.5, version=V1)
    assert _closing(services, [corner], market="corner_total", selection="OVER", line=9.5)["snapshot_id"] == 1
    assert _closing(services, [_snap(1, 5, selection="AWAY")]) is None
    assert _closing(services, [_snap(1, 5, line=-0.25)]) is None


def test_stored_closing_values_never_rewritten(tmp_path, monkeypatch):
    services = _services(tmp_path)
    stored = {
        "paper_trade_id": "pt1", "fixture_id": "api-football:1", "market": "asian_handicap",
        "selection": "HOME", "line": -0.5, "kickoff": KICKOFF, "closing_odds": 1.95,
        "clv_pct": 0.03, "trade_state": "WON", "entry_odds": 2.0, "available_market_price": 2.0,
    }
    calls = {"attach": 0, "closing": 0, "resolved": 0}
    monkeypatch.setattr(services_mod.football_paper_trading, "fetch_market_snapshots",
                        lambda conn, **kw: [_snap(9, 5)])
    monkeypatch.setattr(services_mod.football_paper_trading, "fetch_paper_trade_rows",
                        lambda conn, **kw: [dict(stored)])
    monkeypatch.setattr(services_mod.football_paper_trading, "attach_closing_snapshot",
                        lambda *a, **kw: calls.__setitem__("attach", calls["attach"] + 1))
    original = services._latest_closing_snapshot
    monkeypatch.setattr(services, "_latest_closing_snapshot",
                        lambda *a, **kw: (calls.__setitem__("closing", calls["closing"] + 1), original(*a, **kw))[1])
    monkeypatch.setattr(services, "_resolve_fixture_for_paper_settlement",
                        lambda **kw: (calls.__setitem__("resolved", calls["resolved"] + 1),
                                      {"fixture_id": "api-football:1", "status": "FINISHED"})[1])
    try:
        services._process_paper_trading_updates([])
    except Exception:  # noqa: BLE001 - downstream settlement internals are out of scope here
        pass
    assert calls["resolved"] == 1  # the stored row really reached the closing step
    assert calls["attach"] == 0 and calls["closing"] == 0
