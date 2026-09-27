"""Tests for cross-restart persisted The Odds API snapshot reuse.

These tests prove that a fresh persisted ``market_snapshots`` row satisfies a
focused AH + Corners request without repurchasing already-fresh provider data,
while stale, wrong-line, wrong-fixture, and reference-semantics-incompatible
snapshots are always rejected back to the normal provider path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

# Import the dashboard services module first: the market_data package imports
# api_football, which imports dashboard.services, so establishing this order
# avoids the package-level circular import the rest of the suite already works
# around the same way.
from sports_ai.dashboard.services import DashboardConfig  # noqa: F401

from sports_ai.market_data.providers import NormalizedMarketSnapshot
from sports_ai.market_data.the_odds_api import (
    PERSISTED_REUSE_REASON_FIXTURE_MISMATCH,
    PERSISTED_REUSE_REASON_LINE_MISMATCH,
    TheOddsApiProvider,
    TheOddsApiProviderConfig,
)
from sports_ai.storage import football_paper_trading

FIXTURE_ID = "football-data.org:560546"
OTHER_FIXTURE_ID = "football-data.org:560547"
COMPETITION_ID = "ENG1"
INGESTION_VERSION = "the_odds_api_current_market_provider_v1"

QUOTA_HEADERS = {"x-requests-remaining": "495", "x-requests-used": "5", "x-requests-last": "1"}


def _iso(minutes_offset: float = 0.0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=minutes_offset)
    ).isoformat(timespec="seconds")


def _fixture_context(fixture_id: str = FIXTURE_ID) -> dict:
    return {
        "fixture_id": fixture_id,
        "competition_id": COMPETITION_ID,
        "competition_name": "Premier League",
        "division_code": "E0",
        "season": "2026",
        "home_team_name": "Manchester United",
        "away_team_name": "Liverpool",
        "kickoff_utc": _iso(minutes_offset=2880),
    }


def _snapshot(
    *,
    fixture_id: str = FIXTURE_ID,
    market: str,
    selection: str,
    line: float | None,
    odds: float,
    bookmaker: str = "pinnacle",
    source_timestamp: str | None = None,
    captured_at: str | None = None,
) -> NormalizedMarketSnapshot:
    is_reference = bookmaker == "pinnacle"
    market_key = "spreads" if market == "asian_handicap" else "alternate_totals_corners"
    provider_market_id = f"epl_event_123:{bookmaker}:{market_key}:{selection}"
    if line is not None:
        provider_market_id = f"{provider_market_id}:{line}"
    return NormalizedMarketSnapshot(
        provider="the-odds-api",
        fixture_id=fixture_id,
        provider_fixture_id="epl_event_123",
        captured_at=captured_at or _iso(-1),
        source_timestamp=source_timestamp or _iso(-5),
        market=market,
        selection=selection,
        line=line,
        decimal_odds=odds,
        bookmaker=bookmaker,
        ingestion_version=INGESTION_VERSION,
        provider_market_id=provider_market_id,
        is_pre_match=True,
        home_team_name="Manchester United",
        away_team_name="Liverpool",
        competition_id=COMPETITION_ID,
        competition_name="Premier League",
        kickoff_utc=_iso(minutes_offset=2880),
        freshness_state="FRESH",
        reference_designation="reference" if is_reference else "available",
        reference_rank=0 if is_reference else None,
    )


def _focused_ah_corners(
    *,
    source_timestamp: str | None = None,
    bookmaker: str = "pinnacle",
    ah_line: float = -0.5,
    corners_line: float = 9.5,
    fixture_id: str = FIXTURE_ID,
) -> list[NormalizedMarketSnapshot]:
    return [
        _snapshot(
            fixture_id=fixture_id,
            market="asian_handicap",
            selection="HOME",
            line=ah_line,
            odds=1.97,
            bookmaker=bookmaker,
            source_timestamp=source_timestamp,
        ),
        _snapshot(
            fixture_id=fixture_id,
            market="asian_handicap",
            selection="AWAY",
            line=ah_line,
            odds=1.93,
            bookmaker=bookmaker,
            source_timestamp=source_timestamp,
        ),
        _snapshot(
            fixture_id=fixture_id,
            market="corner_total",
            selection="OVER",
            line=corners_line,
            odds=1.97,
            bookmaker=bookmaker,
            source_timestamp=source_timestamp,
        ),
        _snapshot(
            fixture_id=fixture_id,
            market="corner_total",
            selection="UNDER",
            line=corners_line,
            odds=1.87,
            bookmaker=bookmaker,
            source_timestamp=source_timestamp,
        ),
    ]


def _stored(snapshot: NormalizedMarketSnapshot) -> football_paper_trading.StoredMarketSnapshot:
    return football_paper_trading.StoredMarketSnapshot(
        fixture_id=str(snapshot.fixture_id),
        provider=str(snapshot.provider),
        bookmaker=str(snapshot.bookmaker),
        market=str(snapshot.market),
        selection=str(snapshot.selection),
        odds=float(snapshot.decimal_odds),
        ingestion_version=str(snapshot.ingestion_version),
        line=None if snapshot.line is None else float(snapshot.line),
        provider_market_id=snapshot.provider_market_id,
        provider_timestamp=snapshot.source_timestamp,
        captured_at=str(snapshot.captured_at),
        is_pre_match=bool(snapshot.is_pre_match),
        provider_fixture_id=snapshot.provider_fixture_id,
        home_team_name=snapshot.home_team_name,
        away_team_name=snapshot.away_team_name,
        competition_id=snapshot.competition_id,
        competition_name=snapshot.competition_name,
        kickoff_utc=snapshot.kickoff_utc,
        freshness_state=snapshot.freshness_state,
    )


def _persist(db_path, snapshots: list[NormalizedMarketSnapshot]) -> None:
    with football_paper_trading.connect(db_path) as conn:
        football_paper_trading.initialize_database(conn)
        for snapshot in snapshots:
            football_paper_trading.record_market_snapshot(conn, _stored(snapshot))


def _fetch_rows(db_path, fixture_id: str = FIXTURE_ID) -> list[dict]:
    with football_paper_trading.connect(db_path) as conn:
        return football_paper_trading.fetch_market_snapshots(
            conn, fixture_id=fixture_id, initialize_schema=False
        )


def _provider(db_path, handler, config=None):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    reader = football_paper_trading.persisted_snapshot_reader(db_path)
    return TheOddsApiProvider(config=config, client=client, persisted_snapshot_reader=reader)


def _focused_env(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    monkeypatch.setenv("SPORTS_AI_FOOTBALL_LIVE_MARKET_FOCUS", "asian_handicap,corner_total")


def _blocking_handler(calls: dict):
    def handler(request):
        calls["count"] += 1
        return httpx.Response(500, request=request)

    return handler


def _league_event(source_iso: str) -> dict:
    return {
        "id": "epl_event_123",
        "sport_key": "soccer_epl",
        "commence_time": _iso(minutes_offset=2880),
        "home_team": "Manchester United",
        "away_team": "Liverpool",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": source_iso,
                "markets": [
                    {
                        "key": "spreads",
                        "last_update": source_iso,
                        "outcomes": [
                            {"name": "Manchester United", "price": 1.97, "point": -0.5},
                            {"name": "Liverpool", "price": 1.93, "point": 0.5},
                        ],
                    }
                ],
            }
        ],
    }


def _spreads_event(home: str, away: str, event_id: str = "epl_event_123") -> dict:
    return {
        "id": event_id,
        "sport_key": "soccer_epl",
        "commence_time": _iso(minutes_offset=2880),
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": _iso(-5),
                "markets": [
                    {
                        "key": "spreads",
                        "last_update": _iso(-5),
                        "outcomes": [
                            {"name": home, "price": 1.97, "point": -0.5},
                            {"name": away, "price": 1.93, "point": 0.5},
                        ],
                    }
                ],
            }
        ],
    }


def _alt_spreads_event(source_iso: str) -> dict:
    return {
        "id": "epl_event_123",
        "commence_time": _iso(minutes_offset=2880),
        "home_team": "Manchester United",
        "away_team": "Liverpool",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": source_iso,
                "markets": [
                    {
                        "key": "alternate_spreads",
                        "last_update": source_iso,
                        "outcomes": [
                            {"name": "Manchester United", "price": 1.91, "point": -0.5},
                            {"name": "Liverpool", "price": 1.95, "point": 0.5},
                        ],
                    },
                    {
                        "key": "alternate_totals_corners",
                        "last_update": source_iso,
                        "outcomes": [
                            {"name": "Over", "price": 1.97, "point": 9.5},
                            {"name": "Under", "price": 1.87, "point": 9.5},
                        ],
                    },
                ],
            }
        ],
    }


def _corners_event(source_iso: str) -> dict:
    return {
        "id": "epl_event_123",
        "commence_time": _iso(minutes_offset=2880),
        "home_team": "Manchester United",
        "away_team": "Liverpool",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": source_iso,
                "markets": [
                    {
                        "key": "alternate_totals_corners",
                        "last_update": source_iso,
                        "outcomes": [
                            {"name": "Over", "price": 1.97, "point": 9.5},
                            {"name": "Under", "price": 1.87, "point": 9.5},
                        ],
                    }
                ],
            }
        ],
    }


def _focused_handler(calls: dict, source_iso: str | None = None):
    source_iso = source_iso or _iso(-5)

    def handler(request):
        calls["count"] += 1
        if request.url.path.endswith("/sports/soccer_epl/odds"):
            return httpx.Response(200, json=[_league_event(source_iso)], headers=QUOTA_HEADERS, request=request)
        if "/events/" in request.url.path and request.url.path.endswith("/odds"):
            return httpx.Response(200, json=_corners_event(source_iso), headers=QUOTA_HEADERS, request=request)
        return httpx.Response(404, request=request)

    return handler


# ---------------------------------------------------------------------------
# 1. Fresh persisted snapshot avoids an equivalent provider call
# ---------------------------------------------------------------------------


def test_fresh_persisted_snapshot_avoids_provider_call(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    _persist(db_path, _focused_ah_corners())

    calls = {"count": 0}
    provider = _provider(db_path, _blocking_handler(calls))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())

    assert calls["count"] == 0
    assert {s.market for s in snapshots} == {"asian_handicap", "corner_total"}
    diagnostics = provider.diagnostics()
    assert diagnostics["persisted_snapshot_hits"] == 1
    assert diagnostics["provider_calls_avoided"] == 1
    assert diagnostics["provider_calls_made"] == 0


# ---------------------------------------------------------------------------
# 2. Stale snapshot invokes the normal provider path
# ---------------------------------------------------------------------------


def test_stale_persisted_snapshot_invokes_provider_path(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    _persist(db_path, _focused_ah_corners(source_timestamp=_iso(-120)))

    calls = {"count": 0}
    provider = _provider(db_path, _focused_handler(calls))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())

    assert calls["count"] > 0
    assert {s.market for s in snapshots} == {"asian_handicap", "corner_total"}
    diagnostics = provider.diagnostics()
    assert diagnostics["persisted_snapshot_hits"] == 0
    assert diagnostics["persisted_snapshot_stale"] >= 1


# ---------------------------------------------------------------------------
# 3/4/5. Exact line and fixture identity are never conflated
# ---------------------------------------------------------------------------


def test_wrong_ah_line_rejected(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    _persist(
        db_path,
        _focused_ah_corners(ah_line=-0.25),
    )
    rows = _fetch_rows(db_path)
    provider = TheOddsApiProvider()

    snapshot, reason = provider.resolve_persisted_snapshot(
        rows,
        fixture_id=FIXTURE_ID,
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        bookmaker="pinnacle",
        kickoff_utc=_iso(minutes_offset=2880),
    )

    assert snapshot is None
    assert reason == PERSISTED_REUSE_REASON_LINE_MISMATCH


def test_wrong_corners_line_rejected(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    _persist(db_path, _focused_ah_corners(corners_line=9.5))
    rows = _fetch_rows(db_path)
    provider = TheOddsApiProvider()

    snapshot, reason = provider.resolve_persisted_snapshot(
        rows,
        fixture_id=FIXTURE_ID,
        market="corner_total",
        selection="OVER",
        line=10.5,
        bookmaker="pinnacle",
        kickoff_utc=_iso(minutes_offset=2880),
    )

    assert snapshot is None
    assert reason == PERSISTED_REUSE_REASON_LINE_MISMATCH


def test_wrong_fixture_rejected(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    _persist(db_path, _focused_ah_corners())
    rows = _fetch_rows(db_path)
    provider = TheOddsApiProvider()

    snapshot, reason = provider.resolve_persisted_snapshot(
        rows,
        fixture_id=OTHER_FIXTURE_ID,
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        bookmaker="pinnacle",
        kickoff_utc=_iso(minutes_offset=2880),
    )

    assert snapshot is None
    assert reason == PERSISTED_REUSE_REASON_FIXTURE_MISMATCH


# ---------------------------------------------------------------------------
# 6. Incompatible bookmaker/reference semantics rejected
# ---------------------------------------------------------------------------


def test_incompatible_reference_semantics_rejected(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    # Fresh AH + corners but only from a non-reference bookmaker (bet365).
    _persist(db_path, _focused_ah_corners(bookmaker="bet365"))

    calls = {"count": 0}
    provider = _provider(db_path, _focused_handler(calls))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())

    assert calls["count"] > 0
    assert {s.market for s in snapshots} == {"asian_handicap", "corner_total"}
    diagnostics = provider.diagnostics()
    assert diagnostics["persisted_snapshot_hits"] == 0
    assert diagnostics["persisted_snapshot_semantics_mismatch"] >= 1


# ---------------------------------------------------------------------------
# 7. Process restart reuses a persisted fresh snapshot
# ---------------------------------------------------------------------------


def test_process_restart_reuses_fresh_persisted_snapshot(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"

    # Process A: fetch from provider (mocked) and persist.
    calls_a = {"count": 0}
    provider_a = _provider(db_path, _focused_handler(calls_a))
    snapshots_a = provider_a.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())
    assert calls_a["count"] > 0
    _persist(db_path, snapshots_a)

    # Process B: fresh process with empty in-process memory, same store.
    calls_b = {"count": 0}
    provider_b = _provider(db_path, _blocking_handler(calls_b))
    snapshots_b = provider_b.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())

    assert calls_b["count"] == 0
    assert {s.market for s in snapshots_b} == {"asian_handicap", "corner_total"}
    assert {(s.market, s.selection, s.line) for s in snapshots_b} == {
        ("asian_handicap", "HOME", -0.5),
        ("asian_handicap", "AWAY", -0.5),
        ("corner_total", "OVER", 9.5),
        ("corner_total", "UNDER", 9.5),
    }
    assert provider_b.diagnostics()["persisted_snapshot_hits"] == 1


# ---------------------------------------------------------------------------
# 8. Process restart rejects an expired persisted snapshot
# ---------------------------------------------------------------------------


def test_process_restart_rejects_expired_persisted_snapshot(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    _persist(db_path, _focused_ah_corners(source_timestamp=_iso(-120)))

    calls_b = {"count": 0}
    provider_b = _provider(db_path, _focused_handler(calls_b))
    snapshots_b = provider_b.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())

    assert calls_b["count"] > 0
    assert {s.market for s in snapshots_b} == {"asian_handicap", "corner_total"}
    assert provider_b.diagnostics()["persisted_snapshot_hits"] == 0
    assert provider_b.diagnostics()["persisted_snapshot_stale"] >= 1


# ---------------------------------------------------------------------------
# 9. Reuse does not create a duplicate paper position
# ---------------------------------------------------------------------------


def test_reuse_does_not_create_duplicate_paper_position(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    snapshots = _focused_ah_corners()
    _persist(db_path, snapshots)

    # Reuse returns the same snapshots; the sync loop re-records them. The
    # record_market_snapshot identity dedupe keeps a single snapshot row, so no
    # second paper position can ever be anchored to a duplicate entry snapshot.
    with football_paper_trading.connect(db_path) as conn:
        before = football_paper_trading.fetch_market_snapshots(conn, fixture_id=FIXTURE_ID)
        ids_before = {row["snapshot_id"] for row in before}
        for snapshot in snapshots:
            football_paper_trading.record_market_snapshot(conn, _stored(snapshot))
        after = football_paper_trading.fetch_market_snapshots(conn, fixture_id=FIXTURE_ID)

    assert len(after) == len(before)
    assert {row["snapshot_id"] for row in after} == ids_before


# ---------------------------------------------------------------------------
# 10. Focused AH + Corners reuse preserves exact lines and no extra markets
# ---------------------------------------------------------------------------


def test_focused_reuse_preserves_exact_lines_and_no_extra_markets(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    _persist(
        db_path,
        _focused_ah_corners(ah_line=-0.25, corners_line=10.0),
    )

    calls = {"count": 0}
    provider = _provider(db_path, _blocking_handler(calls))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())

    assert calls["count"] == 0
    assert {s.market for s in snapshots} == {"asian_handicap", "corner_total"}
    ah_lines = {s.line for s in snapshots if s.market == "asian_handicap"}
    corner_lines = {s.line for s in snapshots if s.market == "corner_total"}
    assert ah_lines == {-0.25}
    assert corner_lines == {10.0}


# ---------------------------------------------------------------------------
# 11/12/13. Featured AH vs alternate AH routing with an empty persisted store
# ---------------------------------------------------------------------------


def test_featured_ah_avoids_alternate_ah_with_empty_store(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    captured = {"event_markets": None}

    def handler(request):
        if request.url.path.endswith("/sports/soccer_epl/odds"):
            return httpx.Response(200, json=[_league_event(_iso(-5))], headers=QUOTA_HEADERS, request=request)
        if "/events/" in request.url.path and request.url.path.endswith("/odds"):
            captured["event_markets"] = request.url.params.get("markets")
            return httpx.Response(200, json=_corners_event(_iso(-5)), headers=QUOTA_HEADERS, request=request)
        return httpx.Response(404, request=request)

    provider = _provider(db_path, handler)
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())

    assert captured["event_markets"] == "alternate_totals_corners"
    assert {s.market for s in snapshots} == {"asian_handicap", "corner_total"}


def test_missing_featured_ah_reaches_alternate_fallback_with_empty_store(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    captured = {"event_markets": None}

    def handler(request):
        if request.url.path.endswith("/sports/soccer_epl/odds"):
            return httpx.Response(200, json=[], headers=QUOTA_HEADERS, request=request)
        if request.url.path.endswith("/sports/soccer_epl/events"):
            return httpx.Response(
                200,
                json=[{k: v for k, v in _league_event(_iso(-5)).items() if k != "bookmakers"}],
                headers=QUOTA_HEADERS,
                request=request,
            )
        if "/events/" in request.url.path and request.url.path.endswith("/odds"):
            captured["event_markets"] = request.url.params.get("markets")
            return httpx.Response(200, json=_alt_spreads_event(_iso(-5)), headers=QUOTA_HEADERS, request=request)
        return httpx.Response(404, request=request)

    provider = _provider(db_path, handler)
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())

    assert captured["event_markets"] == "alternate_spreads,alternate_totals_corners"
    assert {s.market for s in snapshots} == {"asian_handicap", "corner_total"}


def test_missing_ah_does_not_suppress_corners_with_empty_store(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    corners_only = {
        "id": "epl_event_123",
        "commence_time": _iso(minutes_offset=2880),
        "home_team": "Manchester United",
        "away_team": "Liverpool",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": _iso(-5),
                "markets": [
                    {
                        "key": "alternate_totals_corners",
                        "last_update": _iso(-5),
                        "outcomes": [
                            {"name": "Over", "price": 1.97, "point": 9.5},
                            {"name": "Under", "price": 1.87, "point": 9.5},
                        ],
                    }
                ],
            }
        ],
    }

    def handler(request):
        if request.url.path.endswith("/sports/soccer_epl/odds"):
            return httpx.Response(200, json=[_league_event(_iso(-5))], headers=QUOTA_HEADERS, request=request)
        if "/events/" in request.url.path and request.url.path.endswith("/odds"):
            return httpx.Response(200, json=corners_only, headers=QUOTA_HEADERS, request=request)
        return httpx.Response(404, request=request)

    provider = _provider(db_path, handler)
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())

    assert {s.market for s in snapshots} == {"asian_handicap", "corner_total"}
    assert any(s.market == "corner_total" for s in snapshots)


# ---------------------------------------------------------------------------
# 14. Event request cap still works
# ---------------------------------------------------------------------------


def test_event_request_cap_still_works_with_empty_store(monkeypatch, tmp_path):
    _focused_env(monkeypatch)
    db_path = tmp_path / "paper.sqlite"
    config = TheOddsApiProviderConfig(max_btts_event_requests=2)
    event_requests = {"count": 0}
    fixtures = [("Arsenal", "Chelsea"), ("Spurs", "West Ham"), ("Everton", "Leeds")]

    def league_events():
        return [
            _spreads_event(home, away, event_id=f"epl_{index}")
            for index, (home, away) in enumerate(fixtures, start=1)
        ]

    def handler(request):
        if request.url.path.endswith("/sports/soccer_epl/odds"):
            return httpx.Response(200, json=league_events(), headers=QUOTA_HEADERS, request=request)
        if "/events/" in request.url.path and request.url.path.endswith("/odds"):
            event_requests["count"] += 1
            return httpx.Response(200, json=_corners_event(_iso(-5)), headers=QUOTA_HEADERS, request=request)
        return httpx.Response(404, request=request)

    provider = _provider(db_path, handler, config=config)
    for home, away in fixtures:
        ctx = _fixture_context()
        ctx["home_team_name"] = home
        ctx["away_team_name"] = away
        provider.get_market_snapshot(FIXTURE_ID, fixture_context=ctx)

    assert event_requests["count"] == 2
    assert provider.diagnostics()["event_request_cap_reached"] >= 1


# ---------------------------------------------------------------------------
# 15. Disabled sync produces zero live provider calls
# ---------------------------------------------------------------------------


def test_not_configured_provider_issues_zero_live_calls(monkeypatch):
    monkeypatch.delenv("SPORTS_AI_THE_ODDS_API_KEY", raising=False)
    monkeypatch.setenv("SPORTS_AI_FOOTBALL_LIVE_MARKET_FOCUS", "asian_handicap,corner_total")
    provider = TheOddsApiProvider()

    assert provider.configured() is False
    with pytest.raises(Exception, match="NOT_CONFIGURED"):
        provider.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())
    assert provider.diagnostics()["request_count"] == 0
    assert provider.diagnostics()["provider_calls_made"] == 0


# ---------------------------------------------------------------------------
# 16. Default no-focus behavior remains backward compatible (no reuse)
# ---------------------------------------------------------------------------


def test_no_focus_mode_never_reuses_persisted_snapshots(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    monkeypatch.delenv("SPORTS_AI_FOOTBALL_LIVE_MARKET_FOCUS", raising=False)
    db_path = tmp_path / "paper.sqlite"
    _persist(db_path, _focused_ah_corners())

    calls = {"count": 0}
    provider = _provider(db_path, _focused_handler(calls))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=_fixture_context())

    assert calls["count"] > 0
    assert {s.market for s in snapshots} == {"asian_handicap", "corner_total"}
    assert provider.diagnostics()["persisted_snapshot_hits"] == 0
