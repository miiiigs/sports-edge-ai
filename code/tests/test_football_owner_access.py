from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from sports_ai.dashboard.app import _template_directory
from sports_ai.dashboard.auth import AuthStore
import sports_ai.dashboard.owner_access as owner_access
from sports_ai.dashboard.owner_access import (
    guest_allowed,
    owner_access_policy,
    signup_allowed,
)
from sports_ai.dashboard.runtime_supervisor import build_child_specs


def _request(path: str, *, method: str = "GET", cookie: str | None = None) -> Request:
    headers = []
    if cookie is not None:
        headers.append((b"cookie", cookie.encode("latin-1")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
            "app": owner_access.app,
        }
    )


async def _allow(_request: Request):
    return PlainTextResponse("ok")


def _render_login() -> str:
    request = _request("/login")
    request.state.owner_signup_allowed = signup_allowed()
    request.state.owner_guest_allowed = guest_allowed()
    templates = Jinja2Templates(directory=str(_template_directory()))
    return templates.get_template("auth.html").render(
        request=request,
        mode="login",
        next_path="/",
        error=None,
        email="",
    )


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


def test_locked_owner_mode_hides_signup_and_guest_controls(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_SIGNUP", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_GUEST", "0")

    html = _render_login()

    assert "Continue as guest" not in html
    assert ">Sign up<" not in html
    assert "Create an account" not in html
    assert ">Sign in<" in html


def test_default_auth_ui_preserves_signup_and_guest_controls(monkeypatch):
    monkeypatch.delenv("SPORTS_AI_AUTH_ALLOW_SIGNUP", raising=False)
    monkeypatch.delenv("SPORTS_AI_AUTH_ALLOW_GUEST", raising=False)

    html = _render_login()

    assert "Continue as guest" in html
    assert ">Sign up<" in html
    assert "Create an account" in html


def test_disabled_signup_redirects_get_and_rejects_post(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "1")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_SIGNUP", "0")

    get_response = asyncio.run(owner_access_policy(_request("/signup"), _allow))
    assert get_response.status_code == 303
    assert get_response.headers["location"] == "/login"

    post_response = asyncio.run(
        owner_access_policy(_request("/signup", method="POST"), _allow)
    )
    assert post_response.status_code == 403
    assert json.loads(post_response.body)["detail"] == "Account creation is disabled"


def test_disabled_guest_route_and_stale_guest_cookie_are_blocked(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "1")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_GUEST", "0")

    guest_response = asyncio.run(
        owner_access_policy(_request("/guest", method="POST"), _allow)
    )
    assert guest_response.status_code == 403

    stale_guest = asyncio.run(
        owner_access_policy(
            _request("/", cookie="sports_edge_guest=1"),
            _allow,
        )
    )
    assert stale_guest.status_code == 303
    assert stale_guest.headers["location"] == "/login"
    assert "sports_edge_guest=" in stale_guest.headers["set-cookie"]


def test_health_remains_public_when_owner_policy_is_locked(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "1")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_SIGNUP", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_GUEST", "0")

    response = asyncio.run(owner_access_policy(_request("/health"), _allow))
    assert response.status_code == 200
    assert response.body == b"ok"


def test_production_supervisor_serves_owner_access_wrapper(monkeypatch):
    monkeypatch.setenv("PORT", "9123")
    specs = build_child_specs()

    assert specs[0].name == "web"
    assert "sports_ai.dashboard.owner_access:app" in specs[0].argv
    assert specs[1].name == "scheduler"
