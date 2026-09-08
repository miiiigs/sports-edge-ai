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
        assert "Continue as guest" in login.text

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


def test_guest_access_allows_reads_and_blocks_mutations(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "1")
    monkeypatch.setenv("SPORTS_AI_AUTH_COOKIE_SECURE", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_DB_PATH", str(tmp_path / "auth.sqlite"))

    app = create_app(services=_StubServices())

    @app.get("/guest-read-test")
    def guest_read_test():
        return {"ok": True}

    @app.post("/guest-write-test")
    def guest_write_test():
        return {"ok": True}

    with TestClient(app, follow_redirects=False) as client:
        guest = client.post("/guest", data={"next": "/guest-read-test"})
        assert guest.status_code == 303
        assert guest.headers["location"] == "/guest-read-test"
        assert "sports_edge_guest=1" in guest.headers["set-cookie"]

        read_response = client.get("/guest-read-test")
        assert read_response.status_code == 200
        assert read_response.json() == {"ok": True}

        write_response = client.post("/guest-write-test")
        assert write_response.status_code == 403
        assert write_response.json()["detail"] == "Sign in required for this action"

        login = client.get("/login")
        assert login.status_code == 200
