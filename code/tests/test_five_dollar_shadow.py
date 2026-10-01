"""Focused tests for the FRA1-only 5Dollar shadow collector (observational only)."""

from __future__ import annotations

import pathlib

import pytest

# Pre-existing market_data <-> dashboard import cycle guard (same as other provider tests).
import sports_ai.dashboard.services  # noqa: F401
from sports_ai.market_data import five_dollar_football as five
from sports_ai.market_data import five_dollar_shadow as shadow
from sports_ai.market_data.providers import MarketProviderError
from sports_ai.storage import football_paper_trading as paper

FIXTURE_ID = "evt_api_football_2311636891"
FRA1_ROW = {
    "competition_id": "FRA1",
    "fixture_id": FIXTURE_ID,
    "home_team_name": "Marseille",
    "away_team_name": "Paris Saint Germain",
    "kickoff_utc": "2026-09-20T18:45:00+00:00",
    "status": "finished",
}


def _quote(market, phase, selection, line, odds, *, captured_at="2026-10-01T00:00:00+00:00"):
    return five.FiveDollarQuote(
        provider=five.PROVIDER,
        provider_fixture_id="2311636891",
        fixture_id=FIXTURE_ID,
        competition_id="FRA1",
        bookmaker="Bet 365",
        market=market,
        phase=phase,
        selection=selection,
        line=line,
        decimal_odds=odds,
        captured_at=captured_at,
        kickoff_utc="2026-09-20T18:45:00+00:00",
    )


def _quotes():
    out = []
    for phase, ah_line, ah_home, ah_away in (
        ("opening", -0.25, 1.95, 1.85),
        ("closing", -0.5, 2.02, 1.78),
        ("inplay", 0.0, 3.5, 1.3),
    ):
        out.append(_quote("asian_handicap", phase, "HOME", ah_line, ah_home))
        out.append(_quote("asian_handicap", phase, "AWAY", ah_line, ah_away))
    for phase, cl_line, over, under in (
        ("opening", 8.5, 1.85, 1.95),
        ("closing", 9.0, 1.90, 1.80),
        ("inplay", 10.5, 3.0, 1.375),
    ):
        out.append(_quote("corner_total", phase, "OVER", cl_line, over))
        out.append(_quote("corner_total", phase, "UNDER", cl_line, under))
    return out


class _FakeProvider:
    def __init__(self, *, enabled=True, state="MATCHED", quotes=None, window=None):
        self._enabled = enabled
        self._state = state
        self._quotes = list(quotes if quotes is not None else _quotes())
        self._window = list(window or [])
        self.requests_allowed = 0
        self.cache_hits = 0

    def provider_name(self):
        return five.PROVIDER

    def configured(self):
        return True

    def enabled(self):
        return self._enabled

    def _fixtures_window(self, fixture_context):
        return list(self._window)

    def match_fixture(self, fixture_context):
        self.requests_allowed += 1  # window call
        return five.FixtureMatch(
            state=self._state,
            provider_fixture_id="2311636891" if self._state == "MATCHED" else None,
            kickoff_utc=fixture_context.get("kickoff_utc"),
            home_team_name=fixture_context.get("home_team_name"),
            away_team_name=fixture_context.get("away_team_name"),
            candidate_count=2 if self._state == "AMBIGUOUS" else 1,
        )

    def get_fixture_quotes(self, fixture_id, *, fixture_context=None):
        self.requests_allowed += 1  # odds call
        return list(self._quotes)

    def diagnostics(self):
        return {"governor": {"requests_allowed": self.requests_allowed}, "cache": {"hits": self.cache_hits}}


def _db(tmp_path):
    conn = paper.connect(tmp_path / "paper.sqlite")
    paper.initialize_database(conn)
    return conn


def _collector(monkeypatch, *, provider=None, conn=None, shadow_flag=True, config=None):
    if shadow_flag:
        monkeypatch.setenv(shadow.ENV_SHADOW_ENABLED, "1")
    else:
        monkeypatch.delenv(shadow.ENV_SHADOW_ENABLED, raising=False)
    return shadow.FiveDollarShadowCollector(
        provider=provider or _FakeProvider(), config=config, conn=conn
    )


# --------------------------------------------------------------------------- enablement
def test_shadow_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(shadow.ENV_SHADOW_ENABLED, raising=False)
    provider = _FakeProvider(enabled=True)
    collector = shadow.FiveDollarShadowCollector(provider=provider, conn=_db(tmp_path))
    assert collector.shadow_enabled() is False
    result = collector.collect([FRA1_ROW])
    assert result["shadow_enabled"] is False
    assert provider.requests_allowed == 0


