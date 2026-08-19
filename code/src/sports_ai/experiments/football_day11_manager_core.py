"""Day 11A football manager core built on frozen Day 10 fair-price rows.

This layer remains provider-independent and historical/demo-ready:

- It consumes frozen Day 10 fair prices as the immutable model authority.
- It consumes a supplied normalized market snapshot contract.
- It validates reference-market completeness and de-vigs the reference market.
- It selects the best available supplied price for the exact normalized selection.
- It computes settlement-aware edge and minimum acceptable price.
- It applies bounded Day 11A threshold and competition rules only.

It does not implement Day 12 vetoes, staking, Kelly, live polling, or execution.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..agents.market.devig import DevigResult, compute_devig_power, compute_devig_shin
from ..locking import canonical_hash

EXPERIMENT_ID = "EXP-014"
MANAGER_VERSION = "football_day11_manager_core_v1"
MANAGER_POLICY_VERSION = "football_day11_manager_policy_v1"
NORMALIZED_MARKET_SNAPSHOT_VERSION = "football_day11_normalized_market_snapshot_v1"
NORMALIZATION_POLICY_VERSION = "football_day11_market_normalization_v1"
REFERENCE_MARKET_POLICY_VERSION = "football_day11_reference_market_priority_v1"
DECISION_RECORD_SCHEMA_VERSION = "football_day11_decision_record_v1"
MANIFEST_VERSION = "football_day11_manager_manifest_v1"
FRESHNESS_POLICY_VERSION = "football_day11_freshness_contract_v1"
THRESHOLD_CONFIG_VERSION = "football_day11_competition_thresholds_v1"
DOMESTIC_CUP_POLICY_VERSION = "football_day11_domestic_cup_rules_v1"
HISTORICAL_MARKET_INPUT_POLICY_VERSION = "football_day11_historical_demo_market_input_v1"
DEVIG_CONFIGURATION_VERSION = "football_day11_reference_devig_v1"
RUN_DATE_STAMP = "2026-08-18"
PRODUCT_AUDIT_FILENAME = "FOOTBALL_DAY11A_ACCEPTANCE_AUDIT_2026-08-19.md"

FRESHNESS_STATE_FRESH = "FRESH"
FRESHNESS_STATE_STALE = "STALE"
FRESHNESS_STATE_UNCONFIGURED = "FRESHNESS_POLICY_UNCONFIGURED"

DECISION_PRELIMINARY_CANDIDATE = "PRELIMINARY_CANDIDATE"
DECISION_NO_BET = "NO_BET"
DECISION_REJECTED_RULE = "REJECTED_RULE"
DECISION_MISSING_REQUIRED_MARKET_DATA = "MISSING_REQUIRED_MARKET_DATA"
DECISION_STALE_MARKET = "STALE_MARKET"

REASON_PASS_EDGE_THRESHOLD = "PASS_EDGE_THRESHOLD"
REASON_PASS_MINIMUM_PRICE = "PASS_MINIMUM_PRICE"
REASON_NO_BET_EDGE_BELOW_THRESHOLD = "NO_BET_EDGE_BELOW_THRESHOLD"
REASON_NO_BET_PRICE_BELOW_MINIMUM = "NO_BET_PRICE_BELOW_MINIMUM"
REASON_REJECT_CUP_MARKET_NOT_ALLOWED = "REJECT_CUP_MARKET_NOT_ALLOWED"
REASON_REJECT_TEAM_LEVEL_OUT_OF_SCOPE = "REJECT_TEAM_LEVEL_OUT_OF_SCOPE"
REASON_MISSING_TEAM_LEVEL_METADATA = "MISSING_TEAM_LEVEL_METADATA"
REASON_MISSING_REFERENCE_MARKET = "MISSING_REFERENCE_MARKET"
REASON_INCOMPLETE_REFERENCE_MARKET = "INCOMPLETE_REFERENCE_MARKET"
REASON_MISSING_AVAILABLE_PRICE = "MISSING_AVAILABLE_PRICE"
REASON_UNKNOWN_COMPETITION_THRESHOLD = "UNKNOWN_COMPETITION_THRESHOLD"
REASON_INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
REASON_STALE_MARKET = "STALE_MARKET"
REASON_INVALID_ODDS = "INVALID_ODDS"

MARKET_FAMILY_1X2 = "1x2"
MARKET_FAMILY_BTTS = "btts"
MARKET_FAMILY_GOALS_TOTAL = "goals_total"
MARKET_FAMILY_CORNER_TOTAL = "corner_total"
MARKET_FAMILY_AH = "asian_handicap"

MARKET_SELECTION_ORDER = {
    MARKET_FAMILY_1X2: ("HOME", "DRAW", "AWAY"),
    MARKET_FAMILY_BTTS: ("YES", "NO"),
    MARKET_FAMILY_GOALS_TOTAL: ("OVER", "UNDER"),
    MARKET_FAMILY_CORNER_TOTAL: ("OVER", "UNDER"),
    MARKET_FAMILY_AH: ("HOME", "AWAY"),
}

_MARKET_TYPE_TO_FAMILY = {
    "1X2": MARKET_FAMILY_1X2,
    "BTTS": MARKET_FAMILY_BTTS,
    "GOALS_TOTAL": MARKET_FAMILY_GOALS_TOTAL,
    "CORNER_TOTAL": MARKET_FAMILY_CORNER_TOTAL,
    "ASIAN_HANDICAP": MARKET_FAMILY_AH,
}

_SELECTION_NORMALIZERS = {
    MARKET_FAMILY_1X2: {
        "HOME": "HOME",
        "DRAW": "DRAW",
        "AWAY": "AWAY",
        "home": "HOME",
        "draw": "DRAW",
        "away": "AWAY",
    },
    MARKET_FAMILY_BTTS: {
        "YES": "YES",
        "NO": "NO",
        "yes": "YES",
        "no": "NO",
    },
    MARKET_FAMILY_GOALS_TOTAL: {
        "OVER": "OVER",
        "UNDER": "UNDER",
        "over": "OVER",
        "under": "UNDER",
    },
    MARKET_FAMILY_CORNER_TOTAL: {
        "OVER": "OVER",
        "UNDER": "UNDER",
        "over": "OVER",
        "under": "UNDER",
    },
    MARKET_FAMILY_AH: {
        "HOME": "HOME",
        "AWAY": "AWAY",
        "home": "HOME",
        "away": "AWAY",
    },
}

_THRESHOLD_BY_COMPETITION = {
    "ENG1": 0.05,
    "ESP1": 0.05,
    "GER1": 0.05,
    "ITA1": 0.05,
    "FRA1": 0.05,
    "UCL": 0.05,
    "UEL": 0.05,
    "ENG2": 0.06,
    "ENG3": 0.06,
    "ENG4": 0.06,
    "SCO1": 0.08,
    "ENG_FA_CUP": 0.08,
    "ENG_LEAGUE_CUP": 0.08,
    "ESP_COPA_DEL_REY": 0.08,
}

_DOMESTIC_CUP_COMPETITIONS = {
    "ENG_FA_CUP",
    "ENG_LEAGUE_CUP",
    "ESP_COPA_DEL_REY",
}

_ALLOWED_CUP_MARKETS = {
    MARKET_FAMILY_CORNER_TOTAL,
    MARKET_FAMILY_BTTS,
}

_REFERENCE_SOURCE_PRIORITY = (
    "pinnacle_closing_1x2",
    "avg_closing_1x2",
)

_AVAILABLE_SOURCES = (
    "bet365_closing_1x2",
    "max_closing_1x2",
)

_DEMO_INPUT_BANNER = "HISTORICAL / DEMONSTRATION MARKET INPUT"
_DEMO_NOT_LIVE_BANNER = "NOT LIVE BETTING OUTPUT"
_DEMO_PRELIMINARY_BANNER = "PRELIMINARY DAY 11 MANAGER DECISIONS"


@dataclass(frozen=True)
class Day11ManagerConfig:
    day10_market_prices_path: str | Path
    research_db_path: str | Path
    technical_output_dir: str | Path
    product_output_dir: str | Path
    latest_output_dir: str | Path
    final_devig_method: str = "shin"
    freshness_max_age_minutes: int | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def default_day11_manager_config() -> Day11ManagerConfig:
    repo_root = _repo_root()
    return Day11ManagerConfig(
        day10_market_prices_path=repo_root
        / "research/football_research/football_day10_market_pricing_2026-08-18_market_prices.jsonl",
        research_db_path=repo_root / "research/football_research/store/football_research.sqlite",
        technical_output_dir=repo_root / "research/football_research",
        product_output_dir=repo_root / "research/product_outputs/football/day11",
        latest_output_dir=repo_root / "research/product_outputs/football/latest",
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _kickoff_at(match_date: str, kickoff_time: str | None) -> str:
    return f"{match_date}T{kickoff_time or '00:00'}:00"


def _line_to_decimal(line: float | int | str | Decimal | None) -> Decimal | None:
    if line is None:
        return None
    try:
        decimal_line = line if isinstance(line, Decimal) else Decimal(str(line))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid line {line!r}") from exc
    quarter_units = decimal_line * Decimal("4")
    if quarter_units != quarter_units.to_integral_value():
        raise ValueError("line must be an integer, half, or quarter step")
    return decimal_line


def normalize_line(line: float | int | str | Decimal | None) -> float | None:
    decimal_line = _line_to_decimal(line)
    return None if decimal_line is None else float(decimal_line)


def normalize_market_family(raw_market_type: str) -> str:
    normalized = _MARKET_TYPE_TO_FAMILY.get(str(raw_market_type).upper())
    if normalized is None:
        raise ValueError(f"unsupported market type {raw_market_type!r}")
    return normalized


def normalize_selection(*, market_family: str, raw_selection: str) -> str:
    lookup = _SELECTION_NORMALIZERS.get(market_family)
    if lookup is None or raw_selection not in lookup:
        raise ValueError(f"unsupported selection {raw_selection!r} for {market_family}")
    return lookup[raw_selection]


def canonical_market_identity(
    *,
    fixture_id: str,
    market_family: str,
    normalized_line: float | None,
    normalized_selection: str,
) -> str:
    return canonical_hash(
        {
            "fixture_id": fixture_id,
            "market_family": market_family,
            "normalized_line": normalized_line,
            "normalized_selection": normalized_selection,
        }
    )


def normalized_market_group_key(
    *,
    fixture_id: str,
    market_family: str,
    normalized_line: float | None,
) -> str:
    return canonical_hash(
        {
            "fixture_id": fixture_id,
            "market_family": market_family,
            "normalized_line": normalized_line,
        }
    )


def _role_for_source(source: str) -> str:
    if source in _REFERENCE_SOURCE_PRIORITY:
        return "reference"
    if source in _AVAILABLE_SOURCES:
        return "available"
    return "ignored"


def normalize_market_snapshot(raw_snapshot: dict[str, Any]) -> dict[str, Any]:
    market_family = str(raw_snapshot["market_family"])
    normalized_line = normalize_line(raw_snapshot.get("line"))
    normalized_selection = normalize_selection(
        market_family=market_family,
        raw_selection=str(raw_snapshot["selection"]),
    )
    fixture_id = str(raw_snapshot["fixture_id"])
    market_group_id = normalized_market_group_key(
        fixture_id=fixture_id,
        market_family=market_family,
        normalized_line=normalized_line,
    )
    canonical_selection_id = canonical_market_identity(
        fixture_id=fixture_id,
        market_family=market_family,
        normalized_line=normalized_line,
        normalized_selection=normalized_selection,
    )
    normalized = {
        "snapshot_schema_version": NORMALIZED_MARKET_SNAPSHOT_VERSION,
        "normalization_policy_version": NORMALIZATION_POLICY_VERSION,
        "market_snapshot_id": canonical_hash(
            {
                "fixture_id": fixture_id,
                "market_family": market_family,
                "normalized_line": normalized_line,
                "normalized_selection": normalized_selection,
                "bookmaker": raw_snapshot["bookmaker"],
                "source": raw_snapshot["source"],
                "market_as_of": raw_snapshot["market_as_of"],
                "decimal_odds": raw_snapshot["decimal_odds"],
            }
        ),
        "market_group_snapshot_id": market_group_id,
        "canonical_market_identity": canonical_selection_id,
        "fixture_id": fixture_id,
        "event_id": str(raw_snapshot["event_id"]),
        "competition_id": str(raw_snapshot["competition_id"]),
        "competition_name": str(raw_snapshot["competition_name"]),
        "season_code": str(raw_snapshot["season_code"]),
        "division_code": str(raw_snapshot["division_code"]),
        "home_team": str(raw_snapshot["home_team"]),
        "away_team": str(raw_snapshot["away_team"]),
        "kickoff_at": str(raw_snapshot["kickoff_at"]),
        "captured_at": str(raw_snapshot["captured_at"]),
        "market_as_of": str(raw_snapshot["market_as_of"]),
        "source": str(raw_snapshot["source"]),
        "bookmaker": str(raw_snapshot["bookmaker"]),
        "reference_designation": str(raw_snapshot["reference_designation"]),
        "market_family": market_family,
        "normalized_line": normalized_line,
        "normalized_selection": normalized_selection,
        "decimal_odds": float(raw_snapshot["decimal_odds"]),
        "provider_market_id": str(raw_snapshot["provider_market_id"]),
        "freshness_state": str(raw_snapshot["freshness_state"]),
        "input_label": str(raw_snapshot["input_label"]),
        "source_snapshot_version_id": str(raw_snapshot["source_snapshot_version_id"]),
    }
    if market_family == MARKET_FAMILY_AH and normalized_line is None:
        raise ValueError("asian handicap snapshots require a normalized line")
    if market_family in {MARKET_FAMILY_GOALS_TOTAL, MARKET_FAMILY_CORNER_TOTAL} and normalized_line is None:
        raise ValueError("totals snapshots require a normalized line")
    return normalized


def _historical_freshness_state(
    *,
    market_as_of: str,
    decision_as_of: str,
    max_age_minutes: int | None,
) -> str:
    if max_age_minutes is None:
        return FRESHNESS_STATE_UNCONFIGURED
    market_dt = _parse_iso_datetime(market_as_of)
    decision_dt = _parse_iso_datetime(decision_as_of)
    age_minutes = (decision_dt - market_dt).total_seconds() / 60.0
    return FRESHNESS_STATE_FRESH if age_minutes <= max_age_minutes else FRESHNESS_STATE_STALE


def load_historical_market_snapshots(
    research_db_path: str | Path,
    *,
    freshness_max_age_minutes: int | None = None,
) -> list[dict[str, Any]]:
    conn = sqlite3.connect(research_db_path)
    conn.row_factory = sqlite3.Row
    query = """
        SELECT
            q.quote_id,
            q.event_id,
            q.competition_id,
            e.competition_name,
            q.season_code,
            q.division_code,
            e.match_date,
            e.kickoff_time,
            e.home_team,
            e.away_team,
            q.source,
            q.market_type,
            q.bookmaker,
            q.selection_prices_json
        FROM football_market_quotes q
        JOIN football_events e
          ON e.event_id = q.event_id
        ORDER BY e.match_date, q.event_id, q.quote_id
    """
    rows = conn.execute(query).fetchall()
    conn.close()
    snapshots: list[dict[str, Any]] = []
    for row in rows:
        market_family = normalize_market_family(str(row["market_type"]))
        if market_family != MARKET_FAMILY_1X2:
            continue
        kickoff_at = _kickoff_at(str(row["match_date"]), None if row["kickoff_time"] is None else str(row["kickoff_time"]))
        prices = json.loads(str(row["selection_prices_json"]))
        role = _role_for_source(str(row["source"]))
        if role == "ignored":
            continue
        for selection_key, price in prices.items():
            raw_snapshot = {
                "fixture_id": str(row["event_id"]),
                "event_id": str(row["event_id"]),
                "competition_id": str(row["competition_id"]),
                "competition_name": str(row["competition_name"]),
                "season_code": str(row["season_code"]),
                "division_code": str(row["division_code"]),
                "home_team": str(row["home_team"]),
                "away_team": str(row["away_team"]),
                "kickoff_at": kickoff_at,
                "captured_at": kickoff_at,
                "market_as_of": kickoff_at,
                "source": str(row["source"]),
                "bookmaker": str(row["bookmaker"]),
                "reference_designation": role,
                "market_family": market_family,
                "line": None,
                "selection": selection_key,
                "decimal_odds": float(price),
                "provider_market_id": f"quote_{row['quote_id']}",
                "freshness_state": _historical_freshness_state(
                    market_as_of=kickoff_at,
                    decision_as_of=kickoff_at,
                    max_age_minutes=freshness_max_age_minutes,
                ),
                "input_label": _DEMO_INPUT_BANNER,
                "source_snapshot_version_id": canonical_hash(
                    {
                        "quote_id": int(row["quote_id"]),
                        "event_id": str(row["event_id"]),
                        "source": str(row["source"]),
                        "bookmaker": str(row["bookmaker"]),
                        "selection_prices": prices,
                    }
                ),
            }
            snapshots.append(normalize_market_snapshot(raw_snapshot))
    return snapshots


def required_selections_for_market_family(market_family: str) -> tuple[str, ...]:
    try:
        return MARKET_SELECTION_ORDER[market_family]
    except KeyError as exc:
        raise ValueError(f"unsupported market family {market_family!r}") from exc


def validate_reference_market_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"is_complete": False, "reason": REASON_MISSING_REFERENCE_MARKET}
    market_family = str(rows[0]["market_family"])
    normalized_line = rows[0]["normalized_line"]
    expected = set(required_selections_for_market_family(market_family))
    seen: dict[str, dict[str, Any]] = {}
    invalid_odds = False
    for row in rows:
        if row["market_family"] != market_family or row["normalized_line"] != normalized_line:
            return {"is_complete": False, "reason": REASON_INCOMPLETE_REFERENCE_MARKET}
        selection = str(row["normalized_selection"])
        if selection in seen:
            return {"is_complete": False, "reason": REASON_INCOMPLETE_REFERENCE_MARKET}
        seen[selection] = row
        if float(row["decimal_odds"]) <= 1.0:
            invalid_odds = True
    reason = None
    if invalid_odds:
        reason = REASON_INVALID_ODDS
    elif set(seen) != expected:
        reason = REASON_INCOMPLETE_REFERENCE_MARKET
    return {
        "is_complete": reason is None,
        "reason": reason,
        "by_selection": seen,
    }


def build_reference_market_booksum(
    rows: Sequence[dict[str, Any]],
    *,
    final_method: str = "shin",
) -> dict[str, Any]:
    validation = validate_reference_market_rows(rows)
    if not validation["is_complete"]:
        raise ValueError(validation["reason"])
    ordered_rows = [
        validation["by_selection"][selection]
        for selection in required_selections_for_market_family(str(rows[0]["market_family"]))
    ]
    odds = [float(row["decimal_odds"]) for row in ordered_rows]
    implied_probabilities = [1.0 / odd for odd in odds]
    power = compute_devig_power(odds)
    shin = compute_devig_shin(odds)
    selected = {"power": power, "shin": shin}[final_method]
    selection_probabilities = {
        ordered_rows[index]["normalized_selection"]: selected.probabilities[index]
        for index in range(len(ordered_rows))
    }
    return {
        "reference_market_snapshot_id": rows[0]["market_group_snapshot_id"],
        "reference_source": ordered_rows[0]["source"],
        "reference_bookmaker": ordered_rows[0]["bookmaker"],
        "market_as_of": ordered_rows[0]["market_as_of"],
        "market_family": ordered_rows[0]["market_family"],
        "normalized_line": ordered_rows[0]["normalized_line"],
        "booksum": sum(implied_probabilities),
        "raw_implied_probabilities": {
            ordered_rows[index]["normalized_selection"]: implied_probabilities[index]
            for index in range(len(ordered_rows))
        },
        "devig_methods": {
            "power": _serialize_devig_result(power, ordered_rows),
            "shin": _serialize_devig_result(shin, ordered_rows),
        },
        "final_devig_method": final_method,
        "final_devig_result": _serialize_devig_result(selected, ordered_rows),
        "devigged_probabilities_by_selection": selection_probabilities,
    }


def _serialize_devig_result(result: DevigResult, rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "method": result.method,
        "configuration_version": DEVIG_CONFIGURATION_VERSION,
        "booksum": result.booksum,
        "implied_probabilities": {
            rows[index]["normalized_selection"]: result.implied_probabilities[index]
            for index in range(len(rows))
        },
        "probabilities": {
            rows[index]["normalized_selection"]: result.probabilities[index]
            for index in range(len(rows))
        },
        "converged": result.converged,
        "iterations": result.iterations,
        "tolerance": result.tolerance,
        "parameter_name": result.parameter_name,
        "parameter_value": result.parameter_value,
    }


def settlement_aware_edge(*, best_available_price: float, effective_win_stake_probability: float, effective_loss_stake_probability: float) -> float:
    return (effective_win_stake_probability * (best_available_price - 1.0)) - effective_loss_stake_probability


def minimum_acceptable_price(*, effective_win_stake_probability: float, effective_loss_stake_probability: float, threshold: float) -> float:
    if effective_win_stake_probability <= 0.0:
        return math.inf
    return 1.0 + ((effective_loss_stake_probability + threshold) / effective_win_stake_probability)


def resolve_competition_threshold(competition_id: str) -> float:
    if competition_id not in _THRESHOLD_BY_COMPETITION:
        raise KeyError(competition_id)
    return _THRESHOLD_BY_COMPETITION[competition_id]


def is_domestic_cup_competition(competition_id: str) -> bool:
    return competition_id in _DOMESTIC_CUP_COMPETITIONS


def choose_best_available_offer(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    valid_rows = [row for row in rows if float(row["decimal_odds"]) > 1.0]
    if not valid_rows:
        return None
    return max(
        valid_rows,
        key=lambda row: (
            float(row["decimal_odds"]),
            str(row["bookmaker"]),
            str(row["source"]),
            str(row["market_snapshot_id"]),
        ),
    )


def _stable_reasons(reasons: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.add(reason)
            ordered.append(reason)
    return ordered


def apply_day11_manager_rules(
    *,
    model_row: dict[str, Any],
    reference_market: dict[str, Any] | None,
    available_offer_rows: Sequence[dict[str, Any]],
    decision_as_of: str,
    team_level_metadata: dict[str, int] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    model_as_of = str(model_row["as_of"])
    kickoff_at = str(model_row["kickoff_at"])
    competition_id = str(model_row["competition_id"])
    market_family = str(model_row["market_family"])

    if _parse_iso_datetime(model_as_of) > _parse_iso_datetime(kickoff_at):
        return {"decision": DECISION_REJECTED_RULE, "reason_codes": [REASON_INVALID_TIMESTAMP]}

    if reference_market is None:
        return {"decision": DECISION_MISSING_REQUIRED_MARKET_DATA, "reason_codes": [REASON_MISSING_REFERENCE_MARKET]}

    reference_as_of = str(reference_market["market_as_of"])
    if _parse_iso_datetime(reference_as_of) > _parse_iso_datetime(decision_as_of):
        return {"decision": DECISION_REJECTED_RULE, "reason_codes": [REASON_INVALID_TIMESTAMP]}
    if _parse_iso_datetime(decision_as_of) > _parse_iso_datetime(kickoff_at):
        return {"decision": DECISION_REJECTED_RULE, "reason_codes": [REASON_INVALID_TIMESTAMP]}

    if available_offer_rows and all(row["freshness_state"] == FRESHNESS_STATE_STALE for row in available_offer_rows):
        return {"decision": DECISION_STALE_MARKET, "reason_codes": [REASON_STALE_MARKET]}

    try:
        threshold = resolve_competition_threshold(competition_id)
    except KeyError:
        return {"decision": DECISION_REJECTED_RULE, "reason_codes": [REASON_UNKNOWN_COMPETITION_THRESHOLD]}

    if is_domestic_cup_competition(competition_id):
        if market_family not in _ALLOWED_CUP_MARKETS:
            return {"decision": DECISION_REJECTED_RULE, "reason_codes": [REASON_REJECT_CUP_MARKET_NOT_ALLOWED]}
        if team_level_metadata is None:
            return {"decision": DECISION_REJECTED_RULE, "reason_codes": [REASON_MISSING_TEAM_LEVEL_METADATA]}
        if min(team_level_metadata.values()) > 4:
            return {"decision": DECISION_REJECTED_RULE, "reason_codes": [REASON_REJECT_TEAM_LEVEL_OUT_OF_SCOPE]}

    best_available = choose_best_available_offer(available_offer_rows)
    if best_available is None:
        invalid_found = any(float(row["decimal_odds"]) <= 1.0 for row in available_offer_rows)
        reason_codes = [REASON_MISSING_AVAILABLE_PRICE]
        if invalid_found:
            reason_codes.append(REASON_INVALID_ODDS)
        return {"decision": DECISION_MISSING_REQUIRED_MARKET_DATA, "reason_codes": reason_codes}

    effective_win_stake_probability = float(model_row["effective_win_stake_probability"])
    effective_loss_stake_probability = float(model_row["effective_loss_stake_probability"])
    edge = settlement_aware_edge(
        best_available_price=float(best_available["decimal_odds"]),
        effective_win_stake_probability=effective_win_stake_probability,
        effective_loss_stake_probability=effective_loss_stake_probability,
    )
    min_price = minimum_acceptable_price(
        effective_win_stake_probability=effective_win_stake_probability,
        effective_loss_stake_probability=effective_loss_stake_probability,
        threshold=threshold,
    )
    passes_edge = edge >= (threshold - 1e-12)
    passes_price = float(best_available["decimal_odds"]) >= (min_price - 1e-12)
    if passes_edge:
        reasons.append(REASON_PASS_EDGE_THRESHOLD)
    else:
        reasons.append(REASON_NO_BET_EDGE_BELOW_THRESHOLD)
    if passes_price:
        reasons.append(REASON_PASS_MINIMUM_PRICE)
    else:
        reasons.append(REASON_NO_BET_PRICE_BELOW_MINIMUM)

    return {
        "decision": DECISION_PRELIMINARY_CANDIDATE if (passes_edge and passes_price) else DECISION_NO_BET,
        "reason_codes": _stable_reasons(reasons),
        "best_available_offer": best_available,
        "competition_threshold": threshold,
        "settlement_aware_edge": edge,
        "minimum_acceptable_price": min_price,
    }


def _index_model_rows(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        indexed[
            canonical_market_identity(
                fixture_id=str(row["fixture_id"]),
                market_family=str(row["market_family"]),
                normalized_line=None if row["line"] is None else float(row["line"]),
                normalized_selection=str(row["selection"]),
            )
        ] = row
    return indexed


def _group_snapshots_by_quote(rows: Sequence[dict[str, Any]]) -> dict[tuple[str, str, float | None, str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, float | None, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row["fixture_id"]),
            str(row["market_family"]),
            row["normalized_line"],
            str(row["source"]),
            str(row["bookmaker"]),
        )
        grouped[key].append(row)
    return grouped


def _group_snapshots_by_selection(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["canonical_market_identity"])].append(row)
    return grouped


def _compact_snapshot_artifact_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = _group_snapshots_by_quote(rows)
    compact_rows: list[dict[str, Any]] = []
    for key in sorted(grouped):
        quote_rows = grouped[key]
        first = quote_rows[0]
        compact_rows.append(
            {
                "snapshot_schema_version": NORMALIZED_MARKET_SNAPSHOT_VERSION,
                "fixture_id": first["fixture_id"],
                "event_id": first["event_id"],
                "competition_id": first["competition_id"],
                "competition_name": first["competition_name"],
                "season_code": first["season_code"],
                "division_code": first["division_code"],
                "kickoff_at": first["kickoff_at"],
                "market_group_snapshot_id": first["market_group_snapshot_id"],
                "source_snapshot_version_id": first["source_snapshot_version_id"],
                "source": first["source"],
                "bookmaker": first["bookmaker"],
                "reference_designation": first["reference_designation"],
                "market_family": first["market_family"],
                "normalized_line": first["normalized_line"],
                "market_as_of": first["market_as_of"],
                "freshness_state": first["freshness_state"],
                "selection_prices": {
                    row["normalized_selection"]: row["decimal_odds"]
                    for row in sorted(quote_rows, key=lambda row: str(row["normalized_selection"]))
                },
            }
        )
    return compact_rows


def _reference_priority(source: str) -> tuple[int, str]:
    try:
        return (_REFERENCE_SOURCE_PRIORITY.index(source), source)
    except ValueError:
        return (999, source)


def _select_reference_market_for_group(group_rows: Sequence[dict[str, Any]], *, final_method: str) -> tuple[dict[str, Any] | None, str | None]:
    by_quote = _group_snapshots_by_quote(group_rows)
    candidates = sorted(
        by_quote.values(),
        key=lambda rows: _reference_priority(str(rows[0]["source"])),
    )
    if not candidates:
        return None, REASON_MISSING_REFERENCE_MARKET
    saw_reference = False
    for candidate in candidates:
        if candidate[0]["reference_designation"] != "reference":
            continue
        saw_reference = True
        validation = validate_reference_market_rows(candidate)
        if validation["is_complete"]:
            analysis = build_reference_market_booksum(candidate, final_method=final_method)
            return analysis, None
        if validation["reason"] == REASON_INVALID_ODDS:
            return None, REASON_INVALID_ODDS
    if saw_reference:
        return None, REASON_INCOMPLETE_REFERENCE_MARKET
    return None, REASON_MISSING_REFERENCE_MARKET


def _build_decision_record(
    *,
    model_row: dict[str, Any],
    reference_market: dict[str, Any] | None,
    decision_payload: dict[str, Any],
    decision_as_of: str,
    available_offer_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    best_available_offer = decision_payload.get("best_available_offer")
    settlement = dict(model_row["settlement_probabilities"])
    record = {
        "decision_record_schema_version": DECISION_RECORD_SCHEMA_VERSION,
        "manager_version": MANAGER_VERSION,
        "manager_policy_version": MANAGER_POLICY_VERSION,
        "decision_id": canonical_hash(
            {
                "fixture_id": model_row["fixture_id"],
                "market_family": model_row["market_family"],
                "line": model_row["line"],
                "selection": model_row["selection"],
                "best_available_snapshot_id": None
                if best_available_offer is None
                else best_available_offer["market_snapshot_id"],
                "reference_market_snapshot_id": None
                if reference_market is None
                else reference_market["reference_market_snapshot_id"],
                "decision": decision_payload["decision"],
                "reason_codes": decision_payload["reason_codes"],
                "manager_version": MANAGER_VERSION,
            }
        ),
        "fixture_id": model_row["fixture_id"],
        "event_id": model_row["event_id"],
        "competition_id": model_row["competition_id"],
        "competition_name": model_row["competition_name"],
        "season_code": model_row["season_code"],
        "division_code": model_row["division_code"],
        "match_date": model_row["match_date"],
        "kickoff_at": model_row["kickoff_at"],
        "model_as_of": model_row["as_of"],
        "decision_as_of": decision_as_of,
        "market_as_of": None if reference_market is None else reference_market["market_as_of"],
        "market_family": model_row["market_family"],
        "line": model_row["line"],
        "selection": model_row["selection"],
        "model_fair_decimal_price": model_row["fair_decimal_price"],
        "source_scoreline_object_id": model_row.get("source_scoreline_object_id"),
        "source_corner_object_id": model_row.get("source_corner_object_id"),
        "model_versions": dict(model_row["model_versions"]),
        "pricing_model_version": model_row["pricing_model_version"],
        "settlement_engine_version": model_row["settlement_engine_version"],
        "settlement_pricing_policy_id": model_row["settlement_pricing_policy_id"],
        "full_win_probability": settlement["full_win_probability"],
        "half_win_probability": settlement["half_win_probability"],
        "push_probability": settlement["push_probability"],
        "half_loss_probability": settlement["half_loss_probability"],
        "full_loss_probability": settlement["full_loss_probability"],
        "effective_win_stake_probability": model_row["effective_win_stake_probability"],
        "effective_loss_stake_probability": model_row["effective_loss_stake_probability"],
        "reference_market_snapshot_id": None
        if reference_market is None
        else reference_market["reference_market_snapshot_id"],
        "reference_source": None if reference_market is None else reference_market["reference_source"],
        "reference_bookmaker": None if reference_market is None else reference_market["reference_bookmaker"],
        "reference_booksum": None if reference_market is None else reference_market["booksum"],
        "reference_raw_implied_probability": None
        if reference_market is None
        else reference_market["raw_implied_probabilities"].get(str(model_row["selection"])),
        "final_devig_method": None if reference_market is None else reference_market["final_devig_method"],
        "final_devig_configuration_version": None
        if reference_market is None
        else reference_market["final_devig_result"]["configuration_version"],
        "final_devig_parameter_name": None
        if reference_market is None
        else reference_market["final_devig_result"]["parameter_name"],
        "final_devig_parameter_value": None
        if reference_market is None
        else reference_market["final_devig_result"]["parameter_value"],
        "final_devig_converged": None
        if reference_market is None
        else reference_market["final_devig_result"]["converged"],
        "final_devig_iterations": None
        if reference_market is None
        else reference_market["final_devig_result"]["iterations"],
        "devigged_reference_probability": None
        if reference_market is None
        else reference_market["devigged_probabilities_by_selection"].get(str(model_row["selection"])),
        "probability_gap_vs_reference": None
        if reference_market is None
        else (
            model_row["effective_win_stake_probability"]
            - reference_market["devigged_probabilities_by_selection"].get(str(model_row["selection"]))
        ),
        "best_available_price": None if best_available_offer is None else best_available_offer["decimal_odds"],
        "best_available_bookmaker": None if best_available_offer is None else best_available_offer["bookmaker"],
        "best_available_source": None if best_available_offer is None else best_available_offer["source"],
        "best_available_market_snapshot_id": None
        if best_available_offer is None
        else best_available_offer["market_snapshot_id"],
        "best_available_provider_market_id": None
        if best_available_offer is None
        else best_available_offer["provider_market_id"],
        "best_available_offer_count": len([row for row in available_offer_rows if float(row["decimal_odds"]) > 1.0]),
        "available_offer_count": len([row for row in available_offer_rows if float(row["decimal_odds"]) > 1.0]),
        "competition_threshold": decision_payload.get("competition_threshold"),
        "minimum_acceptable_price": decision_payload.get("minimum_acceptable_price"),
        "settlement_aware_edge": decision_payload.get("settlement_aware_edge"),
        "decision": decision_payload["decision"],
        "reason_codes": decision_payload["reason_codes"],
        "input_label": _DEMO_INPUT_BANNER,
        "day11_manager_core_ready": "YES",
        "live_paper_pipeline_ready": "NO",
    }
    return record


def run_day11_manager_experiment(config: Day11ManagerConfig | None = None) -> dict[str, Any]:
    resolved_config = default_day11_manager_config() if config is None else config
    model_rows = _read_jsonl(resolved_config.day10_market_prices_path)
    model_index = _index_model_rows(model_rows)
    model_event_ids = {str(row["event_id"]) for row in model_rows}
    snapshots = load_historical_market_snapshots(
        resolved_config.research_db_path,
        freshness_max_age_minutes=resolved_config.freshness_max_age_minutes,
    )
    snapshots = [snapshot for snapshot in snapshots if str(snapshot["event_id"]) in model_event_ids]
    compact_snapshot_rows = _compact_snapshot_artifact_rows(snapshots)
    snapshots_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    snapshots_by_selection = _group_snapshots_by_selection(snapshots)
    for snapshot in snapshots:
        snapshots_by_group[str(snapshot["market_group_snapshot_id"])].append(snapshot)

    reference_analysis_by_group: dict[str, dict[str, Any]] = {}
    group_missing_reasons: dict[str, str] = {}
    for group_id, group_rows in snapshots_by_group.items():
        reference_analysis, missing_reason = _select_reference_market_for_group(
            group_rows,
            final_method=resolved_config.final_devig_method,
        )
        if reference_analysis is not None:
            reference_analysis_by_group[group_id] = reference_analysis
        elif missing_reason is not None:
            group_missing_reasons[group_id] = missing_reason

    decision_records: list[dict[str, Any]] = []
    devig_outputs: list[dict[str, Any]] = []
    joined_rows: list[dict[str, Any]] = []
    matched_selection_count = 0
    for canonical_id, selection_rows in sorted(snapshots_by_selection.items()):
        model_row = model_index.get(canonical_id)
        if model_row is None:
            continue
        matched_selection_count += 1
        market_group_id = str(selection_rows[0]["market_group_snapshot_id"])
        group_rows = snapshots_by_group[market_group_id]
        reference_market = reference_analysis_by_group.get(market_group_id)
        if reference_market is not None and not any(
            output["reference_market_snapshot_id"] == reference_market["reference_market_snapshot_id"]
            for output in devig_outputs
        ):
            devig_outputs.append(reference_market)
        offer_rows = [
            row
            for row in selection_rows
            if row["reference_designation"] == "available"
        ]
        decision_as_of = max(str(model_row["as_of"]), str(selection_rows[0]["market_as_of"]))
        if reference_market is None:
            decision_payload = {
                "decision": DECISION_MISSING_REQUIRED_MARKET_DATA,
                "reason_codes": [group_missing_reasons.get(market_group_id, REASON_MISSING_REFERENCE_MARKET)],
            }
        else:
            decision_payload = apply_day11_manager_rules(
                model_row=model_row,
                reference_market=reference_market,
                available_offer_rows=offer_rows,
                decision_as_of=decision_as_of,
            )
        decision_record = _build_decision_record(
            model_row=model_row,
            reference_market=reference_market,
            decision_payload=decision_payload,
            decision_as_of=decision_as_of,
            available_offer_rows=offer_rows,
        )
        decision_records.append(decision_record)
        joined_rows.append(
            {
                "decision_id": decision_record["decision_id"],
                "canonical_market_identity": canonical_id,
                "reference_market_snapshot_id": decision_record["reference_market_snapshot_id"],
                "best_available_market_snapshot_id": decision_record["best_available_market_snapshot_id"],
                "decision": decision_record["decision"],
                "reason_codes": decision_record["reason_codes"],
            }
        )

    decision_counts = Counter(record["decision"] for record in decision_records)
    competition_counts = Counter(record["competition_id"] for record in decision_records)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "run_timestamp_utc": _utc_now(),
        "run_date_stamp": RUN_DATE_STAMP,
        "manager_version": MANAGER_VERSION,
        "manager_policy_version": MANAGER_POLICY_VERSION,
        "decision_record_schema_version": DECISION_RECORD_SCHEMA_VERSION,
        "normalized_market_snapshot_version": NORMALIZED_MARKET_SNAPSHOT_VERSION,
        "reference_market_policy_version": REFERENCE_MARKET_POLICY_VERSION,
        "threshold_config_version": THRESHOLD_CONFIG_VERSION,
        "domestic_cup_policy_version": DOMESTIC_CUP_POLICY_VERSION,
        "freshness_policy_version": FRESHNESS_POLICY_VERSION,
        "historical_market_input_policy_version": HISTORICAL_MARKET_INPUT_POLICY_VERSION,
        "final_devig_method": resolved_config.final_devig_method,
        "devig_configuration_version": DEVIG_CONFIGURATION_VERSION,
        "day11_manager_core_ready": "YES",
        "live_paper_pipeline_ready": "NO",
        "input_label": _DEMO_INPUT_BANNER,
        "model_row_count": len(model_rows),
        "normalized_market_snapshot_count": len(compact_snapshot_rows),
        "matched_selection_count": matched_selection_count,
        "reference_market_count": len(devig_outputs),
        "decision_record_count": len(decision_records),
        "decision_counts": dict(decision_counts),
        "competition_counts": dict(competition_counts),
        "source_paths": {
            "day10_market_prices_path": str(Path(resolved_config.day10_market_prices_path)),
            "research_db_path": str(Path(resolved_config.research_db_path)),
        },
        "technical_artifacts": {
            "normalized_market_snapshot_jsonl": str(
                Path(resolved_config.technical_output_dir)
                / f"football_day11_manager_{RUN_DATE_STAMP}_normalized_market_snapshots.jsonl"
            ),
            "joined_rows_jsonl": str(
                Path(resolved_config.technical_output_dir)
                / f"football_day11_manager_{RUN_DATE_STAMP}_joined_rows.jsonl"
            ),
            "devig_outputs_jsonl": str(
                Path(resolved_config.technical_output_dir)
                / f"football_day11_manager_{RUN_DATE_STAMP}_devig_outputs.jsonl"
            ),
            "decision_records_jsonl": str(
                Path(resolved_config.product_output_dir) / "football_day11_decisions.jsonl"
            ),
            "manifest_json": str(
                Path(resolved_config.technical_output_dir)
                / f"football_day11_manager_{RUN_DATE_STAMP}_manifest.json"
            ),
        },
    }

    technical_paths = {
        name: Path(path)
        for name, path in manifest["technical_artifacts"].items()
    }
    _write_jsonl(technical_paths["normalized_market_snapshot_jsonl"], compact_snapshot_rows)
    _write_jsonl(technical_paths["joined_rows_jsonl"], joined_rows)
    _write_jsonl(technical_paths["devig_outputs_jsonl"], devig_outputs)
    _write_jsonl(technical_paths["decision_records_jsonl"], decision_records)
    _write_json(technical_paths["manifest_json"], manifest)

    return {
        "manifest": manifest,
        "normalized_market_snapshots": compact_snapshot_rows,
        "joined_rows": joined_rows,
        "devig_outputs": devig_outputs,
        "decision_records": decision_records,
    }
