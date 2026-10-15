"""Focused tests for the long-lived, isolated FRA1 5Dollar shadow runtime."""

from __future__ import annotations

import asyncio
import pathlib
import sqlite3
from datetime import datetime

# Pre-existing market_data <-> dashboard import cycle guard (same as other provider tests).
import sports_ai.dashboard.services  # noqa: F401
from sports_ai.dashboard import scheduler_worker
from sports_ai.market_data import five_dollar_football as five
from sports_ai.market_data import five_dollar_shadow as shadow
from sports_ai.market_data import five_dollar_shadow_reporting as reporting
from sports_ai.market_data import five_dollar_shadow_runtime as runtime_mod
from sports_ai.storage import football_paper_trading as paper
from sports_ai.storage.football_canonical_fixture import (
    canonical_fixture_id_from_identity,
)

FRA1 = "FRA1"


def _fx(provider_id, home, away, kickoff, **extra):
    return {
        "id": provider_id,
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "kickoff_utc": kickoff,
        **extra,
    }


def _window():
    # Deliberately out of kickoff order to prove deterministic selection.
    return [
        _fx("p3", "Lyon", "Nice", "2026-10-05T19:00:00+00:00"),
        _fx("p1", "Marseille", "Paris Saint Germain", "2026-10-03T18:45:00+00:00"),
        _fx("p2", "Brest", "Troyes", "2026-10-04T15:00:00+00:00"),
    ]


def _quote(
    market,
    phase,
    selection,
    line,
    odds,
    *,
    provider_id="p1",
    fixture_id="canon",
    captured_at="2026-10-01T00:00:00+00:00",
):
    return five.FiveDollarQuote(
        provider=five.PROVIDER,
        provider_fixture_id=provider_id,
        fixture_id=fixture_id,
        competition_id=FRA1,
        bookmaker="Bet 365",
        market=market,
        phase=phase,
        selection=selection,
        line=line,
        decimal_odds=odds,
        captured_at=captured_at,
        kickoff_utc="2026-10-03T18:45:00+00:00",
    )


def _quotes():
    return [
        _quote("asian_handicap", "opening", "HOME", -0.25, 1.95),
        _quote("asian_handicap", "opening", "AWAY", -0.25, 1.85),
        _quote("corner_total", "opening", "OVER", 8.5, 1.85),
        _quote("corner_total", "opening", "UNDER", 8.5, 1.95),
    ]


class _Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _FakeProvider:
    def __init__(self, *, enabled=True, window=None, quotes=None, match_state="MATCHED"):
        self._enabled = enabled
        self._window = list(window) if window is not None else _window()
        self._quotes = list(quotes if quotes is not None else _quotes())
        self._match_state = match_state
        self.requests_allowed = 0
        self.cache_hits = 0
        self.list_calls = 0
        self.list_competitions: list[str] = []

    def provider_name(self):
        return five.PROVIDER

    def configured(self):
        return True

    def enabled(self):
        return self._enabled

    def list_league_fixtures(self, competition_id, *, from_utc=None, to_utc=None, per_page=100):
        self.requests_allowed += 1
        self.list_calls += 1
        self.list_competitions.append(competition_id)
        return list(self._window)

    def _fixtures_window(self, fixture_context):
        return list(self._window)

    def match_fixture(self, fixture_context):
        self.requests_allowed += 1
        state = self._match_state
        return five.FixtureMatch(
            state=state,
            provider_fixture_id="p1" if state == "MATCHED" else None,
            kickoff_utc=fixture_context.get("kickoff_utc"),
            home_team_name=fixture_context.get("home_team_name"),
            away_team_name=fixture_context.get("away_team_name"),
            candidate_count=2 if state == "AMBIGUOUS" else 1,
        )

    def get_fixture_quotes(self, fixture_id, *, fixture_context=None):
        self.requests_allowed += 1
        return [
            _quote(
                q.market,
                q.phase,
                q.selection,
                q.line,
                q.decimal_odds,
                provider_id=q.provider_fixture_id,
                fixture_id=fixture_id,
                captured_at=q.captured_at,
            )
            for q in self._quotes
        ]

    def diagnostics(self):
        return {"governor": {"requests_allowed": self.requests_allowed}, "cache": {"hits": self.cache_hits}}


