"""High-confidence Asian Handicap SHADOW lane (forward-only, accuracy tracking).

For each upcoming fixture, this lane takes the frozen pre-kickoff READY
prediction, the same one a Model Pick would use, and selects the AH catalog line
with the longest fair price whose push-excluded win probability q = 1/fair is at
least the threshold (0.70). It records:
- the line, q, fair price and full model provenance;
- separately, any fresh, value-eligible bookmaker price observed for that exact
  line;
- an explicit abstention when evidence is insufficient (zero picks is a valid
  outcome).

Isolation guarantees:
- separate SQLite file, separate module;
- reads the dashboard and paper DBs read-only;
- never writes the Model Pick ledger and never imports the Model Pick policy;
- creates no Value Bets, paper positions or wagers.

Evidence (docs/FOOTBALL_MODEL_ACCURACY_AUDIT.md §9): on the holdout the lane wins
about 75% at fair about 1.38, and a model-free market lane matches it. The
market's probability is better calibrated on the same bets. Nothing here is a
value claim. Recorded offered prices are observations, not accepted bets.

AH q is used uncalibrated: holdout AH ECE was 0.011 (``CALIBRATION_ID``).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..locking import canonical_hash
from ..market_data.market_evidence import resolve_actionable_max_age_minutes
from ..storage.football_canonical_fixture import canonical_fixture_id_from_identity
from .frozen_value_evaluation import snapshot_is_value_eligible
from .model_pick_settlement import build_settlement_plan

POLICY_ID = "football_high_confidence_ah_shadow_v1"
POLICY_VERSION = "v1"
THRESHOLD = 0.70
MIN_VENUE_HISTORY_MATCHES = 2
CALIBRATION_ID = "identity_ah_q_holdout_ece_0.011_2026-10-13"
MARKET_AH = "asian_handicap"
ENABLE_ENV_VAR = "SPORTS_AI_ENABLE_HIGH_CONFIDENCE_SHADOW"
DB_PATH_ENV_VAR = "SPORTS_AI_HIGH_CONFIDENCE_SHADOW_DB_PATH"
DEFAULT_DB_FILENAME = "football_high_confidence_shadow.sqlite"

DECISION_ISSUED = "ISSUED"
ABSTAIN_LATEST_NOT_READY = "LATEST_PREDICTION_NOT_READY"
ABSTAIN_NO_PRE_KICKOFF_READY = "NO_PRE_KICKOFF_READY_PREDICTION"
ABSTAIN_MODEL_STATE_NOT_BEFORE_KICKOFF = "MODEL_STATE_NOT_BEFORE_KICKOFF"
ABSTAIN_IDENTITY = "TEAM_IDENTITY_UNRESOLVED"
ABSTAIN_HISTORY = "INSUFFICIENT_VENUE_HISTORY"
ABSTAIN_NO_LINE = "NO_LINE_AT_THRESHOLD"
ABSTAIN_DUPLICATE = "CANONICAL_DUPLICATE"
ABSTAIN_BLOCKERS = "PREDICTION_BLOCKERS_PRESENT"

_NOT_UPCOMING = {"FINISHED", "CANCELLED", "POSTPONED", "ABANDONED", "LIVE", "HALFTIME", "IN_PLAY", "PAUSED"}
_FINAL_STAKE = {"FULL_WIN": (1.0, 0.0), "HALF_WIN": (0.5, 0.0), "PUSH": (0.0, 0.0), "HALF_LOSS": (0.0, 0.5), "FULL_LOSS": (0.0, 1.0)}

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS hc_shadow_entries (
        shadow_pick_id TEXT PRIMARY KEY,
        policy_id TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        threshold REAL NOT NULL,
        calibration_id TEXT NOT NULL,
        fixture_id TEXT NOT NULL,
        canonical_fixture_id TEXT NOT NULL,
        competition_id TEXT,
        home_team_name TEXT,
        away_team_name TEXT,
        kickoff_utc TEXT NOT NULL,
        market TEXT NOT NULL,
        line REAL NOT NULL,
        selection TEXT NOT NULL,
        win_probability_ex_push REAL NOT NULL,
        unconditional_win_stake_probability REAL,
        model_fair_price REAL NOT NULL,
        prediction_id TEXT NOT NULL,
        prediction_generated_at TEXT NOT NULL,
        model_snapshot_id TEXT,
        model_version TEXT,
        model_as_of TEXT,
        model_versions_json TEXT,
        home_venue_matches INTEGER,
        away_venue_matches INTEGER,
        issued_at TEXT NOT NULL,
        offered_price REAL,
        offered_bookmaker TEXT,
        offered_provider TEXT,
        offered_captured_at TEXT,
        offered_ingestion_version TEXT,
        offered_book_count INTEGER,
        trade_state TEXT,
        settlement_payload_json TEXT,
        settled_at TEXT,
        UNIQUE (policy_id, canonical_fixture_id, market)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hc_shadow_decisions (
        policy_id TEXT NOT NULL,
        canonical_fixture_id TEXT NOT NULL,
        fixture_id TEXT NOT NULL,
        kickoff_utc TEXT NOT NULL,
        decision TEXT NOT NULL,
        reason TEXT,
        evaluated_at TEXT NOT NULL,
        PRIMARY KEY (policy_id, canonical_fixture_id)
    )
    """,
    # Issued picks are immutable; only settlement fields may be filled once.
    """
    CREATE TRIGGER IF NOT EXISTS hc_shadow_entries_immutable
    BEFORE UPDATE ON hc_shadow_entries
    WHEN NEW.line IS NOT OLD.line OR NEW.selection IS NOT OLD.selection
      OR NEW.win_probability_ex_push IS NOT OLD.win_probability_ex_push
      OR NEW.model_fair_price IS NOT OLD.model_fair_price OR NEW.prediction_id IS NOT OLD.prediction_id
      OR NEW.issued_at IS NOT OLD.issued_at OR NEW.kickoff_utc IS NOT OLD.kickoff_utc
      OR NEW.offered_price IS NOT OLD.offered_price OR NEW.fixture_id IS NOT OLD.fixture_id
      OR (OLD.trade_state IS NOT NULL AND NEW.trade_state IS NOT OLD.trade_state)
    BEGIN
        SELECT RAISE(ABORT, 'HC_SHADOW_IMMUTABLE');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS hc_shadow_entries_no_delete
    BEFORE DELETE ON hc_shadow_entries
    BEGIN
        SELECT RAISE(ABORT, 'HC_SHADOW_IMMUTABLE');
    END
    """,
    # A decision becomes final once ISSUED.
    """
    CREATE TRIGGER IF NOT EXISTS hc_shadow_decisions_final
    BEFORE UPDATE ON hc_shadow_decisions
    WHEN OLD.decision = 'ISSUED'
    BEGIN
        SELECT RAISE(ABORT, 'HC_SHADOW_DECISION_FINAL');
    END
    """,
)


