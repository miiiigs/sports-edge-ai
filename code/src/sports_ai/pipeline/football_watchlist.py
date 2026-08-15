"""Generate a blind football watchlist for upcoming fixtures."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import httpx

from ..agents.football.poisson_1x2 import fit_poisson_1x2_from_history
from ..agents.football.poisson_1x2_v3 import fit_poisson_1x2_v3_from_history
from ..data_sources.football_data_co_uk import build_csv_url
from ..data_sources.football_history import DIVISION_TO_COMPETITION
from ..data_sources.football_upcoming import (
    UpcomingFixture,
    discover_upcoming_fixtures,
)
from ..locking import make_lock_row
from ..storage.paper_shadow_ledger import (
    LockRow,
    MarketSnapshot,
    PaperDecision,
    connect as connect_paper_shadow_ledger,
    record_decision,
    record_lock_row,
    record_market_snapshot,
)

SELECTION_ORDER = ("home", "draw", "away")
CANDIDATE_STATUS = "PAPER_SHADOW_CANDIDATE"
CURRENT_ODDS_COLUMNS = {
    "home": ("B365H", "B365CH"),
    "draw": ("B365D", "B365CD"),
    "away": ("B365A", "B365CA"),
}
SUPPORTED_MODEL_NAMES = ("poisson_1x2_v1", "poisson_1x2_v3")


RELIABILITY_MODIFIER_BY_COMPETITION_SELECTION = {
    ("ENG1", "home"): -0.01,
    ("ENG1", "draw"): -0.02,
    ("ENG1", "away"): -0.01,
    ("ENG2", "home"): -0.015,
    ("ENG2", "draw"): -0.025,
    ("ENG2", "away"): -0.015,
    ("ENG3", "home"): -0.005,
    ("ENG3", "draw"): -0.02,
    ("ENG3", "away"): -0.01,
    ("ENG4", "home"): -0.01,
    ("ENG4", "draw"): -0.02,
    ("ENG4", "away"): -0.005,
    ("ESP1", "home"): 0.0,
    ("ESP1", "draw"): -0.02,
    ("ESP1", "away"): -0.005,
}


@dataclass(frozen=True)
class FootballWatchlistConfig:
    research_db_path: str | Path
    season_code: str
    division_codes: tuple[str, ...]
    target_date: str
    bookmaker: str = "Bet365"
    model_name: str = "poisson_1x2_v3"
    min_team_matches: int = 6
    prior_weight: float = 5.0
    strength_regularization: float = 0.03
    edge_threshold: float = 0.03
    min_data_quality: float = 0.82
    max_uncertainty: float = 0.98
    min_uncertainty_adjusted_edge: float = 0.015
    require_model_convergence: bool = True
    allowed_selections: tuple[str, ...] = SELECTION_ORDER
    max_fair_odds: float | None = 2.8
    ledger_db_path: str | Path | None = None
    fixture_source: str = "auto"
    football_data_token: str | None = None
    api_football_token: str | None = None
    api_football_league_map_path: str | Path | None = None
    manual_fixtures_path: str | Path | None = None
    manual_market_path: str | Path | None = None


@dataclass(frozen=True)
class MarketInput:
    bookmaker: str
    captured_at: str
    source_kind: str
    source_ref: str
    price_source_tier: str
    prices: dict[str, float]


@dataclass(frozen=True)
class SelectionDecision:
    selection: str
    available_price: float
    fair_probability: float
    fair_odds: float
    raw_break_even_probability: float
    raw_edge: float
    devig_market_probability: float | None
    model_vs_devig_market: float | None
    expected_value: float
    conservative_probability: float
    uncertainty_adjusted_edge: float
    league_reliability_modifier: float
    reason_code: str
    status: str


def _normalize_date(raw_date: str) -> str:
    parts = raw_date.strip().split("/")
    if len(parts) != 3:
        return raw_date.strip()
    day, month, year = parts
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def _slug(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in text).strip("_")


def _build_event_id(match_date: str, home_team: str, away_team: str) -> str:
    return f"evt_football_{match_date.replace('-', '')}_{_slug(home_team)}_{_slug(away_team)}"


def _connect_research_store(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _competition_identity(division_code: str) -> tuple[str, str]:
    return DIVISION_TO_COMPETITION.get(division_code, (division_code, division_code))


def _safe_float(raw_value: str | None) -> float | None:
    text = (raw_value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _raw_market_probabilities(prices: dict[str, float]) -> dict[str, float]:
    return {
        selection: 1.0 / price
        for selection, price in prices.items()
        if selection in SELECTION_ORDER and price > 1
    }


def _devig_market_probabilities(prices: dict[str, float]) -> dict[str, float]:
    raw = _raw_market_probabilities(prices)
    overround = sum(raw.values())
    if overround <= 0:
        return {}
    return {selection: probability / overround for selection, probability in raw.items()}


def _market_report(prices: dict[str, float]) -> dict[str, Any]:
    raw_probabilities = _raw_market_probabilities(prices)
    devig_probabilities = _devig_market_probabilities(prices)
    overround = sum(raw_probabilities.values())
    return {
        "raw_implied_probabilities": raw_probabilities,
        "bookmaker_overround": overround,
        "bookmaker_margin": overround - 1.0 if overround else None,
        "devig_probabilities": devig_probabilities,
    }


def _model_converged(prediction: dict[str, Any]) -> bool | None:
    details = prediction.get("details") or {}
    value = details.get("converged")
    return value if isinstance(value, bool) else None


def _conservative_probability(
    probability: float,
    *,
    data_quality: float | None,
    uncertainty: float | None,
    reliability_modifier: float,
) -> float:
    quality_penalty = max(0.0, 0.86 - (data_quality or 0.0)) * 0.12
    uncertainty_penalty = max(0.0, (uncertainty or 0.0) - 0.90) * 0.18
    total_penalty = 0.01 + quality_penalty + uncertainty_penalty - min(0.0, reliability_modifier)
    return max(0.0, probability - total_penalty)


def _evaluate_selection_decision(
    *,
    config: FootballWatchlistConfig,
    competition_id: str,
    selection: str,
    price: float,
    prediction: dict[str, Any],
    market_report: dict[str, Any],
) -> SelectionDecision:
    fair_prob = prediction["probabilities"][selection]
    fair_odds = prediction["fair_odds"][selection]
    raw_break_even = 1.0 / price
    raw_edge = fair_prob - raw_break_even
    devig_probability = market_report["devig_probabilities"].get(selection)
    model_vs_devig = fair_prob - devig_probability if devig_probability is not None else None
    expected_value = (fair_prob * price) - 1.0
    data_quality = prediction.get("data_quality")
    uncertainty = prediction.get("uncertainty")
    reliability_modifier = RELIABILITY_MODIFIER_BY_COMPETITION_SELECTION.get((competition_id, selection), 0.0)
    conservative_prob = _conservative_probability(
        fair_prob,
        data_quality=data_quality,
        uncertainty=uncertainty,
        reliability_modifier=reliability_modifier,
    )
    uncertainty_adjusted_edge = conservative_prob - raw_break_even
    model_converged = _model_converged(prediction)

    reason_code = "CANDIDATE_ROBUST_EDGE"
    status = CANDIDATE_STATUS
    if config.max_fair_odds is not None and fair_odds > config.max_fair_odds:
        reason_code = "PASS_FAIR_ODDS_CAP"
        status = "PASS"
    elif raw_edge < config.edge_threshold:
        reason_code = "PASS_LOW_EDGE"
        status = "PASS"
    elif data_quality is not None and data_quality < config.min_data_quality:
        reason_code = "PASS_LOW_DATA_QUALITY"
        status = "PASS"
    elif uncertainty is not None and uncertainty > config.max_uncertainty:
        reason_code = "PASS_HIGH_UNCERTAINTY"
        status = "PASS"
    elif config.require_model_convergence and model_converged is False:
        reason_code = "PASS_MODEL_UNSTABLE"
        status = "PASS"
    elif reliability_modifier <= -0.02:
        reason_code = "PASS_WEAK_LEAGUE_RELIABILITY"
        status = "PASS"
    elif uncertainty_adjusted_edge < config.min_uncertainty_adjusted_edge:
        reason_code = "WATCH_PRICE_NOT_STRONG_ENOUGH"
        status = "WATCH"

    return SelectionDecision(
        selection=selection,
        available_price=price,
        fair_probability=fair_prob,
        fair_odds=fair_odds,
        raw_break_even_probability=raw_break_even,
        raw_edge=raw_edge,
        devig_market_probability=devig_probability,
        model_vs_devig_market=model_vs_devig,
        expected_value=expected_value,
        conservative_probability=conservative_prob,
        uncertainty_adjusted_edge=uncertainty_adjusted_edge,
        league_reliability_modifier=reliability_modifier,
        reason_code=reason_code,
        status=status,
    )


def _decision_as_candidate(decision: SelectionDecision) -> dict[str, Any]:
    return {
        "selection": decision.selection,
        "available_price": decision.available_price,
        "fair_probability": decision.fair_probability,
        "fair_odds": decision.fair_odds,
        "edge": decision.raw_edge,
        "raw_break_even_probability": decision.raw_break_even_probability,
        "devig_market_probability": decision.devig_market_probability,
        "model_vs_devig_market": decision.model_vs_devig_market,
        "expected_value": decision.expected_value,
        "conservative_probability": decision.conservative_probability,
        "uncertainty_adjusted_edge": decision.uncertainty_adjusted_edge,
        "league_reliability_modifier": decision.league_reliability_modifier,
        "reason_code": decision.reason_code,
    }


def _parse_utc_timestamp(raw_value: str | None) -> datetime | None:
    text = (raw_value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _fetch_csv_rows(
    season_code: str,
    division_code: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
) -> tuple[str, list[dict[str, str]]]:
    source_url = build_csv_url(season_code, division_code)
    close_client = False
    if client is None:
        client = httpx.Client(follow_redirects=True, timeout=timeout)
        close_client = True
    try:
        response = client.get(source_url)
        response.raise_for_status()
    finally:
        if close_client:
            client.close()
    text = response.text.lstrip("\ufeff")
    return source_url, list(csv.DictReader(io.StringIO(text)))


def _fixture_key(
    *,
    division_code: str,
    match_date: str,
    home_team: str,
    away_team: str,
) -> tuple[str, str, str, str]:
    return (
        division_code,
        match_date,
        home_team.strip().lower(),
        away_team.strip().lower(),
    )


def _fixture_key_from_fixture(fixture: UpcomingFixture) -> tuple[str, str, str, str]:
    return _fixture_key(
        division_code=fixture.division_code,
        match_date=fixture.match_date,
        home_team=fixture.home_team,
        away_team=fixture.away_team,
    )


def _load_api_football_league_map(path: str | Path | None) -> dict[str, int] | None:
    if path is None:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("API-Football league map must be a JSON object")
    return {str(key): int(value) for key, value in payload.items()}


def _fetch_stored_history(
    conn: sqlite3.Connection,
    *,
    competition_id: str,
    before_date: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT event_id, competition_id, competition_name, season_code, match_date,
               home_team, away_team, home_goals, away_goals, result_code
        FROM football_events
        WHERE competition_id = ?
          AND match_date < ?
          AND home_goals IS NOT NULL
          AND away_goals IS NOT NULL
        ORDER BY match_date, event_id
        """,
        (competition_id, before_date),
    ).fetchall()
    return [dict(row) for row in rows]