def _db(tmp_path):
    conn = paper.connect(tmp_path / "paper.sqlite")
    paper.initialize_database(conn)
    return conn


def _runtime(
    monkeypatch,
    *,
    provider=None,
    config=None,
    conn=None,
    clock=None,
    shadow_flag=True,
    provider_flag=True,
    now_fn=None,
):
    if shadow_flag:
        monkeypatch.setenv(shadow.ENV_SHADOW_ENABLED, "1")
    else:
        monkeypatch.delenv(shadow.ENV_SHADOW_ENABLED, raising=False)
    if provider_flag:
        monkeypatch.setenv(five.ENV_ENABLED, "1")
    else:
        monkeypatch.delenv(five.ENV_ENABLED, raising=False)
    return runtime_mod.FiveDollarShadowRuntime(
        provider=provider if provider is not None else _FakeProvider(),
        config=config,
        conn=conn,
        clock=clock if clock is not None else _Clock(),
        now_fn=now_fn,
    )


def _canon(home, away, kickoff):
    return canonical_fixture_id_from_identity(
        competition_id=FRA1,
        home_team_name=home,
        away_team_name=away,
        kickoff_utc=kickoff,
    )


def _snapshot(fixture_id, market, capture, phase, *, is_pre_match=1, line, odds, kickoff, selection="HOME"):
    return paper.StoredMarketSnapshot(
        fixture_id=fixture_id,
        provider=five.PROVIDER,
        bookmaker="Bet 365",
        market=market,
        selection=selection,
        odds=odds,
        ingestion_version="test",
        line=line,
        provider_market_id=f"999:Bet 365:{market}:{phase}:{selection}:{line}",
        captured_at=capture,
        is_pre_match=is_pre_match,
        provider_fixture_id="999",
        kickoff_utc=kickoff,
    )


# --------------------------------------------------------------------------- enablement
def test_runtime_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(shadow.ENV_SHADOW_ENABLED, raising=False)
    provider = _FakeProvider(enabled=True)
    runtime = runtime_mod.FiveDollarShadowRuntime(provider=provider)
    assert runtime.enabled() is False
    assert runtime._conn is None
    result = runtime.run_cycle()
    assert result["state"] == "disabled"
    assert provider.requests_allowed == 0
    assert result["counters"]["snapshots_written"] == 0


def test_provider_enabled_shadow_disabled_zero_calls(monkeypatch, tmp_path):
    provider = _FakeProvider(enabled=True)
    runtime = _runtime(monkeypatch, provider=provider, shadow_flag=False, conn=_db(tmp_path))
    result = runtime.run_cycle()
    assert result["state"] == "disabled"
    assert provider.requests_allowed == 0
    assert result["counters"]["provider_calls"] == 0


def test_shadow_enabled_provider_disabled_zero_calls(monkeypatch, tmp_path):
    provider = _FakeProvider(enabled=False)
    runtime = _runtime(monkeypatch, provider=provider, shadow_flag=True, conn=_db(tmp_path))
    assert runtime.enabled() is False
    result = runtime.run_cycle()
    assert result["state"] == "disabled"
    assert provider.requests_allowed == 0


def test_both_enabled_collects(monkeypatch, tmp_path):
    provider = _FakeProvider(enabled=True, window=[_fx("p1", "Marseille", "Paris Saint Germain", "2026-10-03T18:45:00+00:00")])
    conn = _db(tmp_path)
    runtime = _runtime(monkeypatch, provider=provider, conn=conn)
    assert runtime.enabled() is True
    result = runtime.run_cycle()
    assert result["state"] == "enabled"
    assert provider.list_competitions == [FRA1]
    assert result["counters"]["fixtures_selected"] == 1
    assert result["counters"]["snapshots_written"] > 0
    assert conn.execute("select count(*) from market_snapshots").fetchone()[0] > 0
    conn.close()