def enabled() -> bool:
    return str(os.environ.get(ENABLE_ENV_VAR, "")).strip().lower() in {"1", "true", "yes", "on"}


def default_db_path(model_pick_db_path: str | Path) -> Path:
    override = os.environ.get(DB_PATH_ENV_VAR)
    return Path(override) if override else Path(model_pick_db_path).with_name(DEFAULT_DB_FILENAME)


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    for statement in _SCHEMA:
        conn.execute(statement)
    conn.commit()
    return conn


def _parse(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ selection
def select_high_confidence_line(market_rows: Sequence[Mapping[str, Any]], threshold: float = THRESHOLD) -> dict[str, Any] | None:
    """AH catalog row with the longest fair price among q = 1/fair >= threshold."""
    best = None
    for row in market_rows:
        if row.get("market_family") != MARKET_AH:
            continue
        try:
            fair = float(row["fair_decimal_price"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(fair) or fair <= 1.0:
            continue
        q = 1.0 / fair
        if q < threshold:
            continue
        key = (fair, -abs(float(row.get("line") or 0.0)))
        if best is None or key > best[0]:
            best = (key, {"line": float(row["line"]), "selection": str(row["selection"]), "q": q, "fair": fair,
                          "unconditional": row.get("effective_win_stake_probability")})
    return None if best is None else best[1]


def _venue_matches(history: Mapping[str, Any] | None) -> int | None:
    if not history or not history.get("available"):
        return None
    counts = history.get("matches_observed_by_statistic") or {}
    return min((int(v) for v in counts.values()), default=None)


def frozen_prediction(predictions: Sequence[Mapping[str, Any]], *, kickoff: datetime) -> tuple[Mapping[str, Any] | None, str | None]:
    """First pre-kickoff READY prediction, valid only while the latest one is READY too."""
    ordered = sorted(predictions, key=lambda p: (str(p.get("generated_at") or ""), str(p.get("prediction_id") or "")))
    if not ordered:
        return None, ABSTAIN_NO_PRE_KICKOFF_READY
    if str(ordered[-1].get("status")) != "READY":
        return None, ABSTAIN_LATEST_NOT_READY
    for p in ordered:
        generated = _parse(p.get("generated_at"))
        if str(p.get("status")) == "READY" and generated is not None and generated < kickoff:
            return p, None
    return None, ABSTAIN_NO_PRE_KICKOFF_READY


def _offered_price(paper_conn: sqlite3.Connection | None, *, fixture_id: str, line: float, selection: str,
                   kickoff: datetime, now: datetime) -> dict[str, Any] | None:
    if paper_conn is None:
        return None
    try:
        rows = paper_conn.execute(
            "SELECT provider, bookmaker, market, selection, line, odds, captured_at, provider_timestamp, "
            "is_pre_match, ingestion_version FROM market_snapshots "
            "WHERE fixture_id = ? AND market = ? AND selection = ? AND abs(line - ?) < 1e-9",
            (fixture_id, MARKET_AH, selection, line),
        ).fetchall()
    except sqlite3.Error:
        return None
    eligible = []
    for r in rows:
        r = dict(r)
        captured = _parse(r.get("captured_at"))
        source = _parse(r.get("provider_timestamp")) or captured
        max_age = resolve_actionable_max_age_minutes(r.get("provider"))
        if not r.get("is_pre_match") or captured is None or captured >= kickoff or source is None or source >= kickoff:
            continue
        if max_age is None or (now - source).total_seconds() > max_age * 60:
            continue
        if not snapshot_is_value_eligible(r):
            continue
        try:
            odds = float(r["odds"])
        except (TypeError, ValueError):
            continue
        if odds > 1.0:
            eligible.append({**r, "odds": odds})
    if not eligible:
        return None
    best = max(eligible, key=lambda r: r["odds"])
    return {
        "offered_price": best["odds"],
        "offered_bookmaker": best["bookmaker"],
        "offered_provider": best["provider"],
        "offered_captured_at": best["captured_at"],
        "offered_ingestion_version": best["ingestion_version"],
        "offered_book_count": len({r["bookmaker"] for r in eligible}),
    }


def _record_decision(conn, *, canonical_id, fixture_id, kickoff_iso, decision, reason, now_iso):
    existing = conn.execute(
        "SELECT decision FROM hc_shadow_decisions WHERE policy_id = ? AND canonical_fixture_id = ?",
        (POLICY_ID, canonical_id),
    ).fetchone()
    if existing is not None and existing["decision"] == DECISION_ISSUED:
        return
    conn.execute(
        "INSERT INTO hc_shadow_decisions (policy_id, canonical_fixture_id, fixture_id, kickoff_utc, decision, reason, evaluated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(policy_id, canonical_fixture_id) DO UPDATE SET "
        "fixture_id = excluded.fixture_id, decision = excluded.decision, reason = excluded.reason, evaluated_at = excluded.evaluated_at",
        (POLICY_ID, canonical_id, fixture_id, kickoff_iso, decision, reason, now_iso),
    )


def issue_high_confidence_shadow(
    dashboard_conn: sqlite3.Connection,
    shadow_conn: sqlite3.Connection,
    paper_conn: sqlite3.Connection | None = None,
    *,
    now: str | datetime | None = None,
) -> dict[str, Any]:
    """Evaluate every upcoming fixture once per cycle; idempotent and forward-only."""
    now_dt = _parse(now) if isinstance(now, str) else (now or datetime.now(timezone.utc))
    now_iso = _iso(now_dt)
    dashboard_conn.row_factory = sqlite3.Row
    fixtures = [dict(r) for r in dashboard_conn.execute(
        "SELECT fixture_id, competition_id, home_team_name, away_team_name, kickoff_utc, status FROM dashboard_fixtures"
    )]
    preds: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in dashboard_conn.execute(
        "SELECT prediction_id, fixture_id, status, generated_at, model_as_of, model_snapshot_id, payload_json FROM dashboard_predictions"
    ):
        preds[str(r["fixture_id"])].append(dict(r))

    summary: dict[str, Any] = {"evaluated_at": now_iso, "issued": 0, "already_issued": 0, "abstained": defaultdict(int), "kickoff_passed_skipped": 0}
    seen_canonical: dict[str, str] = {}
    # Prefer API-Football ids when two providers describe the same match; never merge.
    for fx in sorted(fixtures, key=lambda f: (0 if str(f["fixture_id"]).startswith("api-football:") else 1, str(f["fixture_id"]))):
        kickoff = _parse(fx.get("kickoff_utc"))
        if kickoff is None or kickoff <= now_dt or str(fx.get("status") or "").upper() in _NOT_UPCOMING:
            summary["kickoff_passed_skipped"] += kickoff is not None and kickoff <= now_dt
            continue
        canonical_id = canonical_fixture_id_from_identity(
            competition_id=str(fx.get("competition_id") or ""), home_team_name=str(fx.get("home_team_name") or ""),
            away_team_name=str(fx.get("away_team_name") or ""), kickoff_utc=str(fx["kickoff_utc"]),
        )
        kickoff_iso = _iso(kickoff)
        if canonical_id in seen_canonical:
            summary["abstained"][ABSTAIN_DUPLICATE] += 1
            continue
        seen_canonical[canonical_id] = fx["fixture_id"]

        existing = shadow_conn.execute(
            "SELECT 1 FROM hc_shadow_entries WHERE policy_id = ? AND canonical_fixture_id = ? AND market = ?",
            (POLICY_ID, canonical_id, MARKET_AH),
        ).fetchone()
        if existing is not None:
            summary["already_issued"] += 1
            continue

        def abstain(reason: str) -> None:
            summary["abstained"][reason] += 1
            with shadow_conn:
                _record_decision(shadow_conn, canonical_id=canonical_id, fixture_id=fx["fixture_id"],
                                 kickoff_iso=kickoff_iso, decision="ABSTAINED", reason=reason, now_iso=now_iso)

        prediction, reason = frozen_prediction(preds.get(str(fx["fixture_id"]), []), kickoff=kickoff)
        if prediction is None:
            abstain(reason)
            continue
        model_as_of = str(prediction.get("model_as_of") or "")
        if not model_as_of or model_as_of[:10] >= kickoff_iso[:10]:
            abstain(ABSTAIN_MODEL_STATE_NOT_BEFORE_KICKOFF)
            continue
        try:
            payload = json.loads(prediction.get("payload_json") or "{}")
        except ValueError:
            payload = {}
        if payload.get("blockers"):
            abstain(ABSTAIN_BLOCKERS)
            continue
        resolution = payload.get("team_resolution") or {}
        if not all((resolution.get(side) or {}).get("canonical_team_id") for side in ("home", "away")):
            abstain(ABSTAIN_IDENTITY)
            continue
        history = payload.get("model_history") or {}
        home_n, away_n = _venue_matches(history.get("home")), _venue_matches(history.get("away"))
        if home_n is None or away_n is None or min(home_n, away_n) < MIN_VENUE_HISTORY_MATCHES:
            abstain(ABSTAIN_HISTORY)
            continue
        pick = select_high_confidence_line(payload.get("market_rows") or [])
        if pick is None:
            abstain(ABSTAIN_NO_LINE)
            continue
        offered = _offered_price(paper_conn, fixture_id=fx["fixture_id"], line=pick["line"], selection=pick["selection"],
                                 kickoff=kickoff, now=now_dt) or {}
        shadow_id = "hc_shadow_" + canonical_hash({"policy": POLICY_ID, "canonical_fixture_id": canonical_id, "market": MARKET_AH})
        versions = payload.get("model_versions") or {}
        try:
            with shadow_conn:
                shadow_conn.execute(
                    """
                    INSERT INTO hc_shadow_entries (
                        shadow_pick_id, policy_id, policy_version, threshold, calibration_id, fixture_id, canonical_fixture_id,
                        competition_id, home_team_name, away_team_name, kickoff_utc, market, line, selection,
                        win_probability_ex_push, unconditional_win_stake_probability, model_fair_price, prediction_id,
                        prediction_generated_at, model_snapshot_id, model_version, model_as_of, model_versions_json,
                        home_venue_matches, away_venue_matches, issued_at, offered_price, offered_bookmaker,
                        offered_provider, offered_captured_at, offered_ingestion_version, offered_book_count
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        shadow_id, POLICY_ID, POLICY_VERSION, THRESHOLD, CALIBRATION_ID, fx["fixture_id"], canonical_id,
                        fx.get("competition_id"), fx.get("home_team_name"), fx.get("away_team_name"), kickoff_iso, MARKET_AH,
                        pick["line"], pick["selection"], pick["q"], pick["unconditional"], pick["fair"],
                        prediction["prediction_id"], prediction["generated_at"], prediction.get("model_snapshot_id"),
                        versions.get("current_snapshot_model_version"), model_as_of, json.dumps(versions, sort_keys=True),
                        home_n, away_n, now_iso, offered.get("offered_price"), offered.get("offered_bookmaker"),
                        offered.get("offered_provider"), offered.get("offered_captured_at"),
                        offered.get("offered_ingestion_version"), offered.get("offered_book_count"),
                    ),
                )
                _record_decision(shadow_conn, canonical_id=canonical_id, fixture_id=fx["fixture_id"],
                                 kickoff_iso=kickoff_iso, decision=DECISION_ISSUED, reason=None, now_iso=now_iso)
            summary["issued"] += 1
        except sqlite3.IntegrityError:
            summary["already_issued"] += 1
    summary["abstained"] = dict(summary["abstained"])
    return summary


# ------------------------------------------------------------------ settlement
def settle_high_confidence_shadow(dashboard_conn: sqlite3.Connection, shadow_conn: sqlite3.Connection,
                                  *, now: str | datetime | None = None) -> dict[str, Any]:
    """Settle open shadow picks from verified final scores via the Model Pick settlement engine."""
    now_dt = _parse(now) if isinstance(now, str) else (now or datetime.now(timezone.utc))
    dashboard_conn.row_factory = sqlite3.Row
    open_rows = [dict(r) for r in shadow_conn.execute("SELECT * FROM hc_shadow_entries WHERE trade_state IS NULL")]
    picks = [{"model_pick_id": r["shadow_pick_id"], "fixture_id": r["fixture_id"], "market": r["market"],
              "selection": r["selection"], "line": r["line"]} for r in open_rows]
    fixtures = {str(r["fixture_id"]): dict(r) for r in dashboard_conn.execute("SELECT * FROM dashboard_fixtures")}
    plan = build_settlement_plan(open_picks=picks, fixtures=fixtures, results={}, now_utc=now_dt)
    applied = 0
    for action in plan.actions:
        with shadow_conn:
            cur = shadow_conn.execute(
                "UPDATE hc_shadow_entries SET trade_state = ?, settlement_payload_json = ?, settled_at = ? "
                "WHERE shadow_pick_id = ? AND trade_state IS NULL",
                (action.trade_state, json.dumps(action.settlement_payload, sort_keys=True, default=str), _iso(now_dt), action.model_pick_id),
            )
            applied += cur.rowcount
    return {"applied": applied, "examined": plan.examined, "ineligible": len(plan.ineligible)}


# ------------------------------------------------------------------ performance
def _wilson(p: float | None, n: float, z: float = 1.96) -> list[float | None]:
    if p is None or n <= 0:
        return [None, None]
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [max(0.0, c - h), min(1.0, c + h)]


def performance(shadow_conn: sqlite3.Connection) -> dict[str, Any]:
    rows = [dict(r) for r in shadow_conn.execute("SELECT * FROM hc_shadow_entries WHERE policy_id = ?", (POLICY_ID,))]
    decisions = [dict(r) for r in shadow_conn.execute("SELECT * FROM hc_shadow_decisions WHERE policy_id = ?", (POLICY_ID,))]
    settled = [r for r in rows if r["trade_state"] in _FINAL_STAKE]
    won = sum(_FINAL_STAKE[r["trade_state"]][0] for r in settled)
    lost = sum(_FINAL_STAKE[r["trade_state"]][1] for r in settled)
    share = won / (won + lost) if won + lost else None
    decisive = [r for r in settled if sum(_FINAL_STAKE[r["trade_state"]])]
    mean_q = sum(r["win_probability_ex_push"] for r in decisive) / len(decisive) if decisive else None
    offered = [r for r in rows if r["offered_price"]]
    abstain_reasons: dict[str, int] = defaultdict(int)
    for d in decisions:
        if d["decision"] != DECISION_ISSUED:
            abstain_reasons[str(d["reason"])] += 1
    evaluated = len(decisions)
    return {
        "policy_id": POLICY_ID,
        "threshold": THRESHOLD,
        "issued": len(rows),
        "settled": len(settled),
        "open": len(rows) - len(settled),
        "states": {s: sum(1 for r in settled if r["trade_state"] == s) for s in _FINAL_STAKE},
        "win_share_stake_weighted": share,
        "ci95": _wilson(share, won + lost),
        "mean_q_settled": mean_q,
        "calibration_gap": (share - mean_q) if share is not None and mean_q is not None else None,
        "mean_model_fair_price": sum(r["model_fair_price"] for r in rows) / len(rows) if rows else None,
        "evaluated_fixtures": evaluated,
        "coverage": len(rows) / evaluated if evaluated else None,
        "abstentions": dict(abstain_reasons),
        "offered_price_observed": len(offered),
        "mean_offered_price": sum(r["offered_price"] for r in offered) / len(offered) if offered else None,
        "mean_offered_over_fair": sum(r["offered_price"] / r["model_fair_price"] for r in offered) / len(offered) if offered else None,
        "value_claim": "NONE: offered prices are observations, not accepted bets; no EV is claimed",
        "insufficient_sample": (won + lost) < 300,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="High-confidence AH shadow lane (dry-run unless --apply).")
    parser.add_argument("--dashboard-db", type=Path, required=True)
    parser.add_argument("--paper-db", type=Path)
    parser.add_argument("--shadow-db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="write to --shadow-db (otherwise an in-memory copy)")
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    dash = sqlite3.connect(f"file:{args.dashboard_db.as_posix()}?mode=ro", uri=True)
    paper = sqlite3.connect(f"file:{args.paper_db.as_posix()}?mode=ro", uri=True) if args.paper_db else None
    if paper is not None:
        paper.row_factory = sqlite3.Row
    if args.apply:
        shadow = connect(args.shadow_db)
    else:
        shadow = connect(":memory:")
        if args.shadow_db.exists():
            src = sqlite3.connect(f"file:{args.shadow_db.as_posix()}?mode=ro", uri=True)
            src.backup(shadow)
            src.close()
    out = {
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "issue": issue_high_confidence_shadow(dash, shadow, paper, now=args.now),
        "settle": settle_high_confidence_shadow(dash, shadow, now=args.now),
        "performance": performance(shadow),
    }
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
