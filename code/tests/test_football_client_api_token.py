from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from sports_ai.dashboard.app import create_app
from sports_ai.dashboard.auth import SESSION_COOKIE_NAME
from sports_ai.dashboard.client_api_token import (
    CLIENT_API_READ_PATHS,
    CLIENT_API_TOKEN_ENV_VAR,
    MIN_CLIENT_API_TOKEN_LENGTH,
    configured_token,
    is_read_only_client_request,
    presented_bearer_token,
)

TOKEN = "read-only-unified-client-token-0123456789"

ALLOWED_PATHS = (
    "/api/candidates",
    "/api/performance",
    "/api/system/status",
)

# Routes the unified Tennis transport layer may list, but which the production
# machine credential must NOT authorize. GET with a valid token must still be
# treated as unauthenticated on these.
REMOVED_CAPABILITY_PATHS = (
    "/api/predictions",
    "/api/analysis",
    "/api/fixtures/next",
    "/api/fixtures/today",
    "/api/fixtures/live",
    "/api/fixtures/upcoming",
)


class _StubConfig:
    def __init__(self, tmp_path):
        self.paper_trading_db_path = tmp_path / "paper_trading.sqlite"
        self.dashboard_db_path = tmp_path / "dashboard.sqlite"
        self.model_pick_db_path = tmp_path / "model_picks.sqlite"
        self.display_timezone = "Asia/Manila"


class _StubServices:
    def __init__(self, tmp_path):
        self.config = _StubConfig(tmp_path)

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def get_system_status(self) -> dict:
        return {"status": "ok"}

    def get_candidates_payload(self) -> dict:
        return {"rows": [], "connected": True}

    def get_performance_payload(self) -> dict:
        return {"summary": {}}

    def get_predictions_payload(self) -> dict:
        return {"predictions": []}

    def get_health_payload(self) -> dict:
        return {"status": "ok"}


