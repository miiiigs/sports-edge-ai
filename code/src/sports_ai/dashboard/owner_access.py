"""Production owner-only access wrapper around the low-memory web app."""

from __future__ import annotations

import os

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from .app import GUEST_COOKIE_NAME, GUEST_COOKIE_VALUE
from .low_memory_app import app

ALLOW_SIGNUP_ENV_VAR = "SPORTS_AI_AUTH_ALLOW_SIGNUP"
ALLOW_GUEST_ENV_VAR = "SPORTS_AI_AUTH_ALLOW_GUEST"


def _flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def signup_allowed() -> bool:
    return _flag(ALLOW_SIGNUP_ENV_VAR, default=True)


def guest_allowed() -> bool:
    return _flag(ALLOW_GUEST_ENV_VAR, default=True)


@app.middleware("http")
async def owner_access_policy(request: Request, call_next):
    """Lock signup/guest surfaces without touching Football runtime logic."""
    auth_enabled = _flag("SPORTS_AI_AUTH_ENABLED", default=False)
    allow_signup = signup_allowed()
    allow_guest = guest_allowed()
    request.state.owner_signup_allowed = allow_signup
    request.state.owner_guest_allowed = allow_guest

    if not auth_enabled:
        return await call_next(request)

    path = request.url.path
    if path == "/signup" and not allow_signup:
        if request.method in {"GET", "HEAD"}:
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Account creation is disabled"}, status_code=403)
    if path == "/guest" and not allow_guest:
        return JSONResponse({"detail": "Guest access is disabled"}, status_code=403)

    if (
        not allow_guest
        and request.cookies.get(GUEST_COOKIE_NAME) == GUEST_COOKIE_VALUE
        and path != "/login"
        and path not in {"/health", "/health/wake"}
        and not path.startswith("/static/")
    ):
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(GUEST_COOKIE_NAME, path="/")
        return response

    response = await call_next(request)
    if not allow_guest and request.cookies.get(GUEST_COOKIE_NAME) == GUEST_COOKIE_VALUE:
        response.delete_cookie(GUEST_COOKIE_NAME, path="/")
    return response


__all__ = [
    "ALLOW_GUEST_ENV_VAR",
    "ALLOW_SIGNUP_ENV_VAR",
    "app",
    "guest_allowed",
    "signup_allowed",
]