def test_provider_enabled_shadow_disabled_zero_calls(monkeypatch, tmp_path):
    collector = _collector(monkeypatch, provider=_FakeProvider(enabled=True), conn=_db(tmp_path), shadow_flag=False)
    collector.collect([FRA1_ROW])
    assert collector.last_result()["counters"]["provider_calls"] == 0


def test_shadow_enabled_provider_disabled_zero_calls(monkeypatch, tmp_path):
    collector = _collector(monkeypatch, provider=_FakeProvider(enabled=False), conn=_db(tmp_path), shadow_flag=True)
    assert collector.shadow_enabled() is False
    collector.collect([FRA1_ROW])
    assert collector.last_result()["counters"]["provider_calls"] == 0


# --------------------------------------------------------------------------- scope
def test_non_fra1_makes_zero_provider_calls(monkeypatch, tmp_path):
    provider = _FakeProvider(enabled=True)
    collector = _collector(monkeypatch, provider=provider, conn=_db(tmp_path))
    collector.collect([{**FRA1_ROW, "competition_id": "ENG1"}])
    counters = collector.last_result()["counters"]
    assert counters["provider_calls"] == 0
    assert counters["fixtures_considered"] == 0
    assert provider.requests_allowed == 0


# --------------------------------------------------------------------------- collection
def test_both_enabled_collects_fra1(monkeypatch, tmp_path):
    provider = _FakeProvider(enabled=True)
    conn = _db(tmp_path)
    collector = _collector(monkeypatch, provider=provider, conn=conn)
    result = collector.collect([FRA1_ROW])
    assert result["shadow_enabled"] is True
    counters = result["counters"]
    assert counters["fixtures_matched"] == 1
    assert counters["provider_calls"] == 2
    assert counters["ah_quotes"] == 6  # 3 phases x HOME/AWAY
    assert counters["corner_quotes"] == 6  # 3 phases x OVER/UNDER
    assert counters["snapshots_written"] == 12
    rows = paper.fetch_market_snapshots(conn, fixture_id=FIXTURE_ID)
    assert {r["provider"] for r in rows} == {"5dollar-football-api"}  # no fallback rows
    conn.close()


def test_request_cycle_cap(monkeypatch, tmp_path):
    provider = _FakeProvider(enabled=True)
    collector = _collector(
        monkeypatch,
        provider=provider,
        conn=_db(tmp_path),
        config=shadow.ShadowConfig(max_provider_calls_per_cycle=5, max_provider_calls_per_hour=20),
    )
    rows = [{**FRA1_ROW, "fixture_id": f"{FIXTURE_ID}_{i}"} for i in range(5)]
    result = collector.collect(rows)
    counters = result["counters"]
    assert counters["provider_calls"] <= 5
    assert counters["fixtures_matched"] == 2
    assert counters["rate_blocked"] >= 1
    assert provider.requests_allowed <= 5


def test_repeated_cycle_is_idempotent(monkeypatch, tmp_path):
    conn = _db(tmp_path)
    collector = _collector(monkeypatch, provider=_FakeProvider(enabled=True), conn=conn)
    first = collector.collect([FRA1_ROW])
    assert first["counters"]["snapshots_written"] == 12
    second = collector.collect([FRA1_ROW])
    assert second["counters"]["snapshots_written"] == 0
    assert second["counters"]["snapshots_reused"] == 12
    assert len(paper.fetch_market_snapshots(conn, fixture_id=FIXTURE_ID)) == 12
    conn.close()


def test_status_classification_and_alias_evidence(monkeypatch, tmp_path):
    window = [
        {
            "id": 999,
            "teams": {"home": {"name": "Marseille"}, "away": {"name": "Paris SG"}},
            "kickoff_utc": "2026-09-20T18:45:00+00:00",
        }
    ]
    provider = _FakeProvider(enabled=True, state="UNMATCHED", window=window)
    collector = _collector(monkeypatch, provider=provider, conn=_db(tmp_path))
    result = collector.collect([FRA1_ROW])
    counters = result["counters"]
    assert counters["fixtures_unmatched"] == 1
    assert counters["alias_misses"] == 1
    assert result["alias_candidates"]  # candidate aliases flagged for manual review
    assert result["alias_candidates"][0]["provider"] == ["Marseille", "Paris SG"]


def test_ambiguous_fixture(monkeypatch, tmp_path):
    provider = _FakeProvider(enabled=True, state="AMBIGUOUS")
    collector = _collector(monkeypatch, provider=provider, conn=_db(tmp_path))
    counters = collector.collect([FRA1_ROW])["counters"]
    assert counters["fixtures_ambiguous"] == 1
    assert counters["snapshots_written"] == 0