# --------------------------------------------------------------------------- long-lived / budget
def test_long_lived_collector_reused_across_cycles(monkeypatch, tmp_path):
    provider = _FakeProvider(window=[_fx("p1", "Marseille", "Paris Saint Germain", "2026-10-03T18:45:00+00:00")])
    clock = _Clock()
    runtime = _runtime(monkeypatch, provider=provider, conn=_db(tmp_path), clock=clock)
    collector_before = runtime._collector
    clock.value += 10_000.0
    runtime.run_cycle()
    clock.value += 10_000.0
    runtime.run_cycle()
    assert runtime._collector is collector_before
    assert runtime._collector._provider is provider


def test_hourly_budget_survives_second_cycle(monkeypatch, tmp_path):
    provider = _FakeProvider(window=[_fx("p1", "Marseille", "Paris Saint Germain", "2026-10-03T18:45:00+00:00")])
    clock = _Clock()
    config = runtime_mod.ShadowRuntimeConfig(recent_skip_seconds=0)
    runtime = _runtime(monkeypatch, provider=provider, conn=_db(tmp_path), clock=clock, config=config)
    first = runtime.run_cycle()
    assert first["budget"]["hourly_calls"] == 3  # discovery + match + odds
    clock.value += 100.0  # well inside the rolling hour window
    second = runtime.run_cycle()
    # A fresh collector would reset to 3; a long-lived one accumulates.
    assert second["budget"]["hourly_calls"] == 6
    assert second["budget"]["hourly_calls"] > first["budget"]["hourly_calls"]


# --------------------------------------------------------------------------- fixture limits + selection
def test_fixture_limit_never_exceeds_two(monkeypatch, tmp_path):
    config = runtime_mod.ShadowRuntimeConfig(max_fixtures_per_cycle=9)
    assert config.max_fixtures_per_cycle == 2  # hard clamp
    runtime = _runtime(monkeypatch, provider=_FakeProvider(), conn=_db(tmp_path), config=config)
    result = runtime.run_cycle()
    assert result["counters"]["fixtures_selected"] == 2
    assert len(result["runtime"]["selected"]) == 2


def test_deterministic_earliest_kickoff_first(monkeypatch, tmp_path):
    runtime = _runtime(monkeypatch, provider=_FakeProvider(), conn=_db(tmp_path))
    result = runtime.run_cycle()
    selected = result["runtime"]["selected"]
    assert [row["kickoff_utc"] for row in selected] == sorted(row["kickoff_utc"] for row in selected)
    assert selected[0]["home_team_name"] == "Marseille"


def test_missing_fixture_id_ignored(monkeypatch, tmp_path):
    window = [_fx(None, "Marseille", "Paris Saint Germain", "2026-10-03T18:45:00+00:00")]
    provider = _FakeProvider(window=window)
    runtime = _runtime(monkeypatch, provider=provider, conn=_db(tmp_path))
    result = runtime.run_cycle()
    assert result["counters"]["fixtures_considered"] == 1
    assert result["counters"]["fixtures_selected"] == 0
    assert provider.requests_allowed == 1  # discovery only


def test_non_fra1_rows_are_ignored_by_collector(monkeypatch, tmp_path):
    provider = _FakeProvider(enabled=True)
    runtime = _runtime(monkeypatch, provider=provider, conn=_db(tmp_path))
    row = {
        "competition_id": "ENG1",
        "fixture_id": "some-id",
        "home_team_name": "A",
        "away_team_name": "B",
        "kickoff_utc": "2026-10-03T18:45:00+00:00",
        "status": "",
    }
    result = runtime._collector.collect([row])
    assert result["counters"]["provider_calls"] == 0
    assert result["counters"]["snapshots_written"] == 0


# --------------------------------------------------------------------------- dedupe / recently collected
def test_repeated_cycle_does_not_duplicate_snapshots(monkeypatch, tmp_path):
    provider = _FakeProvider(window=[_fx("p1", "Marseille", "Paris Saint Germain", "2026-10-03T18:45:00+00:00")])
    clock = _Clock()
    conn = _db(tmp_path)
    runtime = _runtime(monkeypatch, provider=provider, conn=conn, clock=clock)
    first = runtime.run_cycle()
    assert first["counters"]["snapshots_written"] > 0
    rows_after_first = conn.execute("select count(*) from market_snapshots").fetchone()[0]
    clock.value += 10_000.0
    second = runtime.run_cycle()
    assert second["counters"]["snapshots_written"] == 0
    assert second["counters"]["snapshots_reused"] > 0
    assert conn.execute("select count(*) from market_snapshots").fetchone()[0] == rows_after_first
    conn.close()


