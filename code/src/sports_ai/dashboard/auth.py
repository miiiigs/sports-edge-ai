"""Minimal email/password authentication for the Sports Edge dashboard."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SESSION_COOKIE_NAME = "sports_edge_session"
PASSWORD_ITERATIONS = 310_000
SESSION_DAYS = 30
ALLOWED_EMAILS_ENV_VAR = "SPORTS_AI_AUTH_ALLOWED_EMAILS"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _allowed_signup_emails() -> frozenset[str]:
    raw = os.getenv(ALLOWED_EMAILS_ENV_VAR, "")
    return frozenset(
        normalized
        for normalized in (_normalize_email(value) for value in raw.split(","))
        if normalized
    )


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
        dklen=32,
    )


def _hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthSettings:
    enabled: bool
    db_path: Path
    cookie_secure: bool

    @classmethod
    def from_env(cls) -> "AuthSettings":
        data_dir = Path(os.getenv("SPORTS_AI_DATA_DIR", "data"))
        db_path = Path(os.getenv("SPORTS_AI_AUTH_DB_PATH", str(data_dir / "sports_edge_auth.sqlite")))
        enabled = os.getenv("SPORTS_AI_AUTH_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
        cookie_secure = os.getenv("SPORTS_AI_AUTH_COOKIE_SECURE", "1").strip().lower() not in {"0", "false", "no", "off"}
        return cls(enabled=enabled, db_path=db_path, cookie_secure=cookie_secure)


class AuthStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
                """
            )
            conn.commit()

    def create_user(self, email: str, password: str) -> dict[str, Any]:
        normalized = _normalize_email(email)
        if not normalized or "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
            raise ValueError("Enter a valid email address.")
        allowed_emails = _allowed_signup_emails()
        if allowed_emails and normalized not in allowed_emails:
            raise ValueError("This email is not authorized to create a Sports Edge account.")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters.")

        salt = secrets.token_bytes(16)
        password_hash = _hash_password(password, salt)
        created_at = _iso(_utcnow())
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "INSERT INTO users (email, password_salt, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (normalized, salt, password_hash, created_at),
                )
                conn.commit()
                user_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ValueError("An account with that email already exists.") from exc
        return {"id": user_id, "email": normalized, "created_at": created_at}

    def authenticate(self, email: str, password: str) -> dict[str, Any] | None:
        normalized = _normalize_email(email)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, email, password_salt, password_hash, created_at FROM users WHERE email = ?",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        expected = bytes(row[3])
        supplied = _hash_password(password, bytes(row[2]))
        if not hmac.compare_digest(expected, supplied):
            return None
        return {"id": int(row[0]), "email": str(row[1]), "created_at": str(row[4])}

    def create_session(self, user_id: int) -> str:
        raw_token = secrets.token_urlsafe(32)
        token_hash = _hash_session_token(raw_token)
        now = _utcnow()
        expires_at = now + timedelta(days=SESSION_DAYS)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_iso(now),))
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, int(user_id), _iso(now), _iso(expires_at)),
            )
            conn.commit()
        return raw_token

    def get_user_for_session(self, raw_token: str | None) -> dict[str, Any] | None:
        if not raw_token:
            return None
        token_hash = _hash_session_token(raw_token)
        now = _utcnow()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT u.id, u.email, u.created_at, s.expires_at
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if _parse_iso(str(row[3])) <= now:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                conn.commit()
                return None
        return {"id": int(row[0]), "email": str(row[1]), "created_at": str(row[2])}

    def delete_session(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_session_token(raw_token),))
            conn.commit()
