from __future__ import annotations

from types import SimpleNamespace

from sports_ai.dashboard import scoped_fixture_provider as scoped
from sports_ai.dashboard.models import FIXTURE_STATUS_LIVE


def _provider(monkeypatch):
    provider = scoped.ScopedApiFootballFixtureProvider()
    monkeypatch.setattr(provider, "configured", lambda: True)
    monkeypatch.setattr(provider, "_league_map", lambda: {"E0": 39, "E1": 40, "SP1": 140})
    return provider


def test_live_scope_is_stable_and_deduplicated(monkeypatch) -> None:
    provider = _provider(monkeypatch)
    assert provider._configured_live_scope() == "39-40-140"


def test_live_request_uses_one_configured_league_scope(monkeypatch) -> None:
    provider = _provider(monkeypatch)
    seen = []
    fixture = SimpleNamespace(
        fixture_id="api-football:1",
        kickoff_utc="2026-09-14T12:00:00+00:00",
        status=FIXTURE_STATUS_LIVE,
    )

    def request(*, params):
        seen.append(dict(params))
        return [{"league": {"id": 39}, "fixture": {"id": 1}}]

    monkeypatch.setattr(provider, "_request_fixtures", request)
    monkeypatch.setattr(provider, "_normalize_fixture", lambda row: fixture)

    result = provider.get_live_fixtures()

    assert seen == [{"live": "39-40-140"}]
    assert result == [fixture]


def test_scoped_provider_still_filters_out_of_scope_rows(monkeypatch) -> None:
    provider = _provider(monkeypatch)
    seen_normalize = []

    monkeypatch.setattr(
        provider,
        "_request_fixtures",
        lambda *, params: [
            {"league": {"id": 39}, "fixture": {"id": 1}},
            {"league": {"id": 999}, "fixture": {"id": 2}},
        ],
    )

    def normalize(row):
        seen_normalize.append(int(row["league"]["id"]))
        return SimpleNamespace(
            fixture_id=f"api-football:{row['fixture']['id']}",
            kickoff_utc="2026-09-14T12:00:00+00:00",
            status=FIXTURE_STATUS_LIVE,
        )

    monkeypatch.setattr(provider, "_normalize_fixture", normalize)

    result = provider.get_live_fixtures()

    assert seen_normalize == [39]
    assert [row.fixture_id for row in result] == ["api-football:1"]


def test_provider_error_falls_back_to_proven_live_all_query(monkeypatch) -> None:
    provider = _provider(monkeypatch)
    seen = []

    def request(*, params):
        seen.append(dict(params))
        return []

    monkeypatch.setattr(provider, "_request_fixtures", request)
    monkeypatch.setattr(
        provider,
        "sync_diagnostics",
        lambda: {"queries": [{"errors": {"provider": "bad scoped query"}}]},
    )

    assert provider.get_live_fixtures() == []
    assert seen == [{"live": "39-40-140"}, {"live": "all"}]
