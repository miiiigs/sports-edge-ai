from __future__ import annotations

import pytest

from sports_ai.dashboard.auth import AuthStore
from sports_ai.dashboard.owner_access import guest_allowed, signup_allowed
from sports_ai.dashboard.runtime_supervisor import build_child_specs


def test_auth_store_optional_owner_email_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOWED_EMAILS", " Owner@Example.com , second@example.com ")
    store = AuthStore(tmp_path / "auth.sqlite")
    store.initialize()

    owner = store.create_user("owner@example.com", "strong-pass-123")
    assert owner["email"] == "owner@example.com"

    with pytest.raises(ValueError, match="not authorized"):
        store.create_user("other@example.com", "strong-pass-123")


def test_auth_store_empty_allowlist_preserves_existing_signup_behavior(tmp_path, monkeypatch):
    monkeypatch.delenv("SPORTS_AI_AUTH_ALLOWED_EMAILS", raising=False)
    store = AuthStore(tmp_path / "auth.sqlite")
    store.initialize()

    user = store.create_user("client@example.com", "strong-pass-123")
    assert user["email"] == "client@example.com"


def test_owner_access_policy_flags_default_to_existing_behavior(monkeypatch):
    monkeypatch.delenv("SPORTS_AI_AUTH_ALLOW_SIGNUP", raising=False)
    monkeypatch.delenv("SPORTS_AI_AUTH_ALLOW_GUEST", raising=False)

    assert signup_allowed() is True
    assert guest_allowed() is True


def test_owner_access_policy_flags_can_lock_signup_and_guest(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_SIGNUP", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_GUEST", "false")

    assert signup_allowed() is False
    assert guest_allowed() is False


def test_production_supervisor_serves_owner_access_wrapper(monkeypatch):
    monkeypatch.setenv("PORT", "9123")
    specs = build_child_specs()

    assert specs[0].name == "web"
    assert "sports_ai.dashboard.owner_access:app" in specs[0].argv
    assert specs[1].name == "scheduler"
