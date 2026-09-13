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
from sports_ai.dashboard.owner_access import guest_allowed, owner_access_policy, signup_allowed
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


def test_auth_store_optional_owner_email_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOWED_EMAILS", " Owner@Example.com , second@example.com ")
    store = AuthStore(tmp_path / "auth.sqlite")
    store.initialize()

    owner = store.create_user("owner@example.com", "strong-pass-123")
    assert owner["email"] == "owner@example.com"
    with pytest.raises(ValueError, match="not authorized"):
        store.create_user("other@example.com", "strong-pass-123")


def test_owner_policy_can_disable_signup_and_guest(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "1")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_SIGNUP", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_GUEST", "0")

    signup_get = asyncio.run(owner_access_policy(_request("/signup"), _allow))
    signup_post = asyncio.run(owner_access_policy(_request("/signup", method="POST"), _allow))
    guest_post = asyncio.run(owner_access_policy(_request("/guest", method="POST"), _allow))

    assert signup_get.status_code == 303
    assert signup_get.headers["location"] == "/login"
    assert signup_post.status_code == 403
    assert json.loads(signup_post.body)["detail"] == "Account creation is disabled"
    assert guest_post.status_code == 403


def test_stale_guest_cookie_is_retired_before_protected_read(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "1")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_GUEST", "0")

    response = asyncio.run(
        owner_access_policy(
            _request("/", cookie="sports_edge_guest=1"),
            _allow,
        )
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert "sports_edge_guest=" in response.headers["set-cookie"]


def test_locked_login_ui_hides_signup_and_guest(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_SIGNUP", "0")
    monkeypatch.setenv("SPORTS_AI_AUTH_ALLOW_GUEST", "0")
    request = _request("/login")
    request.state.owner_signup_allowed = signup_allowed()
    request.state.owner_guest_allowed = guest_allowed()
    templates = Jinja2Templates(directory=str(_template_directory()))

    html = templates.get_template("auth.html").render(
        request=request,
        mode="login",
        next_path="/",
        error=None,
        email="",
    )

    assert "Continue as guest" not in html
    assert ">Sign up<" not in html
    assert "Create an account" not in html
    assert ">Sign in<" in html


def test_supervisor_keeps_low_memory_web_through_owner_wrapper(monkeypatch):
    monkeypatch.setenv("PORT", "9123")
    specs = build_child_specs()

    assert specs[0].name == "web"
    assert "sports_ai.dashboard.owner_access:app" in specs[0].argv
    assert specs[1].name == "scheduler"
    # owner_access imports its app from low_memory_app, preserving web-process isolation.
    assert owner_access.app.state.services.__class__.__name__ == "LowMemoryWebDashboardServices"
