from __future__ import annotations

from fastapi.testclient import TestClient

from sports_ai.dashboard.app import create_app
from sports_ai.dashboard.auth import AuthStore


class _StubServices:
    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def test_auth_store_creates_and_authenticates_user(tmp_path):
    store = AuthStore(tmp_path / "auth.sqlite")
    store.initialize()

    user = store.create_user("  USER@Example.com ", "correct-horse")

    assert user["email"] == "user@example.com"
    assert store.authenticate("user@example.com", "correct-horse")["id"] == user["id"]
    assert store.authenticate("user@example.com", "wrong-password") is None

    token = store.create_session(user["id"])
    assert store.get_user_for_session(token)["email"] == "user@example.com"

    store.delete_session(token)
    assert store.get_user_for_session(token) is None


def test_dashboard_auth_signup_login_and_logout_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "1")
    monkeypatch.setenv("SPORTS_AI_AUTH_COOKIE_SECURE", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_DB_PATH", str(tmp_path / "auth.sqlite"))

    app = create_app(services=_StubServices())
    with TestClient(app, follow_redirects=False) as client:
        protected = client.get("/")
        assert protected.status_code == 303
        assert protected.headers["location"].startswith("/login?next=")

        login = client.get("/login")
        assert login.status_code == 200
        assert "Welcome back" in login.text
        assert "SPORTS EDGE" in login.text

        signup = client.post(
            "/signup",
            data={
                "email": "client@example.com",
                "password": "strong-pass-123",
                "confirm_password": "strong-pass-123",
                "next": "/",
            },
        )
        assert signup.status_code == 303
        assert signup.headers["location"] == "/"
        assert "sports_edge_session=" in signup.headers["set-cookie"]

        signed_in_login = client.get("/login")
        assert signed_in_login.status_code == 303
        assert signed_in_login.headers["location"] == "/"

        logout = client.post("/logout")
        assert logout.status_code == 303
        assert logout.headers["location"] == "/login"

        protected_again = client.get("/")
        assert protected_again.status_code == 303
