"""API-Football adapter for pre-match lineup and injury context.

This adapter deliberately stays separate from Route B prediction code. It only
normalizes point-in-time context evidence that the kill-only veto policy can
consume. Every paid request can share the existing ProviderRequestBudget.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from .models import utc_now_iso
from .provider_budget import (
    ACTION_CACHE_HIT,
    PRIORITY_ESSENTIAL,
    PRIORITY_NORMAL,
    PROVIDER_API_FOOTBALL,
    ProviderRequestBudget,
    classify_provider_response,
)

API_FOOTBALL_LINEUPS_ENDPOINT = "https://v3.football.api-sports.io/fixtures/lineups"
API_FOOTBALL_INJURIES_ENDPOINT = "https://v3.football.api-sports.io/injuries"
API_FOOTBALL_CONTEXT_VERSION = "api_football_context_v1"

LINEUPS_TTL_SECONDS = 120
INJURIES_TTL_SECONDS = 4 * 60 * 60


class FootballContextProviderError(RuntimeError):
    """Raised when external context retrieval fails."""


@dataclass(frozen=True)
class FootballContextProviderConfig:
    token_env_var: str = "SPORTS_AI_API_FOOTBALL_TOKEN"
    timeout_seconds: float = 20.0


class ApiFootballContextProvider:
    """Normalize API-Football lineup/injury evidence for the veto layer."""

    def __init__(self, config: FootballContextProviderConfig | None = None) -> None:
        self._config = FootballContextProviderConfig() if config is None else config
        self._budget: ProviderRequestBudget | None = None

    def attach_request_budget(self, budget: ProviderRequestBudget) -> None:
        self._budget = budget

    def configured(self) -> bool:
        return bool(os.environ.get(self._config.token_env_var, "").strip())

    def provider_name(self) -> str:
        return PROVIDER_API_FOOTBALL

    def _token(self) -> str:
        token = os.environ.get(self._config.token_env_var, "").strip()
        if not token:
            raise FootballContextProviderError(
                f"missing required environment variable {self._config.token_env_var}"
            )
        return token

    @staticmethod
    def _endpoint_name(endpoint: str) -> str:
        if endpoint == API_FOOTBALL_LINEUPS_ENDPOINT:
            return "fixtures/lineups"
        if endpoint == API_FOOTBALL_INJURIES_ENDPOINT:
            return "injuries"
        return endpoint

    def _request_json(
        self,
        *,
        endpoint: str,
        params: dict[str, Any],
        ttl_seconds: int,
        priority: str,
    ) -> tuple[dict[str, Any], str]:
        endpoint_name = self._endpoint_name(endpoint)
        if self._budget is not None:
            decision = self._budget.issue(
                provider=PROVIDER_API_FOOTBALL,
                endpoint=endpoint_name,
                params=params,
                ttl_seconds=ttl_seconds,
                priority=priority,
            )
            if decision.action == ACTION_CACHE_HIT and decision.cached_payload is not None:
                return decision.cached_payload, "CACHE_HIT"
            if not decision.allow:
                return {
                    "response": [],
                    "results": 0,
                    "errors": {endpoint_name: decision.reason or "request blocked"},
                }, decision.action

        payload: dict[str, Any] | None = None
        try:
            with httpx.Client(
                timeout=self._config.timeout_seconds,
                follow_redirects=True,
                headers={"x-apisports-key": self._token()},
            ) as client:
                response = client.get(endpoint, params=params)
                try:
                    payload = response.json()
                except ValueError:
                    payload = {
                        "response": [],
                        "results": 0,
                        "errors": {endpoint_name: response.text[:200]},
                    }
                failure_state, failure_message, _ = classify_provider_response(
                    http_status=int(response.status_code),
                    payload=payload,
                    response_headers=response.headers,
                )
                if response.is_error or failure_state is not None:
                    if self._budget is not None:
                        self._budget.finish_failure(
                            provider=PROVIDER_API_FOOTBALL,
                            endpoint=endpoint_name,
                            params=params,
                            state=failure_state,
                            message=failure_message or f"HTTP {response.status_code}",
                            response_headers=response.headers,
                        )
                    raise FootballContextProviderError(
                        f"API-Football context request failed: {failure_message or f'HTTP {response.status_code}'}"
                    )
                if self._budget is not None:
                    self._budget.finish_success(
                        provider=PROVIDER_API_FOOTBALL,
                        endpoint=endpoint_name,
                        params=params,
                        payload=payload or {},
                        provider_timestamp=None,
                        ttl_seconds=ttl_seconds,
                        response_headers=response.headers,
                    )
        except httpx.HTTPError as exc:
            if self._budget is not None:
                self._budget.finish_failure(
                    provider=PROVIDER_API_FOOTBALL,
                    endpoint=endpoint_name,
                    params=params,
                    state=None,
                    message=str(exc),
                )
            raise FootballContextProviderError(f"API-Football context request failed: {exc}") from exc

        return payload or {}, "OK"

    @staticmethod
    def _player_payload(item: dict[str, Any]) -> dict[str, Any]:
        player = item.get("player") or item
        return {
            "player_id": player.get("id"),
            "name": str(player.get("name") or "").strip() or None,
            "number": player.get("number"),
            "position": player.get("pos") or player.get("position"),
            "grid": player.get("grid"),
        }

    @classmethod
    def _normalize_lineups(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            team = row.get("team") or {}
            coach = row.get("coach") or {}
            normalized.append(
                {
                    "team_id": team.get("id"),
                    "team_name": str(team.get("name") or "").strip() or None,
                    "formation": row.get("formation"),
                    "coach_id": coach.get("id"),
                    "coach_name": str(coach.get("name") or "").strip() or None,
                    "start_xi": [cls._player_payload(item) for item in (row.get("startXI") or [])],
                    "substitutes": [cls._player_payload(item) for item in (row.get("substitutes") or [])],
                }
            )
        return normalized

    @staticmethod
    def _normalize_injuries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for row in rows:
            player = row.get("player") or {}
            team = row.get("team") or {}
            fixture = row.get("fixture") or {}
            normalized.append(
                {
                    "fixture_id": fixture.get("id"),
                    "team_id": team.get("id"),
                    "team_name": str(team.get("name") or "").strip() or None,
                    "player_id": player.get("id"),
                    "player_name": str(player.get("name") or "").strip() or None,
                    "type": player.get("type"),
                    "reason": player.get("reason"),
                }
            )
        return normalized

    def get_lineups(self, provider_fixture_id: str) -> dict[str, Any]:
        captured_at = utc_now_iso()
        payload, request_state = self._request_json(
            endpoint=API_FOOTBALL_LINEUPS_ENDPOINT,
            params={"fixture": str(provider_fixture_id)},
            ttl_seconds=LINEUPS_TTL_SECONDS,
            priority=PRIORITY_ESSENTIAL,
        )
        rows = payload.get("response") or []
        if not isinstance(rows, list):
            raise FootballContextProviderError("unexpected API-Football lineups response payload")
        lineups = self._normalize_lineups(rows)
        return {
            "provider": PROVIDER_API_FOOTBALL,
            "version": API_FOOTBALL_CONTEXT_VERSION,
            "provider_fixture_id": str(provider_fixture_id),
            "captured_at": captured_at,
            "request_state": request_state,
            "evidence_state": "AVAILABLE" if lineups else "MISSING",
            "lineups": lineups,
        }

    def get_injuries(self, provider_fixture_id: str) -> dict[str, Any]:
        captured_at = utc_now_iso()
        payload, request_state = self._request_json(
            endpoint=API_FOOTBALL_INJURIES_ENDPOINT,
            params={"fixture": str(provider_fixture_id)},
            ttl_seconds=INJURIES_TTL_SECONDS,
            priority=PRIORITY_NORMAL,
        )
        rows = payload.get("response") or []
        if not isinstance(rows, list):
            raise FootballContextProviderError("unexpected API-Football injuries response payload")
        injuries = self._normalize_injuries(rows)
        return {
            "provider": PROVIDER_API_FOOTBALL,
            "version": API_FOOTBALL_CONTEXT_VERSION,
            "provider_fixture_id": str(provider_fixture_id),
            "captured_at": captured_at,
            "request_state": request_state,
            "evidence_state": "AVAILABLE" if injuries else "EMPTY",
            "injuries": injuries,
        }

    def get_fixture_context(self, provider_fixture_id: str) -> dict[str, Any]:
        """Fetch the two provider evidence sets without deriving predictive signals."""
        lineups = self.get_lineups(provider_fixture_id)
        injuries = self.get_injuries(provider_fixture_id)
        return {
            "provider": PROVIDER_API_FOOTBALL,
            "version": API_FOOTBALL_CONTEXT_VERSION,
            "provider_fixture_id": str(provider_fixture_id),
            "captured_at": max(str(lineups["captured_at"]), str(injuries["captured_at"])),
            "lineups": lineups,
            "injuries": injuries,
        }