def _app_with_auth(tmp_path, monkeypatch, token: str | None):
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "1")
    monkeypatch.setenv("SPORTS_AI_AUTH_COOKIE_SECURE", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_DB_PATH", str(tmp_path / "auth.sqlite"))
    if token is None:
        monkeypatch.delenv(CLIENT_API_TOKEN_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(CLIENT_API_TOKEN_ENV_VAR, token)
    return create_app(services=_StubServices(tmp_path))


def _app_with_auth_disabled(tmp_path, monkeypatch, token: str | None):
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_DB_PATH", str(tmp_path / "auth.sqlite"))
    if token is None:
        monkeypatch.delenv(CLIENT_API_TOKEN_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(CLIENT_API_TOKEN_ENV_VAR, token)
    return create_app(services=_StubServices(tmp_path))


def _bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_presented_bearer_token_parsing():
    assert presented_bearer_token("Bearer abc") == "abc"
    assert presented_bearer_token("bearer abc") == "abc"
    assert presented_bearer_token("Bearer   abc  ") == "abc"
    assert presented_bearer_token("Basic abc") is None
    assert presented_bearer_token("Bearer") is None
    assert presented_bearer_token("Bearer   ") is None
    assert presented_bearer_token("") is None
    assert presented_bearer_token(None) is None


def test_service_allowlist_is_exactly_the_three_required_paths():
    assert CLIENT_API_READ_PATHS == frozenset(ALLOWED_PATHS)
    assert "/health" not in CLIENT_API_READ_PATHS
    for path in REMOVED_CAPABILITY_PATHS:
        assert path not in CLIENT_API_READ_PATHS


def test_is_read_only_client_request_allows_only_allowlisted_get_and_head(monkeypatch):
    monkeypatch.setenv(CLIENT_API_TOKEN_ENV_VAR, TOKEN)

    for path in ALLOWED_PATHS:
        assert is_read_only_client_request(
            method="GET", path=path, authorization_header=f"Bearer {TOKEN}"
        )
        assert is_read_only_client_request(
            method="HEAD", path=path, authorization_header=f"Bearer {TOKEN}"
        )

    assert not is_read_only_client_request(
        method="POST", path="/api/candidates", authorization_header=f"Bearer {TOKEN}"
    )
    assert not is_read_only_client_request(
        method="GET", path="/health", authorization_header=f"Bearer {TOKEN}"
    )


def test_is_read_only_client_request_denies_removed_transport_paths(monkeypatch):
    monkeypatch.setenv(CLIENT_API_TOKEN_ENV_VAR, TOKEN)

    for path in REMOVED_CAPABILITY_PATHS:
        assert not is_read_only_client_request(
            method="GET", path=path, authorization_header=f"Bearer {TOKEN}"
        )
        assert not is_read_only_client_request(
            method="HEAD", path=path, authorization_header=f"Bearer {TOKEN}"
        )

    assert not is_read_only_client_request(
        method="GET", path="/api/candidates", authorization_header="Bearer wrong"
    )
    assert not is_read_only_client_request(
        method="GET", path="/api/candidates", authorization_header=None
    )

    monkeypatch.delenv(CLIENT_API_TOKEN_ENV_VAR, raising=False)
    assert not is_read_only_client_request(
        method="GET", path="/api/candidates", authorization_header=f"Bearer {TOKEN}"
    )


def test_is_read_only_client_request_rejects_non_ascii_without_error(monkeypatch):
    monkeypatch.setenv(CLIENT_API_TOKEN_ENV_VAR, TOKEN)

    assert not is_read_only_client_request(
        method="GET", path="/api/candidates", authorization_header="Bearer caf\u00e9"
    )
    assert not is_read_only_client_request(
        method="GET", path="/api/candidates", authorization_header="Bearer \u2603"
    )


def test_read_only_token_allows_allowlisted_get_and_head(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        status = client.get("/api/system/status", headers=_bearer())
        assert status.status_code == 200
        assert status.json() == {"status": "ok"}

        candidates = client.get("/api/candidates", headers=_bearer())
        assert candidates.status_code == 200
        assert candidates.json()["connected"] is True

        performance = client.get("/api/performance", headers=_bearer())
        assert performance.status_code == 200

        # HEAD is part of the read contract and is actually served.
        for path in ALLOWED_PATHS:
            head = client.head(path, headers=_bearer())
            assert head.status_code == 200, path
            assert head.content == b"", path


def test_read_only_token_does_not_authorize_removed_endpoints(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        for path in REMOVED_CAPABILITY_PATHS:
            assert client.get(path, headers=_bearer()).status_code == 401, path
            assert client.head(path, headers=_bearer()).status_code == 401, path


def test_read_only_token_denied_without_or_wrong_token(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/api/system/status").status_code == 401
        assert client.get("/api/system/status", headers=_bearer("wrong")).status_code == 401
        assert (
            client.get(
                "/api/system/status",
                headers={"Authorization": f"Basic {TOKEN}"},
            ).status_code
            == 401
        )


def test_read_only_token_cannot_mutate(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        response = client.post("/api/fixtures/refresh", headers=_bearer())
        assert response.status_code == 401


def test_read_only_token_cannot_reach_non_allowlisted_api(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/api/reconciliation/report", headers=_bearer())
        assert response.status_code == 401


def test_read_only_token_cannot_open_html_pages(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/", headers=_bearer())
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")


def test_read_only_token_is_not_echoed(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/api/system/status", headers=_bearer())
        assert response.status_code == 200
        assert TOKEN not in response.text
        assert TOKEN not in "".join(
            f"{key}{value}" for key, value in response.headers.items()
        )


def test_blank_configured_token_never_authorises(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, "   ")
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/api/system/status", headers=_bearer("   "))
        assert response.status_code == 401


def test_short_configured_token_never_authorises(tmp_path, monkeypatch):
    weak = "short-placeholder"
    assert len(weak) < MIN_CLIENT_API_TOKEN_LENGTH
    monkeypatch.setenv(CLIENT_API_TOKEN_ENV_VAR, weak)
    assert configured_token() == ""
    assert not is_read_only_client_request(
        method="GET", path="/api/candidates", authorization_header=f"Bearer {weak}"
    )
    app = _app_with_auth(tmp_path, monkeypatch, weak)
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/api/system/status", headers=_bearer(weak)).status_code == 401


def test_health_is_public_without_token(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/health")
        assert response.status_code == 200


def test_auth_disabled_public_behavior_unchanged_with_token_configured(tmp_path, monkeypatch):
    app = _app_with_auth_disabled(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        # With auth disabled the middleware is a passthrough: configuring the
        # service token must not introduce any new gating.
        assert client.get("/api/system/status").status_code == 200
        assert client.get("/health").status_code == 200
        # Even a non-allowlisted read path stays public while auth is disabled.
        assert client.get("/api/predictions").status_code == 200
        # Presenting the token changes nothing while auth is disabled.
        assert client.get("/api/system/status", headers=_bearer()).status_code == 200


def test_guest_cookie_get_access_unchanged(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set("sports_edge_guest", "1")
        # Guest GET continues to work, including on a path the service token
        # does not authorize.
        assert client.get("/api/system/status").status_code == 200
        assert client.get("/api/predictions").status_code == 200
        # Existing guest write restriction is unchanged.
        assert client.post("/api/fixtures/refresh").status_code == 403


def test_query_parameters_never_authenticate(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        for param in ("token", "api_key", "access_token"):
            response = client.get(f"/api/system/status?{param}={TOKEN}")
            assert response.status_code == 401, param
        assert client.get(f"/api/candidates?access_token={TOKEN}").status_code == 401


def test_bearer_request_sets_no_session_cookie(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/api/system/status", headers=_bearer())
        assert response.status_code == 200
        set_cookie = " ".join(response.headers.get_list("set-cookie"))
        assert SESSION_COOKIE_NAME not in set_cookie
        assert all(cookie.name != SESSION_COOKIE_NAME for cookie in client.cookies.jar)


def test_service_token_not_persisted_in_auth_sqlite(tmp_path, monkeypatch):
    app = _app_with_auth(tmp_path, monkeypatch, TOKEN)
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/api/system/status", headers=_bearer()).status_code == 200

    auth_db = tmp_path / "auth.sqlite"
    assert auth_db.exists()
    assert TOKEN.encode("utf-8") not in auth_db.read_bytes()

    with sqlite3.connect(auth_db) as conn:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        assert "sessions" in tables
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}"):
                assert TOKEN not in " ".join(str(value) for value in row)