def test_recent_fixture_is_skipped(monkeypatch, tmp_path):
    provider = _FakeProvider(window=[_fx("p1", "Marseille", "Paris Saint Germain", "2026-10-03T18:45:00+00:00")])
    clock = _Clock()
    runtime = _runtime(monkeypatch, provider=provider, conn=_db(tmp_path), clock=clock)
    runtime.run_cycle()
    second = runtime.run_cycle()  # same clock < recent_skip window
    assert second["counters"]["fixtures_selected"] == 0
    assert second["counters"]["fixtures_skipped_recently_collected"] == 1
    # Discovery still happens (one governed call), but no match/odds calls.
    assert second["counters"]["provider_calls"] == 1


# --------------------------------------------------------------------------- config clamps
def test_runtime_config_clamps():
    assert runtime_mod.ShadowRuntimeConfig(max_fixtures_per_cycle=-5).max_fixtures_per_cycle == 0
    assert runtime_mod.ShadowRuntimeConfig(max_fixtures_per_cycle=100).max_fixtures_per_cycle == 2
    assert runtime_mod.ShadowRuntimeConfig(interval_seconds=1).interval_seconds == runtime_mod.MIN_INTERVAL_SECONDS
    assert runtime_mod.ShadowRuntimeConfig(recent_skip_seconds=-1).recent_skip_seconds == 0


# --------------------------------------------------------------------------- async worker (disabled)
def test_shadow_worker_disabled_makes_no_calls_or_writes(monkeypatch):
    monkeypatch.delenv(shadow.ENV_SHADOW_ENABLED, raising=False)
    monkeypatch.delenv(five.ENV_ENABLED, raising=False)
    provider = _FakeProvider(enabled=True)
    runtime = runtime_mod.FiveDollarShadowRuntime(provider=provider)

    async def scenario():
        stop = asyncio.Event()
        await scheduler_worker._run_shadow_runtime(runtime, stop)

    asyncio.run(scenario())
    assert provider.requests_allowed == 0
    assert runtime._conn is None


# --------------------------------------------------------------------------- firewall / isolation
def test_runtime_module_has_no_decision_paths():
    src = pathlib.Path(runtime_mod.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "build_fixture_market_evaluation",
        "_maybe_create_paper_entries",
        "paper_trade_entries",
        "DashboardServices",
        "market_provider",
        "portfolio",
        "manager",
    ):
        assert forbidden not in src


def test_scheduler_wiring_is_structural_and_isolated():
    src = pathlib.Path(scheduler_worker.__file__).read_text(encoding="utf-8")
    assert "FiveDollarShadowRuntime" in src
    assert "_run_shadow_runtime" in src
    assert "build_fixture_market_evaluation" not in src
    assert "_maybe_create_paper_entries" not in src


def test_runtime_creates_no_paper_or_decision_rows(monkeypatch, tmp_path):
    provider = _FakeProvider(window=[_fx("p1", "Marseille", "Paris Saint Germain", "2026-10-03T18:45:00+00:00")])
    conn = _db(tmp_path)
    runtime = _runtime(monkeypatch, provider=provider, conn=conn)
    runtime.run_cycle()
    assert conn.execute("select count(*) from paper_trade_entries").fetchone()[0] == 0
    assert conn.execute("select count(*) from paper_trade_updates").fetchone()[0] == 0
    conn.close()


# --------------------------------------------------------------------------- durable rotation (restart-safe)
_CAP = "2026-10-20T00:00:00+00:00"
_NOW = datetime.fromisoformat("2026-10-20T01:00:00+00:00")
_WINDOW3 = [
    _fx("p1", "Marseille", "Paris Saint Germain", "2026-10-22T18:45:00+00:00"),
    _fx("p2", "Lens", "Lyon", "2026-10-23T18:45:00+00:00"),
    _fx("p3", "Monaco", "Toulouse", "2026-10-24T15:00:00+00:00"),
]


