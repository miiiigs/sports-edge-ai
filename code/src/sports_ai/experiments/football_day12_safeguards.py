"""Day 12 football safeguard layer built on frozen Day 11 preliminary candidates.

This layer remains downstream-only:

- It consumes frozen Day 11A preliminary candidates as immutable inputs.
- It reuses frozen Day 10 lineage/support rows for deterministic safeguard checks.
- It applies AH line-preference logic, AH/1X2 derivation consistency checks,
  and an xG sanity-check contract.

It does not invent xG, connect live providers, or publish real-money recommendations.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..locking import canonical_hash
from .football_day11_manager_core import (
    DECISION_PRELIMINARY_CANDIDATE,
    normalize_line,
)

EXPERIMENT_ID = "EXP-015"
DAY12_VERSION = "football_day12_safeguards_v1"
DAY12_POLICY_VERSION = "football_day12_safeguard_policy_v1"
DAY12_CANDIDATE_RECORD_SCHEMA_VERSION = "football_day12_candidate_record_v1"
DAY12_MANIFEST_VERSION = "football_day12_safeguard_manifest_v1"
DAY12_XG_INPUT_SCHEMA_VERSION = "football_day12_xg_input_v1"
DAY12_XG_POLICY_VERSION = "football_day12_xg_sanity_check_v1"
DAY12_AH_POLICY_VERSION = "football_day12_ah_line_preference_v1"
DAY12_DERIVATION_POLICY_VERSION = "football_day12_ah_1x2_consistency_v1"
RUN_DATE_STAMP = "2026-08-19"

INPUT_LABEL = "HISTORICAL / DEMONSTRATION MARKET INPUT"
NOT_LIVE_OUTPUT_LABEL = "NOT LIVE BETTING OUTPUT"
NOT_REAL_MONEY_LABEL = "NOT REAL-MONEY RECOMMENDATIONS"
REAL_HISTORICAL_MARKET_COVERAGE = "archived closing 1X2 only"
CODE_UNIT_TEST_MARKET_SUPPORT = "1X2 plus generic AH safeguard logic and xG-contract coverage"

MARKET_FAMILY_1X2 = "1x2"
MARKET_FAMILY_AH = "asian_handicap"

DAY12_EXPERIMENTAL_CANDIDATE = "EXPERIMENTAL_CANDIDATE"
DAY12_REJECTED_BY_AH_RULE = "REJECTED_BY_AH_RULE"
DAY12_REJECTED_BY_XG_VETO = "REJECTED_BY_XG_VETO"
DAY12_REJECTED_BY_MULTIPLE_VETOES = "REJECTED_BY_MULTIPLE_VETOES"
DAY12_BLOCKED_MISSING_XG_DATA = "BLOCKED_MISSING_XG_DATA"
DAY12_DERIVATION_ERROR = "DERIVATION_ERROR"
DAY12_REVIEW_REQUIRED = "DAY12_REVIEW_REQUIRED"

AH_STATUS_NOT_APPLICABLE = "AH_NOT_APPLICABLE"
AH_STATUS_PASS = "AH_PASS"
AH_STATUS_REJECT = "AH_REJECT"

DERIVATION_STATUS_PASS = "DERIVATION_PASS"
DERIVATION_STATUS_ERROR = "DERIVATION_ERROR"
DERIVATION_STATUS_REVIEW = "DERIVATION_REVIEW_REQUIRED"

XG_STATUS_NOT_APPLICABLE = "XG_NOT_APPLICABLE"
XG_STATUS_PASS = "XG_PASS"
XG_STATUS_VETO = "XG_VETO"
XG_STATUS_MISSING = "XG_MISSING"
XG_STATUS_REVIEW = "XG_REVIEW_REQUIRED"

REASON_AH_NOT_APPLICABLE = "AH_NOT_APPLICABLE"
REASON_AH_CANONICAL_LINE_SELECTED = "AH_CANONICAL_LINE_SELECTED"
REASON_AH_NON_CANONICAL_LINE_REJECTED = "AH_NON_CANONICAL_LINE_REJECTED"
REASON_AH_INTEGER_LINE_PREFERRED = "AH_INTEGER_LINE_PREFERRED"
REASON_AH_THREE_QUARTER_LINE_PREFERRED = "AH_THREE_QUARTER_LINE_PREFERRED"
REASON_AH_FALLBACK_LINE_SELECTED = "AH_FALLBACK_LINE_SELECTED"
REASON_AS_OF_AFTER_KICKOFF = "AS_OF_AFTER_KICKOFF"
REASON_DECISION_NOT_PRELIMINARY_CANDIDATE = "DECISION_NOT_PRELIMINARY_CANDIDATE"
REASON_MISSING_DAY10_SUPPORT = "MISSING_DAY10_SUPPORT"
REASON_SOURCE_SCORELINE_OBJECT_MISMATCH = "SOURCE_SCORELINE_OBJECT_MISMATCH"
REASON_SOURCE_CORNER_OBJECT_MISMATCH = "SOURCE_CORNER_OBJECT_MISMATCH"
REASON_DERIVATION_SUPPORT_MISSING = "DERIVATION_SUPPORT_MISSING"
REASON_DERIVATION_AH_HOME_MINUS_HALF_MISMATCH = "DERIVATION_AH_HOME_MINUS_HALF_MISMATCH"
REASON_DERIVATION_AH_AWAY_PLUS_HALF_MISMATCH = "DERIVATION_AH_AWAY_PLUS_HALF_MISMATCH"
REASON_DERIVATION_AH_ZERO_PUSH_MISMATCH = "DERIVATION_AH_ZERO_PUSH_MISMATCH"
REASON_XG_NOT_REQUIRED_FOR_COMPETITION = "XG_NOT_REQUIRED_FOR_COMPETITION"
REASON_XG_DATA_NOT_SUPPLIED = "XG_DATA_NOT_SUPPLIED"
REASON_XG_DATA_MISSING_HOME = "XG_DATA_MISSING_HOME"
REASON_XG_DATA_MISSING_AWAY = "XG_DATA_MISSING_AWAY"
REASON_XG_DATA_NOT_POINT_IN_TIME_VALID = "XG_DATA_NOT_POINT_IN_TIME_VALID"
REASON_XG_WINDOW_TOO_SMALL = "XG_WINDOW_TOO_SMALL"
REASON_XG_HOME_DIVERGENCE = "XG_HOME_DIVERGENCE"
REASON_XG_AWAY_DIVERGENCE = "XG_AWAY_DIVERGENCE"
REASON_XG_INPUT_PATH_UNCONFIGURED = "XG_INPUT_PATH_UNCONFIGURED"

EDGE_BUCKET_LT_10 = "<10%"
EDGE_BUCKET_10_25 = "10-25%"
EDGE_BUCKET_25_50 = "25-50%"
EDGE_BUCKET_50_100 = "50-100%"
EDGE_BUCKET_GT_100 = ">100%"
EDGE_BUCKET_ORDER = (
    EDGE_BUCKET_LT_10,
    EDGE_BUCKET_10_25,
    EDGE_BUCKET_25_50,
    EDGE_BUCKET_50_100,
    EDGE_BUCKET_GT_100,
)

XG_APPLICABLE_COMPETITIONS = frozenset({"ENG1", "ENG2", "ESP1", "GER1", "ITA1", "FRA1"})
DEFAULT_XG_MAX_ABS_GAP_PER_TEAM = 0.75
DEFAULT_XG_MIN_WINDOW_MATCHES = 5
DEFAULT_DERIVATION_TOLERANCE = 1e-9


@dataclass(frozen=True)
class Day12Config:
    day11_decisions_path: str | Path
    day10_model_prices_csv_path: str | Path
    technical_output_dir: str | Path
    product_output_dir: str | Path
    latest_output_dir: str | Path
    xg_data_path: str | Path | None = None
    xg_max_abs_gap_per_team: float = DEFAULT_XG_MAX_ABS_GAP_PER_TEAM
    xg_min_window_matches: int = DEFAULT_XG_MIN_WINDOW_MATCHES
    derivation_tolerance: float = DEFAULT_DERIVATION_TOLERANCE


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def default_day12_config() -> Day12Config:
    repo_root = _repo_root()
    return Day12Config(
        day11_decisions_path=repo_root / "research/product_outputs/football/day11/football_day11_decisions.jsonl",
        day10_model_prices_csv_path=repo_root / "research/product_outputs/football/day10/football_day10_model_prices.csv",
        technical_output_dir=repo_root / "research/football_research",
        product_output_dir=repo_root / "research/product_outputs/football/day12",
        latest_output_dir=repo_root / "research/product_outputs/football/latest",
        xg_data_path=None,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


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


def load_day11_preliminary_candidates(path: str | Path) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in _read_jsonl(path)
        if str(row.get("decision")) == DECISION_PRELIMINARY_CANDIDATE
    ]


def _line_type_label(line: float) -> str:
    fractional = abs(line) % 1.0
    if fractional < 1e-9:
        return "integer"
    if abs(fractional - 0.75) < 1e-9:
        return "three_quarter"
    if abs(fractional - 0.25) < 1e-9:
        return "quarter"
    if abs(fractional - 0.5) < 1e-9:
        return "half"
    raise ValueError(f"unsupported AH line type {line!r}")


def _ah_line_preference_rank(line: float) -> tuple[int, str]:
    label = _line_type_label(line)
    order = {
        "integer": 0,
        "three_quarter": 1,
        "quarter": 2,
        "half": 3,
    }
    return (order[label], label)


def _candidate_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row["fixture_id"]), str(row["decision_id"]))


def select_canonical_ah_candidate_ids(rows: Sequence[dict[str, Any]]) -> dict[tuple[str, str], str]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row["market_family"]) != MARKET_FAMILY_AH or row.get("line") is None:
            continue
        grouped[(str(row["fixture_id"]), str(row["selection"]))].append(row)

    selected: dict[tuple[str, str], str] = {}
    for group_key, group_rows in grouped.items():
        ordered = sorted(
            group_rows,
            key=lambda row: (
                _ah_line_preference_rank(float(row["line"]))[0],
                -float(row.get("settlement_aware_edge") or 0.0),
                -float(row.get("best_available_price") or 0.0),
                abs(float(row["line"])),
                float(row["line"]),
                str(row["decision_id"]),
            ),
        )
        selected[group_key] = str(ordered[0]["decision_id"])
    return selected


def load_day10_support(
    csv_path: str | Path,
    *,
    fixture_ids: set[str],
) -> dict[str, dict[str, Any]]:
    support: dict[str, dict[str, Any]] = {}
    with Path(csv_path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            fixture_id = str(row["fixture_id"])
            if fixture_id not in fixture_ids:
                continue
            fixture_support = support.setdefault(
                fixture_id,
                {
                    "fixture_id": fixture_id,
                    "event_id": str(row["event_id"]),
                    "home_team": str(row["home_team"]),
                    "away_team": str(row["away_team"]),
                    "competition_id": str(row["competition_id"]),
                    "competition_name": str(row["competition_name"]),
                    "season_code": str(row["season_code"]),
                    "division_code": str(row["division_code"]),
                    "match_date": str(row["match_date"]),
                    "kickoff_at": str(row["kickoff_at"]),
                    "as_of": str(row["as_of"]),
                    "expected_goals_home": float(row["expected_goals_home"]),
                    "expected_goals_away": float(row["expected_goals_away"]),
                    "expected_total_goals": float(row["expected_total_goals"]),
                    "scoreline_object_id": str(row["scoreline_object_id"]) or None,
                    "corner_object_id": str(row["corner_object_id"]) or None,
                    "market_rows": {},
                },
            )
            market_family = str(row["market_family"])
            if market_family not in {MARKET_FAMILY_1X2, MARKET_FAMILY_AH}:
                continue
            normalized_line = None if row["line"] in {"", None} else normalize_line(row["line"])
            key = (market_family, normalized_line, str(row["selection"]))
            fixture_support["market_rows"][key] = {
                "fair_decimal_price": float(row["fair_decimal_price"]),
                "full_win_probability": float(row["full_win_probability"]),
                "push_probability": float(row["push_probability"]),
                "full_loss_probability": float(row["full_loss_probability"]),
            }
    return support


def inspect_xg_source(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "exists": False,
            "path": None,
            "source_names": [],
            "competitions_covered": [],
            "seasons_covered": [],
            "point_in_time_valid": False,
            "sufficient_for_day12_rule": False,
            "reason": REASON_XG_INPUT_PATH_UNCONFIGURED,
        }

    resolved = Path(path)
    if not resolved.exists():
        return {
            "exists": False,
            "path": str(resolved),
            "source_names": [],
            "competitions_covered": [],
            "seasons_covered": [],
            "point_in_time_valid": False,
            "sufficient_for_day12_rule": False,
            "reason": REASON_XG_DATA_NOT_SUPPLIED,
        }

    rows = _read_jsonl(resolved)
    source_names = sorted({str(row.get("source", "")) for row in rows if row.get("source")})
    competitions = sorted({str(row.get("competition_id", "")) for row in rows if row.get("competition_id")})
    seasons = sorted({str(row.get("season_code", "")) for row in rows if row.get("season_code")})
    point_in_time_valid = all(
        row.get("as_of") and row.get("window_end_at") and row.get("window_start_at")
        for row in rows
    )
    sufficient = all(
        row.get("team_role") in {"HOME", "AWAY"} and row.get("recent_xg_for_per_match") is not None
        for row in rows
    )
    return {
        "exists": True,
        "path": str(resolved),
        "source_names": source_names,
        "competitions_covered": competitions,
        "seasons_covered": seasons,
        "point_in_time_valid": point_in_time_valid,
        "sufficient_for_day12_rule": bool(rows) and point_in_time_valid and sufficient,
        "row_count": len(rows),
        "reason": None,
    }


def load_xg_rows(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    resolved = Path(path)
    if not resolved.exists():
        return []
    return _read_jsonl(resolved)


def _index_xg_rows(rows: Sequence[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        indexed[(str(row["fixture_id"]), str(row["team_role"]).upper())] = dict(row)
    return indexed


def validate_day12_candidate_input(
    candidate: dict[str, Any],
    fixture_support: dict[str, Any] | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if str(candidate.get("decision")) != DECISION_PRELIMINARY_CANDIDATE:
        reasons.append(REASON_DECISION_NOT_PRELIMINARY_CANDIDATE)
    if fixture_support is None:
        reasons.append(REASON_MISSING_DAY10_SUPPORT)
        return {"is_valid": False, "reason_codes": reasons}

    if _parse_iso_datetime(str(candidate["model_as_of"])) > _parse_iso_datetime(str(candidate["kickoff_at"])):
        reasons.append(REASON_AS_OF_AFTER_KICKOFF)
    if _parse_iso_datetime(str(candidate["decision_as_of"])) > _parse_iso_datetime(str(candidate["kickoff_at"])):
        reasons.append(REASON_AS_OF_AFTER_KICKOFF)

    market_family = str(candidate["market_family"])
    if market_family != "corner_total":
        expected_scoreline_id = fixture_support.get("scoreline_object_id")
        actual_scoreline_id = candidate.get("source_scoreline_object_id")
        if expected_scoreline_id and actual_scoreline_id != expected_scoreline_id:
            reasons.append(REASON_SOURCE_SCORELINE_OBJECT_MISMATCH)
    if market_family == "corner_total":
        expected_corner_id = fixture_support.get("corner_object_id")
        actual_corner_id = candidate.get("source_corner_object_id")
        if expected_corner_id and actual_corner_id != expected_corner_id:
            reasons.append(REASON_SOURCE_CORNER_OBJECT_MISMATCH)

    return {
        "is_valid": not reasons,
        "reason_codes": reasons,
    }


def evaluate_ah_rule(
    candidate: dict[str, Any],
    *,
    canonical_ah_candidate_ids: dict[tuple[str, str], str],
) -> dict[str, Any]:
    if str(candidate["market_family"]) != MARKET_FAMILY_AH:
        return {
            "status": AH_STATUS_NOT_APPLICABLE,
            "reason_codes": [REASON_AH_NOT_APPLICABLE],
            "selected_decision_id": None,
            "line_type": None,
        }

    line = float(candidate["line"])
    group_key = (str(candidate["fixture_id"]), str(candidate["selection"]))
    selected_decision_id = canonical_ah_candidate_ids.get(group_key)
    line_rank, line_type = _ah_line_preference_rank(line)
    if selected_decision_id == str(candidate["decision_id"]):
        reasons = [REASON_AH_CANONICAL_LINE_SELECTED]
        if line_type == "integer":
            reasons.append(REASON_AH_INTEGER_LINE_PREFERRED)
        elif line_type == "three_quarter":
            reasons.append(REASON_AH_THREE_QUARTER_LINE_PREFERRED)
        else:
            reasons.append(REASON_AH_FALLBACK_LINE_SELECTED)
        return {
            "status": AH_STATUS_PASS,
            "reason_codes": reasons,
            "selected_decision_id": selected_decision_id,
            "line_type": line_type,
            "line_preference_rank": line_rank,
        }
    return {
        "status": AH_STATUS_REJECT,
        "reason_codes": [REASON_AH_NON_CANONICAL_LINE_REJECTED],
        "selected_decision_id": selected_decision_id,
        "line_type": line_type,
        "line_preference_rank": line_rank,
    }


def check_fixture_derivation_consistency(
    fixture_support: dict[str, Any] | None,
    *,
    tolerance: float = DEFAULT_DERIVATION_TOLERANCE,
) -> dict[str, Any]:
    if fixture_support is None:
        return {
            "status": DERIVATION_STATUS_REVIEW,
            "reason_codes": [REASON_MISSING_DAY10_SUPPORT],
        }

    rows = fixture_support["market_rows"]

    def fetch(market_family: str, line: float | None, selection: str) -> dict[str, Any] | None:
        return rows.get((market_family, line, selection))

    home_1x2 = fetch(MARKET_FAMILY_1X2, None, "HOME")
    draw_1x2 = fetch(MARKET_FAMILY_1X2, None, "DRAW")
    away_1x2 = fetch(MARKET_FAMILY_1X2, None, "AWAY")
    home_minus_half = fetch(MARKET_FAMILY_AH, -0.5, "HOME")
    away_plus_half = fetch(MARKET_FAMILY_AH, 0.5, "AWAY")
    home_zero = fetch(MARKET_FAMILY_AH, 0.0, "HOME")
    required = [home_1x2, draw_1x2, away_1x2, home_minus_half, away_plus_half, home_zero]
    if any(row is None for row in required):
        return {
            "status": DERIVATION_STATUS_REVIEW,
            "reason_codes": [REASON_DERIVATION_SUPPORT_MISSING],
        }

    reasons: list[str] = []
    if abs(home_minus_half["full_win_probability"] - home_1x2["full_win_probability"]) > tolerance or abs(
        home_minus_half["fair_decimal_price"] - home_1x2["fair_decimal_price"]
    ) > tolerance:
        reasons.append(REASON_DERIVATION_AH_HOME_MINUS_HALF_MISMATCH)
    if abs(away_plus_half["full_win_probability"] - away_1x2["full_win_probability"]) > tolerance or abs(
        away_plus_half["fair_decimal_price"] - away_1x2["fair_decimal_price"]
    ) > tolerance:
        reasons.append(REASON_DERIVATION_AH_AWAY_PLUS_HALF_MISMATCH)
    if (
        abs(home_zero["full_win_probability"] - home_1x2["full_win_probability"]) > tolerance
        or abs(home_zero["push_probability"] - draw_1x2["full_win_probability"]) > tolerance
        or abs(home_zero["full_loss_probability"] - away_1x2["full_win_probability"]) > tolerance
    ):
        reasons.append(REASON_DERIVATION_AH_ZERO_PUSH_MISMATCH)

    return {
        "status": DERIVATION_STATUS_PASS if not reasons else DERIVATION_STATUS_ERROR,
        "reason_codes": reasons or [],
    }


def evaluate_xg_rule(
    candidate: dict[str, Any],
    *,
    fixture_support: dict[str, Any] | None,
    xg_rows_by_fixture_role: dict[tuple[str, str], dict[str, Any]],
    xg_input_inspection: dict[str, Any],
    max_abs_gap_per_team: float,
    min_window_matches: int,
) -> dict[str, Any]:
    competition_id = str(candidate["competition_id"])
    if competition_id not in XG_APPLICABLE_COMPETITIONS:
        return {
            "status": XG_STATUS_NOT_APPLICABLE,
            "reason_codes": [REASON_XG_NOT_REQUIRED_FOR_COMPETITION],
            "required": False,
        }
    if fixture_support is None:
        return {
            "status": XG_STATUS_REVIEW,
            "reason_codes": [REASON_MISSING_DAY10_SUPPORT],
            "required": True,
        }
    if not xg_input_inspection["exists"]:
        return {
            "status": XG_STATUS_MISSING,
            "reason_codes": [xg_input_inspection["reason"]],
            "required": True,
        }

    fixture_id = str(candidate["fixture_id"])
    home_row = xg_rows_by_fixture_role.get((fixture_id, "HOME"))
    away_row = xg_rows_by_fixture_role.get((fixture_id, "AWAY"))
    if home_row is None or away_row is None:
        reasons = []
        if home_row is None:
            reasons.append(REASON_XG_DATA_MISSING_HOME)
        if away_row is None:
            reasons.append(REASON_XG_DATA_MISSING_AWAY)
        return {
            "status": XG_STATUS_MISSING,
            "reason_codes": reasons,
            "required": True,
        }

    decision_as_of = _parse_iso_datetime(str(candidate["decision_as_of"]))
    for xg_row in (home_row, away_row):
        row_as_of = _parse_iso_datetime(str(xg_row["as_of"]))
        row_window_end = _parse_iso_datetime(str(xg_row["window_end_at"]))
        if row_as_of > decision_as_of or row_window_end > decision_as_of:
            return {
                "status": XG_STATUS_REVIEW,
                "reason_codes": [REASON_XG_DATA_NOT_POINT_IN_TIME_VALID],
                "required": True,
            }
        if int(xg_row["matches_in_window"]) < min_window_matches:
            return {
                "status": XG_STATUS_MISSING,
                "reason_codes": [REASON_XG_WINDOW_TOO_SMALL],
                "required": True,
            }

    home_gap = abs(float(fixture_support["expected_goals_home"]) - float(home_row["recent_xg_for_per_match"]))
    away_gap = abs(float(fixture_support["expected_goals_away"]) - float(away_row["recent_xg_for_per_match"]))
    reasons: list[str] = []
    if home_gap > max_abs_gap_per_team:
        reasons.append(REASON_XG_HOME_DIVERGENCE)
    if away_gap > max_abs_gap_per_team:
        reasons.append(REASON_XG_AWAY_DIVERGENCE)
    return {
        "status": XG_STATUS_VETO if reasons else XG_STATUS_PASS,
        "reason_codes": reasons,
        "required": True,
        "home_recent_xg_for_per_match": float(home_row["recent_xg_for_per_match"]),
        "away_recent_xg_for_per_match": float(away_row["recent_xg_for_per_match"]),
        "home_gap": home_gap,
        "away_gap": away_gap,
        "source": home_row.get("source"),
    }


def determine_day12_disposition(
    *,
    validation: dict[str, Any],
    derivation: dict[str, Any],
    ah_rule: dict[str, Any],
    xg_rule: dict[str, Any],
) -> dict[str, Any]:
    if not validation["is_valid"]:
        return {
            "final_disposition": DAY12_REVIEW_REQUIRED,
            "reason_codes": list(validation["reason_codes"]),
        }
    if derivation["status"] == DERIVATION_STATUS_REVIEW:
        return {
            "final_disposition": DAY12_REVIEW_REQUIRED,
            "reason_codes": list(derivation["reason_codes"]),
        }
    if derivation["status"] == DERIVATION_STATUS_ERROR:
        return {
            "final_disposition": DAY12_DERIVATION_ERROR,
            "reason_codes": list(derivation["reason_codes"]),
        }
    if xg_rule["status"] == XG_STATUS_REVIEW:
        return {
            "final_disposition": DAY12_REVIEW_REQUIRED,
            "reason_codes": list(xg_rule["reason_codes"]),
        }

    vetoes: list[str] = []
    reason_codes: list[str] = []
    if ah_rule["status"] == AH_STATUS_REJECT:
        vetoes.append("AH")
        reason_codes.extend(ah_rule["reason_codes"])
    if xg_rule["status"] == XG_STATUS_VETO:
        vetoes.append("XG")
        reason_codes.extend(xg_rule["reason_codes"])

    if len(vetoes) >= 2:
        return {
            "final_disposition": DAY12_REJECTED_BY_MULTIPLE_VETOES,
            "reason_codes": reason_codes,
        }
    if vetoes == ["AH"]:
        return {
            "final_disposition": DAY12_REJECTED_BY_AH_RULE,
            "reason_codes": reason_codes,
        }
    if vetoes == ["XG"]:
        return {
            "final_disposition": DAY12_REJECTED_BY_XG_VETO,
            "reason_codes": reason_codes,
        }
    if xg_rule["status"] == XG_STATUS_MISSING:
        return {
            "final_disposition": DAY12_BLOCKED_MISSING_XG_DATA,
            "reason_codes": list(xg_rule["reason_codes"]),
        }
    return {
        "final_disposition": DAY12_EXPERIMENTAL_CANDIDATE,
        "reason_codes": list(ah_rule["reason_codes"]) + list(xg_rule["reason_codes"]),
    }


def edge_bucket(edge_value: float | None) -> str:
    edge = 0.0 if edge_value is None else float(edge_value)
    if edge < 0.10:
        return EDGE_BUCKET_LT_10
    if edge < 0.25:
        return EDGE_BUCKET_10_25
    if edge < 0.50:
        return EDGE_BUCKET_25_50
    if edge < 1.00:
        return EDGE_BUCKET_50_100
    return EDGE_BUCKET_GT_100


def _stable_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _build_day12_candidate_record(
    *,
    candidate: dict[str, Any],
    fixture_support: dict[str, Any] | None,
    validation: dict[str, Any],
    derivation: dict[str, Any],
    ah_rule: dict[str, Any],
    xg_rule: dict[str, Any],
    final_disposition: dict[str, Any],
) -> dict[str, Any]:
    fixture_support = fixture_support or {}
    final_reason_codes = _stable_unique(final_disposition["reason_codes"])
    day12_candidate_id = canonical_hash(
        {
            "day11_decision_id": candidate["decision_id"],
            "final_disposition": final_disposition["final_disposition"],
            "reason_codes": final_reason_codes,
            "day12_version": DAY12_VERSION,
        }
    )
    return {
        "day12_candidate_record_schema_version": DAY12_CANDIDATE_RECORD_SCHEMA_VERSION,
        "day12_version": DAY12_VERSION,
        "day12_policy_version": DAY12_POLICY_VERSION,
        "day12_candidate_id": day12_candidate_id,
        "day11_decision_id": candidate["decision_id"],
        "fixture_id": candidate["fixture_id"],
        "event_id": candidate["event_id"],
        "home_team": fixture_support.get("home_team"),
        "away_team": fixture_support.get("away_team"),
        "competition_id": candidate["competition_id"],
        "competition_name": candidate["competition_name"],
        "season_code": candidate["season_code"],
        "division_code": candidate["division_code"],
        "match_date": candidate["match_date"],
        "kickoff_at": candidate["kickoff_at"],
        "model_as_of": candidate["model_as_of"],
        "decision_as_of": candidate["decision_as_of"],
        "market_as_of": candidate["market_as_of"],
        "market_family": candidate["market_family"],
        "line": candidate["line"],
        "selection": candidate["selection"],
        "model_fair_decimal_price": candidate["model_fair_decimal_price"],
        "best_available_price": candidate["best_available_price"],
        "settlement_aware_edge": candidate["settlement_aware_edge"],
        "competition_threshold": candidate["competition_threshold"],
        "minimum_acceptable_price": candidate["minimum_acceptable_price"],
        "day11_manager_version": candidate["manager_version"],
        "pricing_model_version": candidate["pricing_model_version"],
        "settlement_engine_version": candidate["settlement_engine_version"],
        "model_versions": dict(candidate["model_versions"]),
        "source_scoreline_object_id": candidate.get("source_scoreline_object_id"),
        "source_corner_object_id": candidate.get("source_corner_object_id"),
        "fixture_support_scoreline_object_id": fixture_support.get("scoreline_object_id"),
        "fixture_support_corner_object_id": fixture_support.get("corner_object_id"),
        "expected_goals_home": fixture_support.get("expected_goals_home"),
        "expected_goals_away": fixture_support.get("expected_goals_away"),
        "expected_total_goals": fixture_support.get("expected_total_goals"),
        "input_label": INPUT_LABEL,
        "real_historical_market_coverage": REAL_HISTORICAL_MARKET_COVERAGE,
        "code_unit_test_market_support": CODE_UNIT_TEST_MARKET_SUPPORT,
        "validation_status": "VALID" if validation["is_valid"] else "REVIEW_REQUIRED",
        "validation_reason_codes": list(validation["reason_codes"]),
        "ah_rule_status": ah_rule["status"],
        "ah_rule_reason_codes": list(ah_rule["reason_codes"]),
        "ah_selected_decision_id": ah_rule.get("selected_decision_id"),
        "ah_line_type": ah_rule.get("line_type"),
        "ah_line_preference_rank": ah_rule.get("line_preference_rank"),
        "derivation_status": derivation["status"],
        "derivation_reason_codes": list(derivation["reason_codes"]),
        "xg_status": xg_rule["status"],
        "xg_reason_codes": list(xg_rule["reason_codes"]),
        "xg_required_for_competition": bool(xg_rule.get("required", False)),
        "xg_source": xg_rule.get("source"),
        "xg_home_recent_for_per_match": xg_rule.get("home_recent_xg_for_per_match"),
        "xg_away_recent_for_per_match": xg_rule.get("away_recent_xg_for_per_match"),
        "xg_home_gap": xg_rule.get("home_gap"),
        "xg_away_gap": xg_rule.get("away_gap"),
        "xg_gap_threshold": DEFAULT_XG_MAX_ABS_GAP_PER_TEAM,
        "edge_bucket": edge_bucket(candidate.get("settlement_aware_edge")),
        "final_disposition": final_disposition["final_disposition"],
        "final_reason_codes": final_reason_codes,
        "live_paper_pipeline_ready": "NO",
        "not_live_betting_output": NOT_LIVE_OUTPUT_LABEL,
        "not_real_money_recommendations": NOT_REAL_MONEY_LABEL,
    }


def _edge_bucket_summary(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, int]]:
    summary = {
        bucket: {"input": 0, "survived": 0, "rejected": 0, "blocked": 0}
        for bucket in EDGE_BUCKET_ORDER
    }
    for row in rows:
        bucket = str(row["edge_bucket"])
        summary[bucket]["input"] += 1
        disposition = str(row["final_disposition"])
        if disposition == DAY12_EXPERIMENTAL_CANDIDATE:
            summary[bucket]["survived"] += 1
        elif disposition == DAY12_BLOCKED_MISSING_XG_DATA:
            summary[bucket]["blocked"] += 1
        else:
            summary[bucket]["rejected"] += 1
    return summary


def run_day12_safeguard_experiment(config: Day12Config | None = None) -> dict[str, Any]:
    resolved_config = default_day12_config() if config is None else config
    day11_rows = _read_jsonl(resolved_config.day11_decisions_path)
    preliminary_candidates = [
        dict(row)
        for row in day11_rows
        if str(row.get("decision")) == DECISION_PRELIMINARY_CANDIDATE
    ]
    fixture_ids = {str(row["fixture_id"]) for row in preliminary_candidates}
    fixture_support = load_day10_support(
        resolved_config.day10_model_prices_csv_path,
        fixture_ids=fixture_ids,
    )
    xg_input_inspection = inspect_xg_source(resolved_config.xg_data_path)
    xg_rows = load_xg_rows(resolved_config.xg_data_path)
    xg_rows_by_fixture_role = _index_xg_rows(xg_rows)
    canonical_ah_candidate_ids = select_canonical_ah_candidate_ids(preliminary_candidates)
    derivation_by_fixture = {
        fixture_id: check_fixture_derivation_consistency(
            fixture_support.get(fixture_id),
            tolerance=resolved_config.derivation_tolerance,
        )
        for fixture_id in fixture_ids
    }

    candidate_records: list[dict[str, Any]] = []
    for candidate in sorted(preliminary_candidates, key=_candidate_sort_key):
        fixture_id = str(candidate["fixture_id"])
        support_row = fixture_support.get(fixture_id)
        validation = validate_day12_candidate_input(candidate, support_row)
        derivation = derivation_by_fixture[fixture_id]
        ah_rule = evaluate_ah_rule(
            candidate,
            canonical_ah_candidate_ids=canonical_ah_candidate_ids,
        )
        xg_rule = evaluate_xg_rule(
            candidate,
            fixture_support=support_row,
            xg_rows_by_fixture_role=xg_rows_by_fixture_role,
            xg_input_inspection=xg_input_inspection,
            max_abs_gap_per_team=resolved_config.xg_max_abs_gap_per_team,
            min_window_matches=resolved_config.xg_min_window_matches,
        )
        final_disposition = determine_day12_disposition(
            validation=validation,
            derivation=derivation,
            ah_rule=ah_rule,
            xg_rule=xg_rule,
        )
        candidate_records.append(
            _build_day12_candidate_record(
                candidate=candidate,
                fixture_support=support_row,
                validation=validation,
                derivation=derivation,
                ah_rule=ah_rule,
                xg_rule=xg_rule,
                final_disposition=final_disposition,
            )
        )

    disposition_counts = Counter(str(row["final_disposition"]) for row in candidate_records)
    ah_status_counts = Counter(str(row["ah_rule_status"]) for row in candidate_records)
    xg_status_counts = Counter(str(row["xg_status"]) for row in candidate_records)
    derivation_status_counts = Counter(str(row["derivation_status"]) for row in candidate_records)
    competition_counts = Counter(str(row["competition_id"]) for row in candidate_records)
    edge_buckets = _edge_bucket_summary(candidate_records)
    applicable_competition_count = sum(
        1
        for row in candidate_records
        if bool(row["xg_required_for_competition"])
    )
    xg_missing_for_required = xg_status_counts.get(XG_STATUS_MISSING, 0) > 0
    day12_status = "PARTIAL" if (applicable_competition_count > 0 and xg_missing_for_required) else "COMPLETE"
    football_logical_pipeline_ready = "YES" if day12_status == "COMPLETE" else "NO"
    experimental_count = disposition_counts.get(DAY12_EXPERIMENTAL_CANDIDATE, 0)
    manifest = {
        "manifest_version": DAY12_MANIFEST_VERSION,
        "experiment_id": EXPERIMENT_ID,
        "run_timestamp_utc": _utc_now(),
        "run_date_stamp": RUN_DATE_STAMP,
        "day12_version": DAY12_VERSION,
        "day12_policy_version": DAY12_POLICY_VERSION,
        "day12_candidate_record_schema_version": DAY12_CANDIDATE_RECORD_SCHEMA_VERSION,
        "ah_policy_version": DAY12_AH_POLICY_VERSION,
        "derivation_policy_version": DAY12_DERIVATION_POLICY_VERSION,
        "xg_input_schema_version": DAY12_XG_INPUT_SCHEMA_VERSION,
        "xg_policy_version": DAY12_XG_POLICY_VERSION,
        "input_label": INPUT_LABEL,
        "not_live_betting_output": NOT_LIVE_OUTPUT_LABEL,
        "not_real_money_recommendations": NOT_REAL_MONEY_LABEL,
        "real_historical_market_coverage": REAL_HISTORICAL_MARKET_COVERAGE,
        "code_unit_test_market_support": CODE_UNIT_TEST_MARKET_SUPPORT,
        "source_paths": {
            "day11_decisions_path": str(Path(resolved_config.day11_decisions_path)),
            "day10_model_prices_csv_path": str(Path(resolved_config.day10_model_prices_csv_path)),
            "xg_data_path": None if resolved_config.xg_data_path is None else str(Path(resolved_config.xg_data_path)),
        },
        "day11_decision_count": len(day11_rows),
        "day11_preliminary_candidate_count": len(preliminary_candidates),
        "candidate_record_count": len(candidate_records),
        "ignored_non_preliminary_count": len(day11_rows) - len(preliminary_candidates),
        "disposition_counts": dict(disposition_counts),
        "ah_status_counts": dict(ah_status_counts),
        "xg_status_counts": dict(xg_status_counts),
        "derivation_status_counts": dict(derivation_status_counts),
        "competition_counts": dict(competition_counts),
        "edge_bucket_counts": edge_buckets,
        "day12_status": day12_status,
        "football_logical_pipeline_ready": football_logical_pipeline_ready,
        "live_paper_pipeline_ready": "NO",
        "tennis_authorized": "NO",
        "survival_percentage": 0.0 if not candidate_records else (experimental_count / len(candidate_records)),
        "xg_input_inspection": xg_input_inspection,
    }

    technical_candidate_path = (
        Path(resolved_config.technical_output_dir) / f"football_day12_safeguards_{RUN_DATE_STAMP}_candidate_records.jsonl"
    )
    technical_manifest_path = (
        Path(resolved_config.technical_output_dir) / f"football_day12_safeguards_{RUN_DATE_STAMP}_manifest.json"
    )
    _write_jsonl(technical_candidate_path, candidate_records)
    _write_json(technical_manifest_path, manifest)
    manifest["technical_artifacts"] = {
        "candidate_records_jsonl": str(technical_candidate_path),
        "manifest_json": str(technical_manifest_path),
    }
    _write_json(technical_manifest_path, manifest)

    return {
        "manifest": manifest,
        "candidate_records": candidate_records,
    }
