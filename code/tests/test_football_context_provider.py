from __future__ import annotations

from types import SimpleNamespace

from sports_ai.dashboard import football_context_provider as context_module
from sports_ai.dashboard.football_context_provider import ApiFootballContextProvider
from sports_ai.dashboard.provider_budget import ACTION_BLOCKED_GATE


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}
        self.text = ""
        self.is_error = status_code >= 400

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payloads: dict[str, dict], **_kwargs):
        self._payloads = payloads

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, endpoint, params=None):
        _ = params
        return _FakeResponse(self._payloads[endpoint])


class _BlockedBudget:
    def issue(self, **_kwargs):
        return SimpleNamespace(
            action=ACTION_BLOCKED_GATE,
            cached_payload=None,
            allow=False,
            reason="paid provider calls disabled",
        )


def test_context_provider_normalizes_lineups_and_injuries(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")
    payloads = {
        context_module.API_FOOTBALL_LINEUPS_ENDPOINT: {
            "results": 2,
            "errors": [],
            "response": [
                {
                    "team": {"id": 10, "name": "Home FC"},
                    "formation": "4-3-3",
                    "coach": {"id": 1, "name": "Home Coach"},
                    "startXI": [
                        {"player": {"id": 101, "name": "Starter One", "number": 9, "pos": "F", "grid": "1:1"}}
                    ],
                    "substitutes": [
                        {"player": {"id": 102, "name": "Sub One", "number": 19, "pos": "F"}}
                    ],
                },
                {
                    "team": {"id": 20, "name": "Away FC"},
                    "formation": "4-2-3-1",
                    "coach": {"id": 2, "name": "Away Coach"},
                    "startXI": [
                        {"player": {"id": 201, "name": "Away Starter", "number": 8, "pos": "M"}}
                    ],
                    "substitutes": [],
                },
            ],
        },
        context_module.API_FOOTBALL_INJURIES_ENDPOINT: {
            "results": 1,
            "errors": [],
            "response": [
                {
                    "fixture": {"id": 123},
                    "team": {"id": 10, "name": "Home FC"},
                    "player": {
                        "id": 111,
                        "name": "Injured Forward",
                        "type": "Missing Fixture",
                        "reason": "Hamstring",
                    },
                }
            ],
        },
    }
    monkeypatch.setattr(
        context_module.httpx,
        "Client",
        lambda **kwargs: _FakeClient(payloads, **kwargs),
    )

    context = ApiFootballContextProvider().get_fixture_context("123")

    assert context["provider_fixture_id"] == "123"
    assert context["lineups"]["evidence_state"] == "AVAILABLE"
    assert context["lineups"]["lineups"][0]["formation"] == "4-3-3"
    assert context["lineups"]["lineups"][0]["start_xi"][0]["player_id"] == 101
    assert context["injuries"]["evidence_state"] == "AVAILABLE"
    assert context["injuries"]["injuries"][0]["player_name"] == "Injured Forward"
    assert context["injuries"]["injuries"][0]["reason"] == "Hamstring"


def test_context_provider_respects_shared_budget_block_without_http(monkeypatch):
    provider = ApiFootballContextProvider()
    provider.attach_request_budget(_BlockedBudget())

    def _unexpected_client(**_kwargs):
        raise AssertionError("HTTP client must not be created when the shared budget blocks the request")

    monkeypatch.setattr(context_module.httpx, "Client", _unexpected_client)

    lineups = provider.get_lineups("123")

    assert lineups["request_state"] == ACTION_BLOCKED_GATE
    assert lineups["evidence_state"] == "MISSING"
    assert lineups["lineups"] == []