def _fit_prediction(
    config: FootballWatchlistConfig,
    history_rows: list[dict[str, Any]],
    *,
    event_id: str,
    home_team: str,
    away_team: str,
) -> dict[str, Any] | None:
    if config.model_name == "poisson_1x2_v1":
        return fit_poisson_1x2_from_history(
            history_rows,
            event_id=event_id,
            home_team=home_team,
            away_team=away_team,
            min_team_matches=config.min_team_matches,
            prior_weight=config.prior_weight,
        )
    if config.model_name == "poisson_1x2_v3":
        return fit_poisson_1x2_v3_from_history(
            history_rows,
            event_id=event_id,
            home_team=home_team,
            away_team=away_team,
            min_team_matches=config.min_team_matches,
            prior_weight=config.prior_weight,
            strength_regularization=config.strength_regularization,
        )
    raise ValueError(
        f"unsupported watchlist model_name {config.model_name!r}; expected one of {SUPPORTED_MODEL_NAMES}"
    )


def _completed_rows_from_current_csv(rows: list[dict[str, str]], *, before_date: str) -> list[dict[str, Any]]:
    completed: list[dict[str, Any]] = []
    for row in rows:
        result_code = (row.get("FTR") or "").strip()
        match_date = _normalize_date(row.get("Date", ""))
        if not result_code or not match_date or match_date >= before_date:
            continue
        home_goals = _safe_float(row.get("FTHG"))
        away_goals = _safe_float(row.get("FTAG"))
        if home_goals is None or away_goals is None:
            continue
        home_team = (row.get("HomeTeam") or "").strip()
        away_team = (row.get("AwayTeam") or "").strip()
        completed.append(
            {
                "event_id": _build_event_id(match_date, home_team, away_team),
                "match_date": match_date,
                "home_team": home_team,
                "away_team": away_team,
                "home_goals": int(home_goals),
                "away_goals": int(away_goals),
                "result_code": result_code,
            }
        )
    return completed


