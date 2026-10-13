"""Read-only server-to-server API token for the unified Sports Edge client.

The unified Tennis host (``sports-edge-tennis``) reads Football data over
server-side GET requests. When ``SPORTS_EDGE_CLIENT_API_TOKEN`` is configured,
a caller presenting ``Authorization: Bearer <token>`` may read a small explicit
GET/HEAD allowlist of Football API paths.

The token is deliberately least-privilege:

* it is only accepted on ``GET`` or ``HEAD`` requests,
* it is only accepted on the exact read-only allowlist below,
* it never authorises mutating/admin/refresh/login routes,
* it never authorises predictions, analysis or fixture transport routes,
* it never exposes HTML dashboard pages, and
* it is compared in constant time and never logged or echoed back, and
* a configured value shorter than ``MIN_CLIENT_API_TOKEN_LENGTH`` is treated as
  unset (fail closed) so a weak placeholder can never authorise anything.

``/health`` is intentionally *not* in the allowlist: it is already public via
the existing auth middleware and must not depend on the service token.

Nothing here weakens the owner session guard: when no token is configured, or a
request falls outside the allowlist, normal cookie authentication applies.
"""

from __future__ import annotations

import hmac
import os

CLIENT_API_TOKEN_ENV_VAR = "SPORTS_EDGE_CLIENT_API_TOKEN"

# ``secrets.token_urlsafe(32)`` yields 43 characters; anything shorter than 32
# is far more likely a placeholder or typo than a generated credential.
MIN_CLIENT_API_TOKEN_LENGTH = 32

# Smallest GET/HEAD allowlist actually required by the unified pages. Even
# though the Tennis client transport layer may list more routes, the production
# machine credential must not exceed these three read-only JSON endpoints. No
# predictions, analysis or fixture transport route is reachable with the token.
CLIENT_API_READ_PATHS: frozenset[str] = frozenset(
    {
        "/api/candidates",
        "/api/performance",
        "/api/system/status",
    }
)

_READ_METHODS = frozenset({"GET", "HEAD"})

_BEARER_SCHEME = "bearer"


def configured_token() -> str:
    """Return the configured read-only token, or an empty string when unset.

    A configured value shorter than ``MIN_CLIENT_API_TOKEN_LENGTH`` is treated
    as unset so the token path fails closed.
    """
    token = (os.environ.get(CLIENT_API_TOKEN_ENV_VAR) or "").strip()
    if len(token) < MIN_CLIENT_API_TOKEN_LENGTH:
        return ""
    return token


def presented_bearer_token(authorization_header: str | None) -> str | None:
    """Parse a ``Bearer <token>`` Authorization header.

    Returns ``None`` for a missing header, a non-Bearer scheme, or an empty
    credential. The returned token is stripped of surrounding whitespace.
    """
    if not authorization_header:
        return None
    value = authorization_header.strip()
    scheme, _, credential = value.partition(" ")
    if scheme.lower() != _BEARER_SCHEME:
        return None
    token = credential.strip()
    return token or None


def is_read_only_client_request(
    *,
    method: str,
    path: str,
    authorization_header: str | None,
) -> bool:
    """Whether this exact request may proceed using the read-only token."""
    if method.upper() not in _READ_METHODS:
        return False
    if path not in CLIENT_API_READ_PATHS:
        return False
    expected = configured_token()
    if not expected:
        return False
    supplied = presented_bearer_token(authorization_header)
    if supplied is None:
        return False
    # Compare as bytes so a non-ASCII credential cannot raise from
    # hmac.compare_digest (which rejects non-ASCII str) and cannot bypass it.
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


__all__ = [
    "CLIENT_API_READ_PATHS",
    "CLIENT_API_TOKEN_ENV_VAR",
    "MIN_CLIENT_API_TOKEN_LENGTH",
    "configured_token",
    "is_read_only_client_request",
    "presented_bearer_token",
]