def test_durable_recent_skip_survives_new_runtime_instance(monkeypatch, tmp_path):
    monkeypatch.setenv(shadow.ENV_SHADOW_ENABLED, "1")
    monkeypatch.setenv(five.ENV_ENABLED, "1")
    config = runtime_mod.ShadowRuntimeConfig(recent_skip_seconds=108000)
    quotes = [_quote("asian_handicap", "opening", "HOME", -0.25, 1.95, captured_at=_CAP)]
    conn = _db(tmp_path)

    runtime_a = runtime_mod.FiveDollarShadowRuntime(
        provider=_FakeProvider(window=_WINDOW3, quotes=quotes),
        config=config,
        conn=conn,
        now_fn=lambda: _NOW,
    )
    first = runtime_a.run_cycle()
    assert first["counters"]["fixtures_selected"] == 2
    assert conn.execute(
        "select count(distinct fixture_id) from market_snapshots where provider='5dollar-football-api'"
    ).fetchone()[0] == 2

    # Brand-new runtime instance (simulates a Railway restart/deploy).
    provider_b = _FakeProvider(window=_WINDOW3, quotes=quotes)
    runtime_b = runtime_mod.FiveDollarShadowRuntime(
        provider=provider_b,
        config=config,
        conn=conn,
        now_fn=lambda: _NOW,
    )
    second = runtime_b.run_cycle()
    third_id = _canon("Monaco", "Toulouse", "2026-10-24T15:00:00+00:00")
    assert second["counters"]["fixtures_selected"] == 1
    assert second["runtime"]["selected"][0]["fixture_id"] == third_id
    assert second["counters"]["fixtures_skipped_recently_collected"] == 2
    conn.close()


def test_durable_skip_makes_zero_match_odds_calls(monkeypatch, tmp_path):
    monkeypatch.setenv(shadow.ENV_SHADOW_ENABLED, "1")
    monkeypatch.setenv(five.ENV_ENABLED, "1")
    config = runtime_mod.ShadowRuntimeConfig(recent_skip_seconds=108000)
    window = _WINDOW3[:2]
    quotes = [_quote("asian_handicap", "opening", "HOME", -0.25, 1.95, captured_at=_CAP)]
    conn = _db(tmp_path)
    runtime_mod.FiveDollarShadowRuntime(
        provider=_FakeProvider(window=window, quotes=quotes), config=config, conn=conn, now_fn=lambda: _NOW
    ).run_cycle()

    provider_b = _FakeProvider(window=window, quotes=quotes)
    runtime_b = runtime_mod.FiveDollarShadowRuntime(
        provider=provider_b, config=config, conn=conn, now_fn=lambda: _NOW
    )
    result = runtime_b.run_cycle()
    assert result["counters"]["fixtures_selected"] == 0
    assert result["counters"]["fixtures_skipped_recently_collected"] == 2
    assert result["counters"]["provider_calls"] == 1  # discovery only
    assert provider_b.requests_allowed == 1
    conn.close()


def test_durable_read_error_fails_closed(monkeypatch, tmp_path):
    provider = _FakeProvider(window=[_fx("p1", "Marseille", "Paris Saint Germain", "2026-10-22T18:45:00+00:00")])
    runtime = _runtime(monkeypatch, provider=provider, conn=_db(tmp_path))

    def boom(_ids):
        raise sqlite3.OperationalError("no such table: market_snapshots")

    monkeypatch.setattr(runtime, "_persisted_latest_capture", boom)
    result = runtime.run_cycle()
    assert result["counters"]["fixtures_selected"] == 0
    assert result["counters"]["provider_calls"] == 1  # discovery only, no broad querying
    assert result["runtime"]["durable_read_failed"] is True