def _upcoming_rows_from_current_csv(rows: list[dict[str, str]], *, target_date: str) -> list[dict[str, str]]:
    upcoming: list[dict[str, str]] = []
    for row in rows:
        match_date = _normalize_date(row.get("Date", ""))
        result_code = (row.get("FTR") or "").strip()
        if match_date == target_date and not result_code:
            upcoming.append(row)
    return upcoming


def _build_csv_market_index(
    rows: list[dict[str, str]],
    *,
    division_code: str,
    target_date: str,
    bookmaker: str,
    source_ref: str,
) -> dict[tuple[str, str, str, str], MarketInput]:
    index: dict[tuple[str, str, str, str], MarketInput] = {}
    captured_at = _utc_now()
    for row in _upcoming_rows_from_current_csv(rows, target_date=target_date):
        prices = {
            selection: value
            for selection in SELECTION_ORDER
            if (value := _safe_float(row.get(CURRENT_ODDS_COLUMNS[selection][0]) or row.get(CURRENT_ODDS_COLUMNS[selection][1])))
            is not None
            and value > 1
        }
        if not prices:
            continue
        key = _fixture_key(
            division_code=division_code,
            match_date=target_date,
            home_team=(row.get("HomeTeam") or ""),
            away_team=(row.get("AwayTeam") or ""),
        )
        index[key] = MarketInput(
            bookmaker=bookmaker,
            captured_at=captured_at,
            source_kind="football-data-co-uk-csv",
            source_ref=source_ref,
            price_source_tier="PRIMARY_MATCH_FILE_LISTING",
            prices=prices,
        )
    return index


