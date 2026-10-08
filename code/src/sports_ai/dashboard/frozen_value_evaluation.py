"""Prospective Value Bet evaluation from FROZEN LIVE_ISSUED Model Picks.

The predictive input is a single frozen Model Pick (side + line + model
probability + model fair price). Market evidence only ever supplies prices for
that exact frozen line/selection; it can never change the side, the line, or the
model probability. Settlement-aware edge, thresholds and minimum acceptable price
reuse the existing Day-11 Manager arithmetic.

DB-only: no provider requests. This module performs no writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..experiments.football_day11_manager_core import (
    DECISION_MISSING_REQUIRED_MARKET_DATA,
    DECISION_NO_BET,
    DECISION_PRELIMINARY_CANDIDATE,
    DECISION_REJECTED_RULE,
    DECISION_STALE_MARKET,
    apply_day11_manager_rules,
    build_reference_market_booksum,
    collapse_latest_bookmaker_quotes,
    normalize_market_snapshot,
    validate_reference_market_rows,
)
from ..market_data.market_evidence import resolve_actionable_max_age_minutes

POLICY_ID = "football_model_pick_balanced_line_v1"
VALUE_PIPELINE_VERSION = "football_frozen_model_pick_value_v1"

VALUE_SCHEDULER_ENV_VAR = "SPORTS_AI_ENABLE_VALUE_BET_EVALUATION_SCHEDULER"
VALUE_INTERVAL_ENV_VAR = "SPORTS_AI_VALUE_BET_EVALUATION_INTERVAL_SECONDS"
DEFAULT_VALUE_INTERVAL_SECONDS = 600
MIN_VALUE_INTERVAL_SECONDS = 300
HARD_MAX_VALUE_INTERVAL_SECONDS = 86_400


def resolve_value_evaluation_interval_seconds(configured: int | None) -> int:
    if configured is None:
        return DEFAULT_VALUE_INTERVAL_SECONDS
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_VALUE_INTERVAL_SECONDS
    if value <= 0:
        return DEFAULT_VALUE_INTERVAL_SECONDS
    if value < MIN_VALUE_INTERVAL_SECONDS:
        return MIN_VALUE_INTERVAL_SECONDS
    return min(value, HARD_MAX_VALUE_INTERVAL_SECONDS)

MARKET_ASIAN_HANDICAP = "asian_handicap"
MARKET_CORNER_TOTAL = "corner_total"
SUPPORTED_VALUE_MARKETS = (MARKET_ASIAN_HANDICAP, MARKET_CORNER_TOTAL)

# Decision states.
VALUE_BET = "VALUE_BET"
NO_BET = "NO_BET"
NO_MARKET_EVIDENCE = "NO_MARKET_EVIDENCE"
MARKET_STALE = "MARKET_STALE"
MARKET_IDENTITY_UNRESOLVED = "MARKET_IDENTITY_UNRESOLVED"
NO_COMPARABLE_MARKET_LINE = "NO_COMPARABLE_MARKET_LINE"
REFERENCE_MARKET_INCOMPLETE = "REFERENCE_MARKET_INCOMPLETE"
MODEL_PICK_UNAVAILABLE = "MODEL_PICK_UNAVAILABLE"
SAFEGUARD_BLOCKED = "SAFEGUARD_BLOCKED"
FIXTURE_ALREADY_STARTED = "FIXTURE_ALREADY_STARTED"
EXISTING_POSITION = "EXISTING_POSITION"
EXISTING_POSITION_LINEAGE_UNRESOLVED = "EXISTING_POSITION_LINEAGE_UNRESOLVED"

_DEFAULT_REFERENCE_BOOKMAKERS = ("pinnacle", "betfair", "bet365")


def _parse(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bookmaker_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


def reference_bookmakers() -> tuple[str, ...]:
    import os

    raw = os.environ.get("SPORTS_AI_MARKET_REFERENCE_BOOKMAKERS", "").strip()
    if not raw:
        return _DEFAULT_REFERENCE_BOOKMAKERS
    return tuple(_bookmaker_key(item) for item in raw.split(",") if item.strip())


def derive_effective_loss_probability(*, model_probability: float, model_fair_price: float) -> float:
    """Reconstruct effective loss stake probability from the frozen Day-10 pair.

    Day-10 invariant: ``fair_decimal_price = 1 + effective_loss / effective_win``
    therefore ``effective_loss = effective_win * (fair_decimal_price - 1)``. This
    is used (never ``1 - W``) because quarter lines include push/half outcomes.
    """
    return float(model_probability) * (float(model_fair_price) - 1.0)


@dataclass(frozen=True)
class EvaluationResult:
    model_pick_id: str
    fixture_id: str
    market: str
    selection: str
    line: float | None
    state: str
    reason: str | None = None
    model_probability: float | None = None
    model_fair_price: float | None = None
    effective_win: float | None = None
    effective_loss: float | None = None
    reference_bookmaker: str | None = None
    best_bookmaker: str | None = None
    best_price: float | None = None
    edge: float | None = None
    threshold: float | None = None
    minimum_acceptable_price: float | None = None
    market_source_timestamp: str | None = None
    market_age_minutes: float | None = None
    safeguard_state: str | None = None
    entry_snapshot_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_pick_id": self.model_pick_id,
            "fixture_id": self.fixture_id,
            "market": self.market,
            "selection": self.selection,
            "line": self.line,
            "state": self.state,
            "reason": self.reason,
            "model_probability": self.model_probability,
            "model_fair_price": self.model_fair_price,
            "effective_win": self.effective_win,
            "effective_loss": self.effective_loss,
            "reference_bookmaker": self.reference_bookmaker,
            "best_bookmaker": self.best_bookmaker,
            "best_price": self.best_price,
            "edge": self.edge,
            "threshold": self.threshold,
            "minimum_acceptable_price": self.minimum_acceptable_price,
            "market_source_timestamp": self.market_source_timestamp,
            "market_age_minutes": self.market_age_minutes,
            "safeguard_state": self.safeguard_state,
            "entry_snapshot_id": self.entry_snapshot_id,
        }


@dataclass
class ValuePlan:
    evaluated: list[EvaluationResult] = field(default_factory=list)

    def by_state(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.evaluated:
            counts[row.state] = counts.get(row.state, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_version": VALUE_PIPELINE_VERSION,
            "counts": self.by_state(),
            "eligible": [row.to_dict() for row in self.evaluated if row.state == VALUE_BET],
            "evaluated": [row.to_dict() for row in self.evaluated],
            "action_hash": value_action_hash(self.evaluated),
        }


def value_action_hash(rows: Sequence[EvaluationResult]) -> str:
    from ..locking import canonical_hash

    actions = [
        f"{row.model_pick_id}|{row.fixture_id}|{row.market}|{row.selection}|{row.line}|"
        f"{row.entry_snapshot_id}|{row.best_price}"
        for row in rows
        if row.state == VALUE_BET
    ]
    return canonical_hash({"value_actions": sorted(actions)})


def _day11_row_from_snapshot(row: Mapping[str, Any], *, fixture: Mapping[str, Any]) -> dict[str, Any]:
    bookmaker = str(row.get("bookmaker") or "")
    references = reference_bookmakers()
    key = _bookmaker_key(bookmaker)
    rank = references.index(key) if key in references else None
    return normalize_market_snapshot(
        {
            "fixture_id": str(row["fixture_id"]),
            "event_id": str(row["fixture_id"]),
            "competition_id": str(fixture.get("competition_id") or ""),
            "competition_name": str(fixture.get("competition_name") or ""),
            "season_code": str(fixture.get("season") or ""),
            "division_code": str(fixture.get("division_code") or ""),
            "home_team": str(fixture.get("home_team_name") or ""),
            "away_team": str(fixture.get("away_team_name") or ""),
            "kickoff_at": str(fixture.get("kickoff_utc") or ""),
            "captured_at": str(row.get("captured_at") or ""),
            "market_as_of": str(row.get("provider_timestamp") or row.get("captured_at") or ""),
            "source": str(row.get("provider") or ""),
            "bookmaker": bookmaker,
            "reference_designation": "reference" if rank is not None else "available",
            "reference_rank": rank,
            "market_family": str(row.get("market") or ""),
            "line": row.get("line"),
            "selection": str(row.get("selection") or ""),
            "decimal_odds": float(row.get("odds")),
            "provider_market_id": str(row.get("provider_market_id") or f"{row.get('market')}:{row.get('selection')}:{row.get('line')}"),
            "freshness_state": str(row.get("freshness_state") or "UNKNOWN"),
            "input_label": "FROZEN_MODEL_PICK_VALUE",
            "source_snapshot_version_id": str(row.get("snapshot_id") or ""),
        }
    )


def _reference_market(rows: Sequence[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    reference_rows = [row for row in rows if row.get("reference_designation") == "reference"]
    if not reference_rows:
        return None, REFERENCE_MARKET_INCOMPLETE
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in reference_rows:
        rank = row.get("reference_rank")
        grouped.setdefault((999 if rank is None else int(rank), str(row["bookmaker"])), []).append(row)
    for group_key in sorted(grouped):
        candidate_rows = grouped[group_key]
        validation = validate_reference_market_rows(candidate_rows)
        if validation["is_complete"]:
            return build_reference_market_booksum(candidate_rows, final_method="shin"), None
    return None, REFERENCE_MARKET_INCOMPLETE


def evaluate_frozen_pick(
    *,
    pick: Mapping[str, Any],
    fixture: Mapping[str, Any] | None,
    market_rows: Sequence[Mapping[str, Any]],
    now_utc: datetime,
    safeguard_state: str | None = None,
    existing_position: bool = False,
    existing_position_lineage_ok: bool = True,
) -> EvaluationResult:
    """Evaluate one frozen LIVE_ISSUED Model Pick against exact market evidence."""
    model_pick_id = str(pick.get("model_pick_id"))
    fixture_id = str(pick.get("fixture_id"))
    market = str(pick.get("market"))
    selection = str(pick.get("selection"))
    line = None if pick.get("line") is None else float(pick["line"])

    def _result(state: str, reason: str | None = None, **kw: Any) -> EvaluationResult:
        return EvaluationResult(
            model_pick_id=model_pick_id, fixture_id=fixture_id, market=market,
            selection=selection, line=line, state=state, reason=reason, **kw,
        )

    if market not in SUPPORTED_VALUE_MARKETS:
        return _result(MODEL_PICK_UNAVAILABLE, "unsupported_market")
    if fixture is None:
        return _result(MARKET_IDENTITY_UNRESOLVED, "fixture_not_found")
    if not str(fixture.get("provider_fixture_id") or "").strip():
        return _result(MARKET_IDENTITY_UNRESOLVED, "provider_fixture_id_missing")

    kickoff = _parse(fixture.get("kickoff_utc"))
    if kickoff is None:
        return _result(MARKET_IDENTITY_UNRESOLVED, "kickoff_missing")
    if now_utc >= kickoff:
        return _result(FIXTURE_ALREADY_STARTED, "kickoff_passed")

    if existing_position:
        return _result(
            EXISTING_POSITION if existing_position_lineage_ok else EXISTING_POSITION_LINEAGE_UNRESOLVED,
            "existing_position",
        )

    probability = pick.get("model_probability")
    fair = pick.get("model_fair_price")
    if probability is None or fair is None:
        return _result(MODEL_PICK_UNAVAILABLE, "missing_frozen_probability")
    probability = float(probability)
    fair = float(fair)
    if probability <= 0.0 or fair <= 1.0:
        return _result(MODEL_PICK_UNAVAILABLE, "invalid_frozen_probability")
    effective_loss = derive_effective_loss_probability(model_probability=probability, model_fair_price=fair)

    # Exact comparable line only (no interpolation / nearest / opposite side).
    _common_frozen = {
        "model_probability": probability,
        "model_fair_price": fair,
        "effective_win": probability,
        "effective_loss": effective_loss,
    }

    def _stuck(state: str, reason: str) -> EvaluationResult:
        return _result(state, reason, **_common_frozen)

    market_only = [row for row in market_rows if str(row.get("market") or "") == market]
    if not market_only:
        return _stuck(NO_MARKET_EVIDENCE, "no_rows_for_market")
    if line is None:
        return _stuck(NO_COMPARABLE_MARKET_LINE, "frozen_line_missing")
    line_rows = [
        row for row in market_only
        if row.get("line") is not None and abs(float(row["line"]) - line) <= 1e-12
    ]
    if not line_rows:
        return _stuck(NO_COMPARABLE_MARKET_LINE, "no_exact_line")
    if not any(str(row.get("selection") or "") == selection for row in line_rows):
        return _stuck(NO_COMPARABLE_MARKET_LINE, "frozen_selection_absent_at_line")

    # Fresh pre-match only, at decision time (provider-specific actionable age).
    fresh: list[Mapping[str, Any]] = []
    stale: list[Mapping[str, Any]] = []
    max_age = resolve_actionable_max_age_minutes(str(next((r.get("provider") for r in line_rows), "api-football")))
    for row in line_rows:
        if not bool(row.get("is_pre_match", True)):
            stale.append(row)
            continue
        captured = _parse(row.get("captured_at"))
        source = _parse(row.get("provider_timestamp")) if row.get("provider_timestamp") else None
        if captured is None or captured >= kickoff or (source is not None and source >= kickoff):
            stale.append(row)
            continue
        effective_ts = source or captured
        if max_age is None or (now_utc - effective_ts).total_seconds() > max_age * 60:
            stale.append(row)
            continue
        fresh.append(row)
    if not fresh:
        if stale:
            return _stuck(MARKET_STALE, "no_actionable_fresh_quote")
        return _stuck(NO_MARKET_EVIDENCE, "no_fresh_prematch_rows")

    fresh_pick_rows = [row for row in fresh if str(row.get("selection") or "") == selection]
    references = set(reference_bookmakers())
    fresh_has_reference = any(_bookmaker_key(r.get("bookmaker")) in references for r in fresh)
    if not fresh_has_reference:
        if any(_bookmaker_key(r.get("bookmaker")) in references for r in stale):
            return _stuck(MARKET_STALE, "stale_reference_book")
        return _stuck(REFERENCE_MARKET_INCOMPLETE, "reference_book_absent")
    if not fresh_pick_rows:
        return _stuck(MARKET_STALE, "frozen_selection_not_fresh")
    if all(str(row.get("odds")) and float(row["odds"]) <= 1.0 for row in fresh_pick_rows):
        return _stuck(NO_MARKET_EVIDENCE, "invalid_odds")

    day11_rows = [_day11_row_from_snapshot(row, fixture=fixture) for row in fresh]
    collapsed = collapse_latest_bookmaker_quotes(day11_rows)
    reference_market, reference_error = _reference_market(collapsed)
    if reference_market is None:
        return _stuck(REFERENCE_MARKET_INCOMPLETE, reference_error)

    reference_bookmaker = str(reference_market["reference_bookmaker"])
    available_offer_rows = [
        row for row in collapsed
        if str(row.get("bookmaker") or "") != reference_bookmaker
        and str(row.get("normalized_selection") or row.get("selection") or "") == selection
    ]
    model_row = {
        "as_of": str(pick.get("model_as_of") or pick.get("prediction_generated_at") or fixture.get("kickoff_utc")),
        "kickoff_at": str(fixture.get("kickoff_utc")),
        "competition_id": str(fixture.get("competition_id") or ""),
        "market_family": market,
        "effective_win_stake_probability": probability,
        "effective_loss_stake_probability": effective_loss,
    }
    decision = apply_day11_manager_rules(
        model_row=model_row,
        reference_market=reference_market,
        available_offer_rows=available_offer_rows,
        decision_as_of=now_utc.isoformat(timespec="seconds"),
    )
    best = decision.get("best_available_offer")
    reference_source = reference_market.get("market_as_of")
    best_source = None if best is None else best.get("market_as_of")
    effective_ts = _parse(reference_source) or _parse(best_source)
    age = None if effective_ts is None else round((now_utc - effective_ts).total_seconds() / 60.0, 2)

    best_snapshot_id = None
    if best is not None and str(best.get("market_snapshot_id") or "").isdigit():
        best_snapshot_id = int(str(best["market_snapshot_id"]))
    common: dict[str, Any] = {
        "model_probability": probability,
        "model_fair_price": fair,
        "effective_win": probability,
        "effective_loss": effective_loss,
        "reference_bookmaker": reference_bookmaker,
        "best_bookmaker": None if best is None else str(best["bookmaker"]),
        "best_price": None if best is None else float(best["decimal_odds"]),
        "edge": decision.get("settlement_aware_edge"),
        "threshold": decision.get("competition_threshold"),
        "minimum_acceptable_price": decision.get("minimum_acceptable_price"),
        "market_source_timestamp": reference_source,
        "market_age_minutes": age,
        "safeguard_state": safeguard_state,
        "entry_snapshot_id": best_snapshot_id,
    }

    if decision["decision"] == DECISION_STALE_MARKET:
        return _result(MARKET_STALE, "manager_stale", **common)
    if decision["decision"] == DECISION_MISSING_REQUIRED_MARKET_DATA:
        return _result(NO_MARKET_EVIDENCE, "missing_available_price", **common)
    if decision["decision"] == DECISION_REJECTED_RULE:
        return _result(NO_MARKET_EVIDENCE, str((decision.get("reason_codes") or ["rejected_rule"])[0]), **common)
    if decision["decision"] == DECISION_NO_BET:
        return _result(NO_BET, str((decision.get("reason_codes") or ["no_bet"])[0]), **common)
    if decision["decision"] == DECISION_PRELIMINARY_CANDIDATE:
        if safeguard_state in {"BLOCKED", "SAFEGUARD_BLOCKED"}:
            return _result(SAFEGUARD_BLOCKED, "safeguard_blocked", **common)
        return _result(VALUE_BET, None, **common)
    return _result(NO_MARKET_EVIDENCE, "unexpected_decision", **common)
