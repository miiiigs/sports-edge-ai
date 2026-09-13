from __future__ import annotations

from sports_ai.dashboard.auth import AuthStore


def test_shared_username_password_mode_bootstraps_and_authenticates(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_USERNAME", "private-user")
    monkeypatch.setenv("SPORTS_AI_AUTH_PASSWORD", "test-password-123")

    store = AuthStore(tmp_path / "auth.sqlite")
    store.initialize()

    user = store.authenticate("private-user", "test-password-123")
    assert user is not None
    assert user["email"] == "private-user"
    assert store.authenticate("private-user", "wrong-password") is None
    assert store.authenticate("someone-else", "test-password-123") is None

    token = store.create_session(user["id"])
    assert store.get_user_for_session(token)["email"] == "private-user"


def test_shared_mode_rejects_existing_account_bypass(tmp_path, monkeypatch):
    store = AuthStore(tmp_path / "auth.sqlite")
    store.initialize()
    store.create_user("old@example.com", "old-password-123")

    monkeypatch.setenv("SPORTS_AI_AUTH_USERNAME", "private-user")
    monkeypatch.setenv("SPORTS_AI_AUTH_PASSWORD", "test-password-123")
    store.initialize()

    assert store.authenticate("old@example.com", "old-password-123") is None
    assert store.authenticate("private-user", "test-password-123") is not None