def _read_manual_rows(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(target)
    text = target.read_text(encoding="utf-8-sig")
    if target.suffix.lower() == ".json":
        payload = json.loads(text)
        if isinstance(payload, dict):
            if "rows" in payload and isinstance(payload["rows"], list):
                return list(payload["rows"])
            if "prices" in payload and isinstance(payload["prices"], list):
                return list(payload["prices"])
            raise ValueError("manual market JSON must contain rows or prices")
        if isinstance(payload, list):
            return payload
        raise ValueError("manual market JSON must be a list or contain rows")
    return list(csv.DictReader(io.StringIO(text)))


def _build_manual_market_index(
    file_path: str | Path | None,
    *,
    target_date: str,
    bookmaker: str,
) -> dict[tuple[str, str, str, str], MarketInput]:
    if file_path is None:
        return {}
    rows = _read_manual_rows(file_path)
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        match_date = str(row.get("match_date") or row.get("date") or "").strip()
        if match_date != target_date:
            continue
        division_code = str(row.get("division_code") or row.get("competition") or "").strip()
        home_team = str(row.get("home_team") or row.get("HomeTeam") or "").strip()
        away_team = str(row.get("away_team") or row.get("AwayTeam") or "").strip()
        if not division_code or not home_team or not away_team:
            continue
        row_bookmaker = str(row.get("bookmaker") or bookmaker).strip() or bookmaker
        key = _fixture_key(
            division_code=division_code,
            match_date=match_date,
            home_team=home_team,
            away_team=away_team,
        )
        bucket = grouped.setdefault(
            key,
            {
                "bookmaker": row_bookmaker,
                "captured_at": str(row.get("captured_at") or _utc_now()),
                "source_kind": str(row.get("source_kind") or "manual-market-import"),
                "source_ref": str(row.get("source_ref") or Path(file_path)),
                "price_source_tier": str(
                    row.get("price_source_tier")
                    or (
                        "SECONDARY_BOOKMAKER_LISTING"
                        if str(row.get("source_ref") or "").strip().startswith("http")
                        else "MANUAL_OPERATOR_ENTRY"
                    )
                ),
                "prices": {},
            },
        )
        if "selection" in row and "decimal_odds" in row:
            selection = str(row.get("selection") or "").strip()
            odds = _safe_float(str(row.get("decimal_odds") or ""))
            if selection in SELECTION_ORDER and odds is not None and odds > 1:
                bucket["prices"][selection] = odds
        else:
            for selection in SELECTION_ORDER:
                raw_value = row.get(f"{selection}_price") or row.get(selection)
                odds = _safe_float(str(raw_value or ""))
                if odds is not None and odds > 1:
                    bucket["prices"][selection] = odds

    return {
        key: MarketInput(
            bookmaker=value["bookmaker"],
            captured_at=value["captured_at"],
            source_kind=value["source_kind"],
            source_ref=value["source_ref"],
            price_source_tier=value["price_source_tier"],
            prices=dict(value["prices"]),
        )
        for key, value in grouped.items()
        if value["prices"]
    }


def _provider_priority(source: str) -> tuple[str, ...]:
    if source == "api-football":
        return ("api-football",)
    if source == "football-data-org":
        return ("football-data-org",)
    if source == "csv":
        return ("csv",)
    if source == "manual":
        return ("manual",)
    return ("api-football", "football-data-org", "csv", "manual")


def generate_football_watchlist(
    config: FootballWatchlistConfig,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Generate a blind watchlist for a target date."""

    run_id = f"run_watchlist_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    cards: list[dict[str, Any]] = []
    logged_ids: list[str] = []
    funnel = {
        "provider_calls": 0,
        "provider_failures": 0,
        "fixtures_returned_raw": 0,
        "fixtures_discovered": 0,
        "blind_prediction_possible": 0,
        "insufficient_history_fixtures": 0,
        "market_prices_available": 0,
        "market_prices_missing": 0,
        "market_snapshots_rejected_late": 0,
        "selections_considered": 0,
        "selections_missing_price": 0,
        "selections_failed_fair_odds_cap": 0,
        "selections_failed_edge_threshold": 0,
        "selections_failed_data_quality": 0,
        "selections_failed_uncertainty": 0,
        "selections_failed_model_stability": 0,
        "selections_failed_reliability": 0,
        "selections_failed_uncertainty_adjusted_edge": 0,
        "selections_cleared_edge_threshold": 0,
        "selections_cleared_robust_gate": 0,
        "risk_gate_rejected": 0,
        "final_paper_shadow_candidates": 0,
    }

    provider_diagnostics: list[dict[str, Any]] = []
    manual_market_index = _build_manual_market_index(
        config.manual_market_path,
        target_date=config.target_date,
        bookmaker=config.bookmaker,
    )

    api_football_league_map = _load_api_football_league_map(config.api_football_league_map_path)
    fixtures, provider_diagnostics = discover_upcoming_fixtures(
        season_code=config.season_code,
        division_codes=config.division_codes,
        target_date=config.target_date,
        provider_priority=_provider_priority(config.fixture_source),
        manual_fixtures_path=config.manual_fixtures_path,
        football_data_token=config.football_data_token,
        api_football_token=config.api_football_token,
        api_football_league_map=api_football_league_map,
        client=client,
    )
    funnel["provider_calls"] = len(provider_diagnostics)
    funnel["provider_failures"] = sum(entry["status"] == "FAILED" for entry in provider_diagnostics)
    funnel["fixtures_returned_raw"] = sum(entry["fixtures_returned"] for entry in provider_diagnostics if entry["status"] == "OK")
    funnel["fixtures_discovered"] = len(fixtures)

    with _connect_research_store(config.research_db_path) as research_conn:
        ledger_conn = connect_paper_shadow_ledger(config.ledger_db_path) if config.ledger_db_path else None
        try:
            csv_history_rows: dict[str, list[dict[str, str]]] = {}
            csv_history_sources: dict[str, str] = {}
            for division_code in config.division_codes:
                try:
                    source_url, rows = _fetch_csv_rows(config.season_code, division_code, client=client)
                    csv_history_rows[division_code] = rows
                    csv_history_sources[division_code] = source_url
                except Exception:
                    csv_history_rows[division_code] = []
                    csv_history_sources[division_code] = build_csv_url(config.season_code, division_code)

            csv_market_index: dict[tuple[str, str, str, str], MarketInput] = {}
            for division_code, rows in csv_history_rows.items():
                csv_market_index.update(
                    _build_csv_market_index(
                        rows,
                        division_code=division_code,
                        target_date=config.target_date,
                        bookmaker=config.bookmaker,
                        source_ref=csv_history_sources[division_code],
                    )
                )

            for fixture in fixtures:
                competition_id, competition_name = _competition_identity(fixture.division_code)
                stored_history = _fetch_stored_history(
                    research_conn,
                    competition_id=competition_id,
                    before_date=config.target_date,
                )
                current_completed = _completed_rows_from_current_csv(
                    csv_history_rows.get(fixture.division_code, []),
                    before_date=config.target_date,
                )
                history_rows = stored_history + current_completed

                prediction = _fit_prediction(
                    config,
                    history_rows,
                    event_id=fixture.event_id,
                    home_team=fixture.home_team,
                    away_team=fixture.away_team,
                )
                if prediction is None:
                    funnel["insufficient_history_fixtures"] += 1
                    cards.append(
                        {
                            "event_id": fixture.event_id,
                            "competition_id": competition_id,
                            "competition_name": competition_name,
                            "division_code": fixture.division_code,
                            "match_date": fixture.match_date,
                            "kickoff_time": fixture.kickoff_time,
                            "kickoff_at_utc": fixture.kickoff_at_utc,
                            "home_team": fixture.home_team,
                            "away_team": fixture.away_team,
                            "status": "PASS",
                            "reason": "insufficient blind history for both teams",
                            "source_url": fixture.source_ref,
                            "source_kind": fixture.source_kind,
                        }
                    )
                    continue

                funnel["blind_prediction_possible"] += 1
                prediction_payload = {
                    "competition_id": competition_id,
                    "competition_name": competition_name,
                    "division_code": fixture.division_code,
                    "match_date": fixture.match_date,
                    "kickoff_time": fixture.kickoff_time,
                    "kickoff_at_utc": fixture.kickoff_at_utc,
                    "home_team": fixture.home_team,
                    "away_team": fixture.away_team,
                    "source_kind": fixture.source_kind,
                    "source_ref": fixture.source_ref,
                    "prediction": prediction,
                    "model_name": config.model_name,
                }
                prediction_lock = make_lock_row(
                    fixture.event_id,
                    run_id,
                    [prediction_payload],
                    prev_hash=None,
                )
                if ledger_conn is not None:
                    record_lock_row(
                        ledger_conn,
                        LockRow(
                            stage="prediction",
                            event_id=fixture.event_id,
                            run_id=run_id,
                            payload_json=json.dumps([prediction_payload], sort_keys=True, default=str),
                            payload_hash=prediction_lock["payload_hash"],
                            prev_hash=prediction_lock["prev_hash"],
                        ),
                    )

                fixture_key = _fixture_key_from_fixture(fixture)
                market_input = manual_market_index.get(fixture_key) or csv_market_index.get(fixture_key)
                kickoff_at_utc = _parse_utc_timestamp(fixture.kickoff_at_utc)
                market_captured_at = _parse_utc_timestamp(market_input.captured_at) if market_input is not None else None
                late_market_snapshot = (
                    market_input is not None
                    and kickoff_at_utc is not None
                    and market_captured_at is not None
                    and market_captured_at >= kickoff_at_utc
                )
                if market_input is not None:
                    funnel["market_prices_available"] += 1
                    if late_market_snapshot:
                        funnel["market_snapshots_rejected_late"] += 1
                else:
                    funnel["market_prices_missing"] += 1

                if market_input is not None:
                    for selection, price in market_input.prices.items():
                        if ledger_conn is not None:
                            record_market_snapshot(
                                ledger_conn,
                                MarketSnapshot(
                                    run_id=run_id,
                                    event_id=fixture.event_id,
                                    bookmaker=market_input.bookmaker,
                                    market="1X2",
                                    selection=selection,
                                    decimal_odds=price,
                                    source_kind=market_input.source_kind,
                                    source_ref=market_input.source_ref,
                                    captured_at=market_input.captured_at,
                                ),
                            )
                    market_payload = {
                        "captured_at": market_input.captured_at,
                        "bookmaker": market_input.bookmaker,
                        "market": "1X2",
                        "prices": market_input.prices,
                        "source_kind": market_input.source_kind,
                        "source_ref": market_input.source_ref,
                        "price_source_tier": market_input.price_source_tier,
                        "kickoff_at_utc": fixture.kickoff_at_utc,
                        "late_snapshot": late_market_snapshot,
                    }
                    current_market_report = _market_report(market_input.prices)
                    market_payload.update(current_market_report)
                else:
                    market_payload = {
                        "captured_at": None,
                        "bookmaker": config.bookmaker,
                        "market": "1X2",
                        "prices": {},
                        "source_kind": None,
                        "source_ref": None,
                        "price_source_tier": None,
                        "kickoff_at_utc": fixture.kickoff_at_utc,
                        "late_snapshot": False,
                        "raw_implied_probabilities": {},
                        "bookmaker_overround": None,
                        "bookmaker_margin": None,
                        "devig_probabilities": {},
                    }
                    current_market_report = {
                        "raw_implied_probabilities": {},
                        "bookmaker_overround": None,
                        "bookmaker_margin": None,
                        "devig_probabilities": {},
                    }

                market_lock = make_lock_row(
                    fixture.event_id,
                    run_id,
                    [market_payload],
                    prev_hash=prediction_lock["payload_hash"],
                )
                if ledger_conn is not None:
                    record_lock_row(
                        ledger_conn,
                        LockRow(
                            stage="market",
                            event_id=fixture.event_id,
                            run_id=run_id,
                            payload_json=json.dumps([market_payload], sort_keys=True, default=str),
                            payload_hash=market_lock["payload_hash"],
                            prev_hash=market_lock["prev_hash"],
                        ),
                    )

                best_candidate: dict[str, Any] | None = None
                selection_decisions: list[dict[str, Any]] = []
                final_reason_code = "PASS_LOW_EDGE"
                card_status = "PASS"
                for selection in config.allowed_selections:
                    funnel["selections_considered"] += 1
                    if market_input is None:
                        funnel["selections_missing_price"] += 1
                        continue
                    if late_market_snapshot:
                        final_reason_code = "PASS_LATE_MARKET_SNAPSHOT"
                        continue
                    price = market_input.prices.get(selection)
                    if price is None:
                        funnel["selections_missing_price"] += 1
                        continue
                    decision = _evaluate_selection_decision(
                        config=config,
                        competition_id=competition_id,
                        selection=selection,
                        price=price,
                        prediction=prediction,
                        market_report=current_market_report,
                    )
                    selection_decisions.append(_decision_as_candidate(decision))
                    if decision.reason_code == "PASS_FAIR_ODDS_CAP":
                        funnel["selections_failed_fair_odds_cap"] += 1
                        continue
                    if decision.reason_code == "PASS_LOW_EDGE":
                        funnel["selections_failed_edge_threshold"] += 1
                        continue
                    if decision.reason_code == "PASS_LOW_DATA_QUALITY":
                        funnel["selections_failed_data_quality"] += 1
                        continue
                    if decision.reason_code == "PASS_HIGH_UNCERTAINTY":
                        funnel["selections_failed_uncertainty"] += 1
                        continue
                    if decision.reason_code == "PASS_MODEL_UNSTABLE":
                        funnel["selections_failed_model_stability"] += 1
                        continue
                    if decision.reason_code == "PASS_WEAK_LEAGUE_RELIABILITY":
                        funnel["selections_failed_reliability"] += 1
                        continue
                    if decision.reason_code == "WATCH_PRICE_NOT_STRONG_ENOUGH":
                        funnel["selections_failed_uncertainty_adjusted_edge"] += 1
                        card_status = "WATCH"
                        final_reason_code = decision.reason_code
                        continue
                    funnel["selections_cleared_edge_threshold"] += 1
                    funnel["selections_cleared_robust_gate"] += 1
                    candidate = _decision_as_candidate(decision)
                    if best_candidate is None or candidate["edge"] > best_candidate["edge"]:
                        best_candidate = candidate
                        final_reason_code = decision.reason_code
                        card_status = CANDIDATE_STATUS

                if best_candidate is None and selection_decisions and card_status != "WATCH":
                    ranked_decisions = sorted(
                        selection_decisions,
                        key=lambda item: item.get("raw_edge") if item.get("raw_edge") is not None else -999,
                        reverse=True,
                    )
                    final_reason_code = ranked_decisions[0]["reason_code"]

                card = {
                    "event_id": fixture.event_id,
                    "competition_id": competition_id,
                    "competition_name": competition_name,
                    "division_code": fixture.division_code,
                    "match_date": fixture.match_date,
                    "kickoff_time": fixture.kickoff_time,
                    "kickoff_at_utc": fixture.kickoff_at_utc,
                    "home_team": fixture.home_team,
                    "away_team": fixture.away_team,
                    "status": (
                        card_status
                        if best_candidate
                        else "WATCH" if market_input is None else card_status
                    ),
                    "reason": (
                        f"{final_reason_code}: robust edge {best_candidate['uncertainty_adjusted_edge']:+.2%} on {best_candidate['selection']}"
                        if best_candidate
                        else (
                            "fixture found, waiting for market price from bookmaker source"
                            if market_input is None
                            else (
                                "market snapshot captured at/after kickoff; invalid for pre-match decision"
                                if late_market_snapshot
                                else final_reason_code
                            )
                        )
                    ),
                    "reason_code": (
                        "WATCH_MISSING_MARKET_DATA"
                        if market_input is None
                        else "PASS_LATE_MARKET_SNAPSHOT"
                        if late_market_snapshot
                        else final_reason_code
                    ),
                    "prediction": prediction,
                    "candidate": best_candidate,
                    "selection_decisions": selection_decisions,
                    "market_report": current_market_report,
                    "source_url": fixture.source_ref,
                    "source_kind": fixture.source_kind,
                    "market_source_kind": market_input.source_kind if market_input else None,
                    "market_source_ref": market_input.source_ref if market_input else None,
                    "market_price_source_tier": market_input.price_source_tier if market_input else None,
                    "market_captured_at": market_input.captured_at if market_input else None,
                    "late_market_snapshot": late_market_snapshot,
                    "prediction_lock_hash": prediction_lock["payload_hash"],
                    "market_lock_hash": market_lock["payload_hash"],
                }
                cards.append(card)

                if best_candidate:
                    funnel["final_paper_shadow_candidates"] += 1
                    if ledger_conn is not None:
                        decision = PaperDecision(
                            run_id=run_id,
                            event_id=fixture.event_id,
                            sport="football",
                            competition=competition_id,
                            market="1X2",
                            selection=f"{best_candidate['selection']}_{fixture.home_team}_{fixture.away_team}",
                            available_price=best_candidate["available_price"],
                            bookmaker=market_input.bookmaker if market_input else config.bookmaker,
                            fair_probability=best_candidate["fair_probability"],
                            fair_odds=best_candidate["fair_odds"],
                            market_probability=1.0 / best_candidate["available_price"],
                            edge=best_candidate["edge"],
                            data_quality=prediction["data_quality"],
                            uncertainty=prediction["uncertainty"],
                            decision=CANDIDATE_STATUS,
                            decision_reason=(
                                f"{best_candidate['reason_code']}: blind Poisson watchlist candidate at "
                                f"{best_candidate['available_price']:.2f} with raw edge {best_candidate['edge']:+.2%} "
                                f"and robust edge {best_candidate['uncertainty_adjusted_edge']:+.2%}"
                            ),
                            model_version=config.model_name,
                            strategy_version="football_watchlist_v1",
                            policy_version="football_watchlist_gate_v1",
                            notes=(
                                f"target_date={config.target_date}; division={fixture.division_code}; "
                                f"model_name={config.model_name}; "
                                f"fixture_source_url={fixture.source_ref}; fixture_source_kind={fixture.source_kind}; "
                                f"market_source_url={(market_input.source_ref if market_input else '')}; "
                                f"market_source_kind={(market_input.source_kind if market_input else '')}; "
                                f"price_source_tier={(market_input.price_source_tier if market_input else '')}; "
                                f"kickoff_at_utc={(fixture.kickoff_at_utc or '')}; "
                                f"prediction_lock={prediction_lock['payload_hash']}; "
                                f"market_lock={market_lock['payload_hash']}; "
                                f"captured_at={(market_input.captured_at if market_input else '')}"
                                f"; reason_code={best_candidate['reason_code']}; "
                                f"raw_break_even_probability={best_candidate['raw_break_even_probability']:.6f}; "
                                f"devig_market_probability={(best_candidate['devig_market_probability'] if best_candidate['devig_market_probability'] is not None else '')}; "
                                f"model_vs_devig_market={(best_candidate['model_vs_devig_market'] if best_candidate['model_vs_devig_market'] is not None else '')}; "
                                f"expected_value={best_candidate['expected_value']:.6f}; "
                                f"conservative_probability={best_candidate['conservative_probability']:.6f}; "
                                f"uncertainty_adjusted_edge={best_candidate['uncertainty_adjusted_edge']:.6f}"
                            ),
                        )
                        logged_ids.append(record_decision(ledger_conn, decision))
        finally:
            if ledger_conn is not None:
                ledger_conn.close()

    cards.sort(key=lambda card: (card["status"] != CANDIDATE_STATUS, card["competition_id"], card["home_team"]))
    return {
        "run_id": run_id,
        "model_name": config.model_name,
        "season_code": config.season_code,
        "division_codes": list(config.division_codes),
        "target_date": config.target_date,
        "card_count": len(cards),
        "bet_candidate_count": sum(card["status"] == CANDIDATE_STATUS for card in cards),
        "provider_diagnostics": provider_diagnostics,
        "funnel": funnel,
        "cards": cards,
        "logged_decision_ids": logged_ids,
    }


def default_tomorrow_iso() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate a blind football watchlist for upcoming fixtures.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument("--season", required=True, help="Season code like 2627.")
    parser.add_argument(
        "--division",
        action="append",
        dest="divisions",
        required=True,
        help="Division code like E0 or SP1. Repeat for multiple target leagues.",
    )
    parser.add_argument("--target-date", default=default_tomorrow_iso(), help="Target fixture date in YYYY-MM-DD.")
    parser.add_argument("--bookmaker", default="Bet365", help="Bookmaker price to compare against.")
    parser.add_argument(
        "--model",
        default="poisson_1x2_v3",
        choices=SUPPORTED_MODEL_NAMES,
        help="Blind model to run in the live watchlist.",
    )
    parser.add_argument("--min-team-matches", type=int, default=6, help="Minimum prior matches per team.")
    parser.add_argument("--prior-weight", type=float, default=5.0, help="Shrinkage weight toward league average.")
    parser.add_argument(
        "--strength-regularization",
        type=float,
        default=0.03,
        help="Opponent-strength shrinkage for the promoted v3 live watchlist model.",
    )
    parser.add_argument("--edge-threshold", type=float, default=0.03, help="Minimum blind edge for a candidate.")
    parser.add_argument("--min-data-quality", type=float, default=0.82, help="Minimum data quality for a robust candidate.")
    parser.add_argument("--max-uncertainty", type=float, default=0.98, help="Maximum uncertainty for a robust candidate.")
    parser.add_argument(
        "--min-uncertainty-adjusted-edge",
        type=float,
        default=0.015,
        help="Minimum conservative probability edge over market break-even.",
    )
    parser.add_argument(
        "--allow-nonconverged-model",
        action="store_true",
        help="Allow non-converged model fits through the robust candidate gate.",
    )
    parser.add_argument(
        "--selection",
        action="append",
        dest="selections",
        help="Allowed selection key like home, draw, away. Repeat to narrow picks.",
    )
    parser.add_argument("--max-fair-odds", type=float, default=2.8, help="Cap on fair odds to avoid longshots.")
    parser.add_argument("--ledger-db", help="Optional ledger path to record paper/shadow candidate rows.")
    parser.add_argument(
        "--fixture-source",
        default="auto",
        choices=("auto", "api-football", "football-data-org", "csv", "manual"),
        help="Fixture source strategy with provider priority or one forced source.",
    )
    parser.add_argument("--football-data-token", help="Optional football-data.org API token override.")
    parser.add_argument("--api-football-token", help="Optional API-Football token override.")
    parser.add_argument("--api-football-league-map", help="Optional JSON file mapping division codes to API-Football league ids.")
    parser.add_argument("--manual-fixtures", help="Optional CSV or JSON manual fixture import path.")
    parser.add_argument("--manual-market", help="Optional CSV or JSON manual market import path.")
    args = parser.parse_args(argv)

    result = generate_football_watchlist(
        FootballWatchlistConfig(
            research_db_path=args.research_db,
            season_code=args.season,
            division_codes=tuple(args.divisions),
            target_date=args.target_date,
            bookmaker=args.bookmaker,
            model_name=args.model,
            min_team_matches=args.min_team_matches,
            prior_weight=args.prior_weight,
            strength_regularization=args.strength_regularization,
            edge_threshold=args.edge_threshold,
            min_data_quality=args.min_data_quality,
            max_uncertainty=args.max_uncertainty,
            min_uncertainty_adjusted_edge=args.min_uncertainty_adjusted_edge,
            require_model_convergence=not args.allow_nonconverged_model,
            allowed_selections=tuple(args.selections) if args.selections else SELECTION_ORDER,
            max_fair_odds=args.max_fair_odds,
            ledger_db_path=args.ledger_db,
            fixture_source=args.fixture_source,
            football_data_token=args.football_data_token,
            api_football_token=args.api_football_token,
            api_football_league_map_path=args.api_football_league_map,
            manual_fixtures_path=args.manual_fixtures,
            manual_market_path=args.manual_market,
        )
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
