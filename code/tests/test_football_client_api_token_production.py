"""Read-only client token against the real production web composition.

``test_football_client_api_token.py`` exercises the token with stub services on
the bare ``create_app``. Production instead serves
``sports_ai.dashboard.owner_access:app``: ``create_app`` over
``LowMemoryWebDashboardServices`` with ``owner_access_policy`` layered on top,
signup and guest disabled, and the owner signing in with shared credentials.
These tests rebuild that exact composition against an empty temporary data
directory so the token is verified alongside the owner session, and pin the
payload keys the unified Sports Edge client reads.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from sports_ai.dashboard.app import create_app
from sports_ai.dashboard.auth import SESSION_COOKIE_NAME
from sports_ai.dashboard.client_api_token import (
    CLIENT_API_READ_PATHS,
    CLIENT_API_TOKEN_ENV_VAR,
    MIN_CLIENT_API_TOKEN_LENGTH,
)
from sports_ai.dashboard.low_memory_app import LowMemoryWebDashboardServices
from sports_ai.dashboard.owner_access import owner_access_policy

TOKEN = "unified-client-production-composition-token-0123456789"
OWNER_USERNAME = "owner"
OWNER_PASSWORD = "owner-password-for-tests-only"

# Keys read by the unified client (sports-edge-tennis
# ``dashboard/football_client.py``). Removing one silently degrades the
# unified Football pages, so the contract is pinned here.
CLIENT_CONSUMED_KEYS = {
    "/api/candidates": {"rows", "visible_rows", "pagination", "summary", "connected"},
    "/api/performance": {
        "market_breakdown",
        "current_paper_summary",
        "window_rows",
        "recent_paper_bets",
        "client_history",
        "clv_status_label",
        "current_streak",
    },
    "/api/system/status": {"fixture_feed", "prediction_engine", "odds_feed", "market_sync", "storage"},
    "/api/monitoring/football": {
        "schema_version",
        "generated_at",
        "operational",
        "model_performance",
        "qualified_betting",
        "high_confidence_shadow",
    },
}

NON_ALLOWLISTED_READS = (
    "/api/predictions",
    "/api/analysis",
    "/api/reconciliation/report",
    "/api/fixtures/today",
    "/api/fixtures/upcoming",
)
HTML_AND_SCHEMA_PATHS = ("/", "/today", "/performance", "/docs", "/redoc", "/openapi.json")


def _production_app(tmp_path, monkeypatch, *, token: str | None = TOKEN):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTS_AI_RUNTIME_ROLE", "web")
    monkeypatch.setenv("SPORTS_AI_ALLOW_PAID_PROVIDER_CALLS", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "1")
    monkeypatch.setenv("SPORTS_AI_AUTH_COOKIE_SECURE", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_DB_PATH", str(tmp_path / "sports_edge_auth.sqlite"))
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_SIGNUP", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_GUEST", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_USERNAME", OWNER_USERNAME)
    monkeypatch.setenv("SPORTS_AI_AUTH_PASSWORD", OWNER_PASSWORD)
    if token is None:
        monkeypatch.delenv(CLIENT_API_TOKEN_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(CLIENT_API_TOKEN_ENV_VAR, token)
    # Mirrors owner_access.py: the policy middleware wraps the low-memory app.
    app = create_app(services=LowMemoryWebDashboardServices())
    app.middleware("http")(owner_access_policy)
    return app


def _bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    with TestClient(_production_app(tmp_path, monkeypatch), follow_redirects=False) as test_client:
        yield test_client


def _owner_login(test_client: TestClient) -> None:
    response = test_client.post(
        "/login",
        data={"email": OWNER_USERNAME, "password": OWNER_PASSWORD, "next": "/"},
    )
    assert response.status_code == 303
    assert test_client.cookies.get(SESSION_COOKIE_NAME)


def test_token_reads_exact_allowlist_with_json_contract(client):
    for path in sorted(CLIENT_API_READ_PATHS):
        response = client.get(path, headers=_bearer())
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("application/json"), path
        payload = response.json()
        assert isinstance(payload, dict), path
        missing = CLIENT_CONSUMED_KEYS[path] - set(payload)
        assert not missing, (path, missing)


def test_token_head_is_served_without_body(client):
    for path in sorted(CLIENT_API_READ_PATHS):
        response = client.head(path, headers=_bearer())
        assert response.status_code == 200, path
        assert response.content == b"", path
        assert client.head(path).status_code == 401, path


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer wrong-token-of-sufficient-length-0123456789"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
        {"Authorization": f"Bearer {TOKEN}x"},
        {"Authorization": f"Bearer {TOKEN[:-1]}"},
        {"X-Api-Key": TOKEN},
    ],
)
def test_token_rejections_return_json_401(client, headers):
    for path in sorted(CLIENT_API_READ_PATHS):
        response = client.get(path, headers=headers)
        assert response.status_code == 401, (path, headers)
        assert response.json() == {"detail": "Authentication required"}


def test_token_never_authorises_mutation_or_other_methods(client):
    for path in sorted(CLIENT_API_READ_PATHS) + ["/api/fixtures/refresh"]:
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            response = client.request(method, path, headers=_bearer())
            assert response.status_code == 401, (method, path)


def test_token_never_reaches_other_api_html_or_schema(client):
    for path in NON_ALLOWLISTED_READS:
        assert client.get(path, headers=_bearer()).status_code == 401, path
    for path in HTML_AND_SCHEMA_PATHS:
        response = client.get(path, headers=_bearer())
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login"), path
    for path in sorted(CLIENT_API_READ_PATHS):
        assert client.get(path + "/", headers=_bearer()).status_code == 401, path


def test_token_does_not_reopen_signup_or_guest(client):
    assert client.post("/signup", headers=_bearer()).status_code == 403
    assert client.post("/guest", headers=_bearer()).status_code == 403


def test_owner_session_is_unchanged_and_independent_of_token(client):
    assert client.get("/api/predictions").status_code == 401
    _owner_login(client)
    for path in sorted(CLIENT_API_READ_PATHS) + list(NON_ALLOWLISTED_READS[:3]) + ["/"]:
        assert client.get(path).status_code == 200, path
    # A wrong bearer header never downgrades a valid owner session.
    assert client.get("/api/predictions", headers=_bearer("wrong")).status_code == 200


def test_token_request_creates_no_session(client):
    response = client.get("/api/system/status", headers=_bearer())
    assert response.status_code == 200
    assert SESSION_COOKIE_NAME not in " ".join(response.headers.get_list("set-cookie"))
    # The cookie jar stays empty, so the next request without the header is denied.
    assert client.get("/api/system/status").status_code == 401


def test_token_absent_from_logs_and_responses(client, caplog):
    caplog.set_level(logging.DEBUG)
    for path in sorted(CLIENT_API_READ_PATHS):
        response = client.get(path, headers=_bearer())
        assert TOKEN not in response.text
        assert all(TOKEN not in value for value in response.headers.values())
    client.get("/api/predictions", headers=_bearer())
    assert TOKEN not in caplog.text


def test_unset_token_disables_bearer_path(tmp_path, monkeypatch):
    app = _production_app(tmp_path, monkeypatch, token=None)
    with TestClient(app, follow_redirects=False) as test_client:
        assert test_client.get("/api/candidates", headers=_bearer()).status_code == 401
        _owner_login(test_client)
        assert test_client.get("/api/candidates").status_code == 200


def test_short_configured_token_fails_closed(tmp_path, monkeypatch):
    weak = "x" * (MIN_CLIENT_API_TOKEN_LENGTH - 1)
    app = _production_app(tmp_path, monkeypatch, token=weak)
    with TestClient(app, follow_redirects=False) as test_client:
        assert test_client.get("/api/candidates", headers=_bearer(weak)).status_code == 401
