"""Production access-policy wrapper for owner-only Football deployments.

The core dashboard authentication remains in ``dashboard.app``. This wrapper adds
small environment-controlled policy gates so production can disable public
signup and guest access without coupling access control to Football model or
betting state.
"""

from __future__ import annotations

import os

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from .app import GUEST_COOKIE_NAME, GUEST_COOKIE_VALUE, app
from .auth import SESSION_COOKIE_NAME

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
    """Apply deployment policy outside the existing authentication middleware.

    Defaults preserve the pre-existing signup + guest behavior. Owner-only
    production can explicitly disable either surface through environment flags.
    """
    auth_enabled = _flag("SPORTS_AI_AUTH_ENABLED", default=False)
    if not auth_enabled:
        return await call_next(request)

    allow_signup = signup_allowed()
    allow_guest = guest_allowed()
    path = request.url.path

    if path == "/signup" and not allow_signup:
        if request.method in {"GET", "HEAD"}:
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Account creation is disabled"}, status_code=403)

    if path == "/guest" and not allow_guest:
        return JSONResponse({"detail": "Guest access is disabled"}, status_code=403)

    # Retire any previously-issued guest cookie once guest access is disabled.
    # Redirecting before the inner auth middleware prevents the stale cookie
    # from granting one final read-only request.
    if (
        not allow_guest
        and request.cookies.get(GUEST_COOKIE_NAME) == GUEST_COOKIE_VALUE
        and path not in {"/login", "/health"}
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
