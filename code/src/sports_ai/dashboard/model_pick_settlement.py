"""Automatic Model Pick settlement from verified sporting truth (DB-only).

This module settles open ``LIVE_ISSUED`` Model Picks using ONLY already-persisted
sporting truth (``dashboard_fixtures`` + ``dashboard_fixture_results``) and the
existing proven settlement engine. It never contacts a provider, never touches
the paper/value-bet ledger, never creates Value Bets, and never modifies the
sporting-truth stores.

Settlement math is delegated to
:func:`sports_ai.storage.football_paper_trading.settlement_state_for_market`
and lifecycle writes to
:func:`sports_ai.storage.football_model_pick_ledger.update_settlement`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..locking import canonical_hash
from ..storage import football_model_pick_ledger as ledger
from ..storage.football_paper_trading import settlement_state_for_market

SETTLEMENT_SCHEDULER_ENV_VAR = "SPORTS_AI_ENABLE_MODEL_PICK_SETTLEMENT_SCHEDULER"
SETTLEMENT_INTERVAL_ENV_VAR = "SPORTS_AI_MODEL_PICK_SETTLEMENT_INTERVAL_SECONDS"

DEFAULT_SETTLEMENT_INTERVAL_SECONDS = 600
MIN_SETTLEMENT_INTERVAL_SECONDS = 300
HARD_MAX_SETTLEMENT_INTERVAL_SECONDS = 86_400

MARKET_AH = "asian_handicap"
MARKET_CORNER = "corner_total"
SUPPORTED_SETTLEMENT_MARKETS = (MARKET_AH, MARKET_CORNER)

_STATE_FINISHED = "FINISHED"

# Ineligibility / conflict reason codes.
REASON_UPCOMING = "UPCOMING"
REASON_NON_FINAL = "NON_FINAL"
REASON_FINAL_SCORE_MISSING = "FINAL_SCORE_MISSING"
REASON_CORNER_TRUTH_MISSING = "CORNER_TRUTH_MISSING"
REASON_CORNER_TRUTH_CONFLICT = "CORNER_TRUTH_CONFLICT"
REASON_IDENTITY_TRUTH_MISSING = "IDENTITY_TRUTH_MISSING"
REASON_UNSUPPORTED_MARKET = "UNSUPPORTED_MARKET"

CONFLICT_SETTLEMENT_STATE = "MODEL_PICK_SETTLEMENT_CONFLICT"
CONFLICT_SETTLEMENT_EVIDENCE = "MODEL_PICK_SETTLEMENT_EVIDENCE_CONFLICT"


def resolve_settlement_interval_seconds(configured: int | None) -> int:
    if configured is None:
        return DEFAULT_SETTLEMENT_INTERVAL_SECONDS
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_SETTLEMENT_INTERVAL_SECONDS
    if value <= 0:
        return DEFAULT_SETTLEMENT_INTERVAL_SECONDS
    if value < MIN_SETTLEMENT_INTERVAL_SECONDS:
        return MIN_SETTLEMENT_INTERVAL_SECONDS
    return min(value, HARD_MAX_SETTLEMENT_INTERVAL_SECONDS)


def _parse_kickoff(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SettlementAction:
    model_pick_id: str
    fixture_id: str
    market: str
    selection: str
    line: float | None
    trade_state: str
    settlement_payload: dict[str, Any]
    evidence_key: tuple
    result_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_pick_id": self.model_pick_id,
            "fixture_id": self.fixture_id,
            "market": self.market,
            "selection": self.selection,
            "line": self.line,
            "trade_state": self.trade_state,
            "evidence_key": list(self.evidence_key),
        }


@dataclass(frozen=True)
class SettlementPlan:
    actions: tuple[SettlementAction, ...]
    ineligible: tuple[dict[str, Any], ...]
    conflicts: tuple[dict[str, Any], ...]
    examined: int

    @property
    def eligible_ah(self) -> int:
        return sum(1 for action in self.actions if action.market == MARKET_AH)

    @property
    def eligible_corners(self) -> int:
        return sum(1 for action in self.actions if action.market == MARKET_CORNER)

    def state_counts(self) -> dict[str, int]:
        counts = {
            ledger.STATE_FULL_WIN: 0,
            ledger.STATE_HALF_WIN: 0,
            ledger.STATE_PUSH: 0,
            ledger.STATE_HALF_LOSS: 0,
            ledger.STATE_FULL_LOSS: 0,
        }
        for action in self.actions:
            counts[action.trade_state] = counts.get(action.trade_state, 0) + 1
        return counts

    def ineligible_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.ineligible:
            reason = str(row.get("reason") or "OTHER")
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    @property
    def action_hash(self) -> str:
        return settlement_action_hash(self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "eligible_ah": self.eligible_ah,
            "eligible_corners": self.eligible_corners,
            "eligible_total": len(self.actions),
            "state_counts": self.state_counts(),
            "ineligible_reason_counts": self.ineligible_reason_counts(),
            "conflict_count": len(self.conflicts),
            "action_hash": self.action_hash,
            "actions": [action.to_dict() for action in self.actions],
        }


def settlement_action_hash(actions: Sequence[SettlementAction]) -> str:
    rows = sorted(
        f"{action.model_pick_id}|{action.market}|{action.trade_state}|{json.dumps(list(action.evidence_key), sort_keys=True)}"
        for action in actions
    )
    return canonical_hash({"settlement_actions": rows})


def _fixture_status(fixture: Mapping[str, Any]) -> str:
    return str(fixture.get("status") or "").strip().upper()


def _ah_action(pick: Mapping[str, Any], fixture: Mapping[str, Any], home: int, away: int) -> SettlementAction:
    state = settlement_state_for_market(
        market=MARKET_AH,
        selection=str(pick.get("selection") or ""),
        line=pick.get("line"),
        actual_home_goals=home,
        actual_away_goals=away,
    )
    normalized = ledger.normalize_settlement_state(state)
    payload = {
        "raw_state": state,
        "result": {
            "final_status": _STATE_FINISHED,
            "home_goals": home,
            "away_goals": away,
            "result_source": fixture.get("provider"),
            "result_source_timestamp": fixture.get("last_provider_update"),
        },
    }
    return SettlementAction(
        model_pick_id=str(pick["model_pick_id"]),
        fixture_id=str(pick["fixture_id"]),
        market=MARKET_AH,
        selection=str(pick.get("selection") or ""),
        line=None if pick.get("line") is None else float(pick["line"]),
        trade_state=str(normalized),
        settlement_payload=payload,
        evidence_key=ledger.settlement_evidence_key(MARKET_AH, payload),
        result_state=state,
    )


def _corner_action(
    pick: Mapping[str, Any],
    fixture: Mapping[str, Any],
    home: int,
    away: int,
    result_row: Mapping[str, Any],
    total_corners: int,
) -> SettlementAction:
    state = settlement_state_for_market(
        market=MARKET_CORNER,
        selection=str(pick.get("selection") or ""),
        line=pick.get("line"),
        actual_home_goals=home,
        actual_away_goals=away,
        actual_total_corners=total_corners,
    )
    normalized = ledger.normalize_settlement_state(state)
    payload = {
        "raw_state": state,
        "result": {
            "final_status": _STATE_FINISHED,
            "home_goals": home,
            "away_goals": away,
            "home_corners": result_row.get("home_corners"),
            "away_corners": result_row.get("away_corners"),
            "total_corners": total_corners,
            "statistics_provider": result_row.get("statistics_provider"),
            "statistics_source_timestamp": result_row.get("source_timestamp"),
            "statistics_captured_at": result_row.get("captured_at"),
        },
    }
    return SettlementAction(
        model_pick_id=str(pick["model_pick_id"]),
        fixture_id=str(pick["fixture_id"]),
        market=MARKET_CORNER,
        selection=str(pick.get("selection") or ""),
        line=None if pick.get("line") is None else float(pick["line"]),
        trade_state=str(normalized),
        settlement_payload=payload,
        evidence_key=ledger.settlement_evidence_key(MARKET_CORNER, payload),
        result_state=state,
    )


def build_settlement_plan(
    *,
    open_picks: Sequence[Mapping[str, Any]],
    fixtures: Mapping[str, Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    now_utc: datetime,
) -> SettlementPlan:
    """Pure, read-only settlement plan over open LIVE_ISSUED picks.

    Callers must pass only ``record_origin == LIVE_ISSUED`` open picks.
    """
    actions: list[SettlementAction] = []
    ineligible: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    def _ineligible(pick: Mapping[str, Any], reason: str) -> None:
        ineligible.append(
            {
                "model_pick_id": str(pick.get("model_pick_id")),
                "fixture_id": str(pick.get("fixture_id")),
                "market": str(pick.get("market")),
                "reason": reason,
            }
        )

    for pick in open_picks:
        fixture = fixtures.get(str(pick.get("fixture_id")))
        if fixture is None:
            _ineligible(pick, REASON_IDENTITY_TRUTH_MISSING)
            continue
        status = _fixture_status(fixture)
        if status != _STATE_FINISHED:
            kickoff = _parse_kickoff(fixture.get("kickoff_utc"))
            _ineligible(pick, REASON_UPCOMING if kickoff is not None and kickoff > now_utc else REASON_NON_FINAL)
            continue
        home = _to_int(fixture.get("home_score"))
        away = _to_int(fixture.get("away_score"))
        if home is None or away is None:
            _ineligible(pick, REASON_FINAL_SCORE_MISSING)
            continue
        market = str(pick.get("market"))
        if market == MARKET_AH:
            actions.append(_ah_action(pick, fixture, home, away))
        elif market == MARKET_CORNER:
            result_row = results.get(str(pick.get("fixture_id"))) or {}
            home_corners = _to_int(result_row.get("home_corners"))
            away_corners = _to_int(result_row.get("away_corners"))
            total_corners = _to_int(result_row.get("total_corners"))
            if home_corners is None or away_corners is None or total_corners is None:
                _ineligible(pick, REASON_CORNER_TRUTH_MISSING)
                continue
            if total_corners != home_corners + away_corners:
                _ineligible(pick, REASON_CORNER_TRUTH_CONFLICT)
                conflicts.append(
                    {
                        "model_pick_id": str(pick.get("model_pick_id")),
                        "fixture_id": str(pick.get("fixture_id")),
                        "reason": REASON_CORNER_TRUTH_CONFLICT,
                        "home_corners": home_corners,
                        "away_corners": away_corners,
                        "total_corners": total_corners,
                    }
                )
                continue
            actions.append(_corner_action(pick, fixture, home, away, result_row, total_corners))
        else:
            _ineligible(pick, REASON_UNSUPPORTED_MARKET)

    return SettlementPlan(
        actions=tuple(actions),
        ineligible=tuple(ineligible),
        conflicts=tuple(conflicts),
        examined=len(open_picks),
    )


def is_open_pick(row: Mapping[str, Any]) -> bool:
    return ledger.normalize_settlement_state(row.get("trade_state")) is None
