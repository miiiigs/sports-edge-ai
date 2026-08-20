"""Shared models for the operational football dashboard."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

FIXTURE_STATUS_SCHEDULED = "SCHEDULED"
FIXTURE_STATUS_PRE_MATCH = "PRE_MATCH"
FIXTURE_STATUS_LIVE = "LIVE"
FIXTURE_STATUS_HALFTIME = "HALFTIME"
FIXTURE_STATUS_FINISHED = "FINISHED"
FIXTURE_STATUS_POSTPONED = "POSTPONED"
FIXTURE_STATUS_CANCELLED = "CANCELLED"

FIXTURE_PREMATCH_STATUSES = {
    FIXTURE_STATUS_SCHEDULED,
    FIXTURE_STATUS_PRE_MATCH,
}
FIXTURE_LIVE_STATUSES = {
    FIXTURE_STATUS_LIVE,
    FIXTURE_STATUS_HALFTIME,
}
FIXTURE_TERMINAL_STATUSES = {
    FIXTURE_STATUS_FINISHED,
    FIXTURE_STATUS_POSTPONED,
    FIXTURE_STATUS_CANCELLED,
}

PREDICTION_STATUS_PENDING = "PENDING"
PREDICTION_STATUS_RUNNING = "RUNNING"
PREDICTION_STATUS_READY = "READY"
PREDICTION_STATUS_BLOCKED = "BLOCKED"
PREDICTION_STATUS_FAILED = "FAILED"
PREDICTION_STATUS_STALE = "STALE"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def slugify(text: str) -> str:
    normalized = normalize_text_key(text)
    return normalized.replace(" ", "_")


def normalize_text_key(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    lowered = folded.lower()
    lowered = lowered.replace("&", " and ")
    lowered = lowered.replace("/", " ")
    lowered = lowered.replace("-", " ")
    lowered = lowered.replace(".", " ")
    lowered = lowered.replace("'", "")
    lowered = re.sub(r"[^a-z0-9 ]+", " ", lowered)
    token_map = {
        "utd": "united",
    }
    removable_tokens = {
        "fc",
        "afc",
        "cf",
        "cd",
        "ud",
        "ac",
        "as",
        "ssc",
        "club",
        "the",
    }
    normalized_tokens = []
    for token in lowered.split():
        mapped = token_map.get(token, token)
        if mapped in removable_tokens:
            continue
        normalized_tokens.append(mapped)
    lowered = " ".join(normalized_tokens)
    lowered = re.sub(r"\bhotspur\b", "", lowered)
    lowered = re.sub(r"\bwanderers\b", "", lowered)
    lowered = re.sub(r"\balbion\b", "", lowered)
    lowered = re.sub(r"\bathletic\b", "ath", lowered)
    lowered = re.sub(r"\batletico\b", "ath", lowered)
    lowered = re.sub(r"\brayo\b", "", lowered)
    lowered = re.sub(r"\breal sociedad\b", "sociedad", lowered)
    lowered = re.sub(r"\breal betis\b", "betis", lowered)
    lowered = re.sub(r"\bdeportivo alaves\b", "alaves", lowered)
    lowered = re.sub(r"\bcelta vigo\b", "celta", lowered)
    lowered = re.sub(r"\brcd espanyol\b", "espanyol", lowered)
    lowered = re.sub(r"\bwolverhampton\b", "wolves", lowered)
    lowered = re.sub(r"\btottenham hotspur\b", "tottenham", lowered)
    lowered = re.sub(r"\bmanchester united\b", "man united", lowered)
    lowered = re.sub(r"\bmanchester city\b", "man city", lowered)
    lowered = re.sub(r"\bnewcastle united\b", "newcastle", lowered)
    lowered = re.sub(r"\bwest bromwich\b", "west brom", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def build_fixture_id(
    *,
    provider: str,
    provider_fixture_id: str | None,
    kickoff_utc: str,
    home_team_name: str,
    away_team_name: str,
) -> str:
    if provider_fixture_id:
        return f"{provider}:{provider_fixture_id}"
    digest = sha256(
        "|".join(
            [
                provider,
                kickoff_utc,
                normalize_text_key(home_team_name),
                normalize_text_key(away_team_name),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"{provider}:generated:{digest}"


@dataclass(frozen=True)
class NormalizedFixture:
    fixture_id: str
    provider_fixture_id: str | None
    provider: str
    competition_id: str
    competition_name: str
    division_code: str | None
    season: str
    home_team_id: str | None
    home_team_name: str
    away_team_id: str | None
    away_team_name: str
    kickoff_utc: str
    status: str
    elapsed_minute: int | None
    home_score: int | None
    away_score: int | None
    last_provider_update: str
    source_version: str
    provider_payload: dict[str, Any]

    def kickoff_local(self, timezone_name: str) -> str:
        local_dt = parse_iso_datetime(self.kickoff_utc).astimezone(ZoneInfo(timezone_name))
        return local_dt.strftime("%Y-%m-%d %H:%M")

    def to_dict(
        self,
        *,
        timezone_name: str,
        prediction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = asdict(self)
        payload["kickoff_local"] = self.kickoff_local(timezone_name)
        payload["prediction"] = prediction
        return payload


@dataclass(frozen=True)
class TeamResolution:
    provider: str
    competition_id: str
    provider_team_id: str | None
    provider_team_name: str
    normalized_team_name: str
    canonical_team_id: str | None
    canonical_team_name: str | None
    resolution_kind: str
    confidence: float
    reason: str | None = None
    diagnostic_category: str | None = None
    suggested_canonical_name: str | None = None
    canonical_candidates: tuple[str, ...] = ()
    model_history_available: bool | None = None
    model_history_reason: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.canonical_team_id is not None and self.canonical_team_name is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
