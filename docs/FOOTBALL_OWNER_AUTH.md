# Football Owner Authentication Runbook

Status: implementation-ready, production enablement gated on an authorized owner email. This document contains no credentials.

## Purpose

Football already has email/password authentication and read-only guest access. The owner-access hardening in PR #40 adds an explicit allowlist plus deployment-policy switches so the Railway dashboard can be converted from the current permissive/default behavior to private owner-only access without touching Football model, market, portfolio, settlement, calibration, or scheduler logic.

## Persistence

Authentication data is stored separately from Football betting/model truth.

Default auth database:

```text
/data/sports_edge_auth.sqlite
```

The Football Railway service already uses the persistent `/data` volume. Do not place the auth SQLite database in an ephemeral application directory.

## Environment variables

```text
SPORTS_AI_AUTH_ENABLED
SPORTS_AI_AUTH_DB_PATH
SPORTS_AI_AUTH_COOKIE_SECURE
SPORTS_AI_AUTH_ALLOWED_EMAILS
SPORTS_AI_AUTH_ALLOW_SIGNUP
SPORTS_AI_AUTH_ALLOW_GUEST
```

Recommended Railway values after owner bootstrap is complete:

```text
SPORTS_AI_AUTH_ENABLED=1
SPORTS_AI_AUTH_DB_PATH=/data/sports_edge_auth.sqlite
SPORTS_AI_AUTH_COOKIE_SECURE=1
SPORTS_AI_AUTH_ALLOW_SIGNUP=0
SPORTS_AI_AUTH_ALLOW_GUEST=0
```

`SPORTS_AI_AUTH_ALLOWED_EMAILS` must contain only explicitly authorized account email addresses. Do not commit email addresses, passwords, session tokens, or Railway secrets to Git.

## Safe bootstrap sequence

Do not disable signup before the first authorized owner account exists.

1. Confirm the exact owner/client email that is allowed to create the account.
2. Merge/deploy the reviewed owner-access code while leaving authentication disabled, or preserve the current auth policy until the Railway deployment is healthy.
3. Verify `/health` still returns successfully.
4. Configure the persistent auth DB path and Secure-cookie setting shown above.
5. Set `SPORTS_AI_AUTH_ALLOWED_EMAILS` to the confirmed owner email.
6. Temporarily configure:

```text
SPORTS_AI_AUTH_ENABLED=1
SPORTS_AI_AUTH_ALLOW_SIGNUP=1
SPORTS_AI_AUTH_ALLOW_GUEST=0
```

7. Let the owner open `/signup` and create their own password. The password must never be sent to the developer, stored in Railway variables, or committed to Git.
8. Verify the owner can sign in, open the dashboard, and sign out successfully.
9. Lock account creation by changing:

```text
SPORTS_AI_AUTH_ALLOW_SIGNUP=0
```

10. Verify `/signup` now redirects/rejects account creation, `/guest` is unavailable, stale guest cookies no longer grant access, authenticated dashboard reads still work, and `/health` remains public.

## Important ordering rule

The lock-down sequence is intentionally two-stage:

```text
allowlisted signup -> owner account exists -> signup disabled
```

Do not deploy `ALLOW_SIGNUP=0` and `AUTH_ENABLED=1` into an empty auth database. That would create an avoidable owner lockout.

## Rollback

If owner access causes an application-access regression, preserve the auth database and disable the gate:

```text
SPORTS_AI_AUTH_ENABLED=0
```

Do not delete `/data/sports_edge_auth.sqlite` as part of rollback. Disabling authentication is sufficient to restore the pre-auth application behavior while preserving the account/session evidence for diagnosis.

If only the owner-policy wrapper is the problem and the underlying authentication is known-good, restore the previous known-good Football Git commit/branch using the normal Railway rollback path. Do not change Route B, market thresholds, portfolio rules, provider cadence, or betting data to repair an authentication issue.

## Verification checklist

After final lock-down, verify all of the following:

- unauthenticated dashboard page -> login
- unauthenticated protected API -> 401 where applicable
- authorized owner login -> dashboard access
- wrong password -> rejected
- non-allowlisted signup -> rejected
- `/signup` unavailable after bootstrap
- `/guest` unavailable
- stale guest cookie does not grant one final read
- logout invalidates the session
- `/health` remains publicly reachable
- auth database remains under `/data`
- Football scheduler/provider processes continue independently of dashboard authentication

## Current production gate

Do not merge PR #40 solely because CI is green. Football Railway tracks `main`, so merging changes production source immediately. The final prerequisite is the confirmed authorized owner email and a deliberate bootstrap window using the sequence above.