def test_empty_eligible_set_is_safe(monkeypatch, tmp_path):
    provider = _FakeProvider(window=[])
    runtime = _runtime(monkeypatch, provider=provider, conn=_db(tmp_path))
    result = runtime.run_cycle()
    assert result["counters"]["fixtures_considered"] == 0
    assert result["counters"]["fixtures_selected"] == 0
    assert result["counters"]["provider_calls"] == 1  # discovery only
    assert result["state"] == "enabled"


def test_shadow_hourly_cap_is_enforced_within_process(monkeypatch, tmp_path):
    config = runtime_mod.ShadowRuntimeConfig(recent_skip_seconds=0)
    provider = _FakeProvider(window=[_fx("p1", "Marseille", "Paris Saint Germain", "2026-10-22T18:45:00+00:00")])
    runtime = _runtime(monkeypatch, provider=provider, conn=_db(tmp_path), config=config, clock=_Clock())
    hourly = []
    rate_blocked = 0
    for _ in range(8):
        result = runtime.run_cycle()
        hourly.append(result["budget"]["hourly_calls"])
        rate_blocked += result["counters"]["rate_blocked"]
    assert max(hourly) <= 20
    assert rate_blocked > 0  # the 20/hour ceiling eventually blocks


# --------------------------------------------------------------------------- lifecycle / coverage reporting
def test_last_pre_match_observed_uses_own_timestamp(monkeypatch, tmp_path):
    conn = _db(tmp_path)
    kickoff = "2026-10-10T15:00:00+00:00"
    paper.record_market_snapshot(conn, _snapshot("f1", "asian_handicap", "2026-10-10T13:00:00+00:00", "opening", line=-0.25, odds=1.9, kickoff=kickoff))
    paper.record_market_snapshot(conn, _snapshot("f1", "asian_handicap", "2026-10-10T14:00:00+00:00", "closing", line=-0.25, odds=1.95, kickoff=kickoff))
    paper.record_market_snapshot(conn, _snapshot("f1", "asian_handicap", "2026-10-10T16:00:00+00:00", "inplay", is_pre_match=0, line=-0.25, odds=2.1, kickoff=kickoff))
    rows = reporting.last_pre_match_observed(conn, fixture_id="f1")
    assert len(rows) == 1
    assert rows[0]["last_pre_match_observed"] == "2026-10-10T14:00:00+00:00"
    assert rows[0]["provider_phase"] == "closing"
    conn.close()


def test_coverage_report_counts_missing_and_first_seen(monkeypatch, tmp_path):
    conn = _db(tmp_path)
    kickoff1 = "2026-10-10T15:00:00+00:00"
    fixtures = [
        {"fixture_id": "f1", "home_team_name": "Lens", "away_team_name": "Lyon", "kickoff_utc": kickoff1},
        {"fixture_id": "f2", "home_team_name": "Monaco", "away_team_name": "Toulouse", "kickoff_utc": "2026-10-11T15:00:00+00:00"},
    ]
    paper.record_market_snapshot(conn, _snapshot("f1", "asian_handicap", "2026-10-09T00:00:00+00:00", "opening", line=-0.25, odds=1.9, kickoff=kickoff1))
    paper.record_market_snapshot(conn, _snapshot("f1", "corner_total", "2026-10-09T00:00:00+00:00", "opening", line=8.5, odds=1.85, kickoff=kickoff1))
    report = reporting.build_coverage_report(conn, fixtures, now=datetime.fromisoformat("2026-10-09T06:00:00+00:00"))
    summary = report["summary"]
    assert summary["fixtures_discovered"] == 2
    assert summary["fixtures_ever_collected"] == 1
    assert summary["ah_coverage_count"] == 1
    assert summary["corner_coverage_count"] == 1
    assert summary["fixtures_missing_ah"] == ["f2"]
    assert summary["fixtures_missing_corners"] == ["f2"]
    f1 = next(f for f in report["fixtures"] if f["fixture_id"] == "f1")
    assert f1["corner"]["available"] is True
    # corner first seen 2026-10-09T00:00Z, kickoff 2026-10-10T15:00Z -> 39.0h
    assert f1["corner"]["hours_before_kickoff_when_first_seen"] == 39.0
    assert f1["last_pre_match_observed"] == "2026-10-09T00:00:00+00:00"
    conn.close()