# --------------------------------------------------------------------------- phases
def test_phases_and_inplay_pre_match_flag(monkeypatch, tmp_path):
    conn = _db(tmp_path)
    _collector(monkeypatch, provider=_FakeProvider(enabled=True), conn=conn).collect([FRA1_ROW])
    rows = paper.fetch_market_snapshots(conn, fixture_id=FIXTURE_ID)
    by_phase = {}
    for r in rows:
        phase = (r["provider_market_id"] or "").split(":")[3]
        by_phase.setdefault(phase, []).append(r)
    assert set(by_phase) == {"opening", "closing", "inplay"}
    assert all(r["is_pre_match"] == 1 for r in by_phase["opening"])
    assert all(r["is_pre_match"] == 1 for r in by_phase["closing"])
    assert all(r["is_pre_match"] == 0 for r in by_phase["inplay"])
    conn.close()


# --------------------------------------------------------------------------- firewall
def test_shadow_cycle_creates_no_paper_or_decision_rows(monkeypatch, tmp_path):
    conn = _db(tmp_path)
    before = {
        t: conn.execute(f"select count(*) from {t}").fetchone()[0]
        for t in ("paper_trade_entries", "paper_trade_updates")
    }
    _collector(monkeypatch, provider=_FakeProvider(enabled=True), conn=conn).collect([FRA1_ROW])
    after = {
        t: conn.execute(f"select count(*) from {t}").fetchone()[0]
        for t in ("paper_trade_entries", "paper_trade_updates")
    }
    assert before == after
    assert after["paper_trade_entries"] == 0
    conn.close()


def test_shadow_module_has_no_decision_paths():
    src = pathlib.Path(shadow.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "build_fixture_market_evaluation",
        "_maybe_create_paper_entries",
        "paper_trade_entries",
        "DashboardServices",
        "portfolio",
        "manager",
    ):
        assert forbidden not in src


def test_comparison_is_read_only_and_never_averages(monkeypatch, tmp_path):
    conn = _db(tmp_path)
    # seed an API-Football observation for the same fixture/market/selection/line
    paper.record_market_snapshot(
        conn,
        paper.StoredMarketSnapshot(
            fixture_id=FIXTURE_ID,
            provider="api-football",
            bookmaker="Bet365",
            market="asian_handicap",
            selection="HOME",
            odds=2.0,
            ingestion_version="test",
            line=-0.25,
            captured_at="2026-09-30T00:00:00+00:00",
        ),
    )
    collector = _collector(monkeypatch, provider=_FakeProvider(enabled=True), conn=conn)
    collector.collect([FRA1_ROW])
    rows = collector.compare_with_api_football(FIXTURE_ID)
    assert rows
    for row in rows:
        assert "average" not in {str(k).lower() for k in row}
    match = next(r for r in rows if r["market"] == "asian_handicap" and r["selection"] == "HOME" and r["line"] == -0.25)
    assert match["api_football_price"] == 2.0
    assert match["five_dollar_price"] == 1.95
    assert match["difference"] == pytest.approx(1.95 - 2.0)
    conn.close()


def test_no_model_contamination_on_shadow_output(monkeypatch, tmp_path):
    from sports_ai import firewall

    conn = _db(tmp_path)
    collector = _collector(monkeypatch, provider=_FakeProvider(enabled=True), conn=conn)
    result = collector.collect([FRA1_ROW])
    # shadow diagnostics carry no odds values into blind-model payloads
    try:
        firewall.scan_for_market_contamination({"shadow": {"enabled": result["shadow_enabled"]}})
    except firewall.FirewallViolation:  # pragma: no cover - guard
        pytest.fail("shadow metadata tripped the blind firewall")
    # but an odds payload is still rejected (firewall intact)
    with pytest.raises(firewall.FirewallViolation):
        firewall.scan_for_market_contamination({"decimal_odds": 1.95})
    conn.close()


def test_provider_error_fails_closed(monkeypatch, tmp_path):
    class _BoomProvider(_FakeProvider):
        def match_fixture(self, fixture_context):
            raise MarketProviderError("PROVIDER_ERROR status=500")

    collector = _collector(monkeypatch, provider=_BoomProvider(enabled=True), conn=_db(tmp_path))
    counters = collector.collect([FRA1_ROW])["counters"]
    assert counters["fixtures_unmatched"] == 1
    assert counters["snapshots_written"] == 0


def test_shadow_run_wrapper(monkeypatch, tmp_path):
    collector = _collector(monkeypatch, provider=_FakeProvider(enabled=True), conn=_db(tmp_path))
    run = shadow.ShadowRun.from_result(collector.collect([FRA1_ROW]))
    assert run.shadow_enabled is True
    assert run.counters["fixtures_matched"] == 1
    assert "budget" in run.to_dict()