def test_coverage_report_read_error_is_safe():
    class _BoomConn:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("no such table")

    report = reporting.build_coverage_report(
        _BoomConn(),
        [{"fixture_id": "f1", "home_team_name": "a", "away_team_name": "b", "kickoff_utc": "2026-10-10T15:00:00+00:00"}],
    )
    assert report.get("error") == "shadow_market_read_failed"


_WINDOW4 = [
    _fx("p1", "Marseille", "Paris Saint Germain", "2026-10-22T18:45:00+00:00"),
    _fx("p2", "Lens", "Lyon", "2026-10-23T18:45:00+00:00"),
    _fx("p3", "Monaco", "Toulouse", "2026-10-24T15:00:00+00:00"),
    _fx("p4", "Nantes", "Rennes", "2026-10-25T15:00:00+00:00"),
]


def _wake(conn, *, at: str):
    """One production-like wake: fresh runtime, one cycle, unchanged prices captured now."""
    quotes = [_quote("asian_handicap", "opening", "HOME", -0.25, 1.95, captured_at=at)]
    return runtime_mod.FiveDollarShadowRuntime(
        provider=_FakeProvider(window=_WINDOW4, quotes=quotes),
        config=runtime_mod.ShadowRuntimeConfig(recent_skip_seconds=3600, max_fixtures_per_cycle=2),
        conn=conn,
        now_fn=lambda: datetime.fromisoformat(at),
    ).run_cycle()


def _selected_ids(result):
    return [row["fixture_id"] for row in result["runtime"]["selected"]]


def test_unchanged_prices_still_rotate_selection_across_wakes(monkeypatch, tmp_path):
    # Production 2026-10-14: every wake re-fetched the same two earliest fixtures
    # (written=0 reused=16) because dedupe never advanced MAX(captured_at).
    monkeypatch.setenv(shadow.ENV_SHADOW_ENABLED, "1")
    monkeypatch.setenv(five.ENV_ENABLED, "1")
    conn = _db(tmp_path)
    first_two = [_canon("Marseille", "Paris Saint Germain", "2026-10-22T18:45:00+00:00"),
                 _canon("Lens", "Lyon", "2026-10-23T18:45:00+00:00")]
    last_two = [_canon("Monaco", "Toulouse", "2026-10-24T15:00:00+00:00"),
                _canon("Nantes", "Rennes", "2026-10-25T15:00:00+00:00")]

    assert _selected_ids(_wake(conn, at="2026-10-20T00:00:00+00:00")) == first_two
    assert _selected_ids(_wake(conn, at="2026-10-20T00:10:00+00:00")) == last_two
    # Hours later every fixture is due again; the earliest are re-polled with
    # identical prices (no new rows) ...
    reobserved = _wake(conn, at="2026-10-20T05:00:00+00:00")
    assert _selected_ids(reobserved) == first_two
    assert reobserved["counters"]["snapshots_written"] == 0
    # ... and the next wake must move on instead of re-fetching them again.
    assert _selected_ids(_wake(conn, at="2026-10-20T05:10:00+00:00")) == last_two
    conn.close()


def test_reobservation_advances_last_observed_without_rewriting_capture(monkeypatch, tmp_path):
    monkeypatch.setenv(shadow.ENV_SHADOW_ENABLED, "1")
    monkeypatch.setenv(five.ENV_ENABLED, "1")
    conn = _db(tmp_path)
    _wake(conn, at="2026-10-20T00:00:00+00:00")
    _wake(conn, at="2026-10-20T05:00:00+00:00")

    rows = conn.execute(
        "select captured_at, first_observed_at, last_observed_at from market_snapshots "
        "where provider='5dollar-football-api' and fixture_id = ?",
        (_canon("Marseille", "Paris Saint Germain", "2026-10-22T18:45:00+00:00"),),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["captured_at"] == "2026-10-20T00:00:00+00:00"
    assert rows[0]["first_observed_at"] == "2026-10-20T00:00:00+00:00"
    assert rows[0]["last_observed_at"] == "2026-10-20T05:00:00+00:00"
    conn.close()
