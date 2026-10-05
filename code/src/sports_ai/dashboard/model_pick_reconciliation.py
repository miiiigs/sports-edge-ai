"""Phase A reconciliation: build the LIVE_ISSUED model-pick ledger.

Reads persisted production evidence ONLY (no model rerun):

* ``dashboard_predictions`` READY artifacts (exact persisted ``market_rows``)
* ``dashboard_fixtures`` (identity + result) and ``dashboard_fixture_results``
  (corners)

and applies ``football_model_pick_balanced_line_v1`` to each first-issued,
pre-kickoff READY prediction. Missing days become ``REPLAY_PENDING``; no
CURRENT_SYSTEM_REPLAY rows are written in Phase A.

Terminology enforced here: ``NO_MARKET_EVIDENCE`` (value could not be evaluated)
is distinct from ``NO_BET`` (market existed and the Manager rejected it).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .model_pick_policy import POLICY_ID, POLICY_VERSION, select_model_picks

RECORD_ORIGIN_LIVE_ISSUED = "LIVE_ISSUED"
RECORD_ORIGIN_CURRENT_SYSTEM_REPLAY = "CURRENT_SYSTEM_REPLAY"

MARKET_EVIDENCE_AVAILABLE = "MARKET_EVIDENCE_AVAILABLE"
NO_MARKET_EVIDENCE = "NO_MARKET_EVIDENCE"

DAY_LIVE_ISSUED = "LIVE_ISSUED"
DAY_NO_FIXTURES = "NO_FIXTURES"
DAY_NO_LIVE_PREDICTION = "NO_LIVE_PREDICTION"
DAY_REPLAY_PENDING = "REPLAY_PENDING"

_FINISHED_STATUSES = frozenset({"FINISHED", "FT", "AET", "PEN", "FINISHED_AFTER_EXTRA_TIME", "FINISHED_AFTER_PENALTIES"})

_MODEL_VERSION_KEYS = ("current_snapshot_model_version", "pricing_model_version", "day10_model_market_pricing_version")


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
    return parsed


def _day(value: Any) -> str | None:
    parsed = _parse(value)
    return None if parsed is None else parsed.date().isoformat()


def _is_valid_pre_kickoff(row: Mapping[str, Any], kickoff: datetime) -> bool:
    if str(row.get("status") or "").upper() != "READY":
        return False
    generated = _parse(row.get("generated_at"))
    model_as_of = _parse(row.get("model_as_of"))
    if generated is None or model_as_of is None:
        return False
    if generated >= kickoff:
        return False
    return not (model_as_of > generated or model_as_of >= kickoff)


def _valid_pre_kickoff(
    predictions: Sequence[Mapping[str, Any]],
    *,
    kickoff_utc: str,
) -> list[tuple[str, str, Mapping[str, Any]]]:
    kickoff = _parse(kickoff_utc)
    if kickoff is None:
        return []
    valid = [
        (str(row.get("generated_at")), str(row.get("version_key") or row.get("prediction_id") or ""), row)
        for row in predictions
        if _is_valid_pre_kickoff(row, kickoff)
    ]
    valid.sort(key=lambda item: (item[0], item[1]))
    return valid


def resolve_first_issued_prediction(
    predictions: Sequence[Mapping[str, Any]],
    *,
    kickoff_utc: str,
) -> Mapping[str, Any] | None:
    """Earliest valid READY prediction genuinely issued before kickoff.

    Rule: status READY, ``generated_at < kickoff``, ``model_as_of`` present with
    ``model_as_of <= generated_at`` and ``model_as_of < kickoff``. Earliest
    ``generated_at`` wins; equal instants tie-break on ``version_key``. Post-kickoff
    versions are never used.
    """
    valid = _valid_pre_kickoff(predictions, kickoff_utc=kickoff_utc)
    return valid[0][2] if valid else None


def _pick_rows_from_prediction(
    prediction: Mapping[str, Any],
    *,
    fixture: Mapping[str, Any],
) -> list[dict[str, Any]]:
    payload = prediction.get("payload") or {}
    market_rows = payload.get("market_rows") or []
    selection = select_model_picks(market_rows)
    model_versions = payload.get("model_versions") or {}
    model_version = next((str(model_versions[k]) for k in _MODEL_VERSION_KEYS if model_versions.get(k)), None)
    rows: list[dict[str, Any]] = []
    for pick in (selection["asian_handicap"], selection["corner_total"]):
        if pick is None:
            continue
        rows.append(
            {
                "policy_id": POLICY_ID,
                "policy_version": POLICY_VERSION,
                "fixture_id": str(prediction["fixture_id"]),
                "canonical_fixture_id": fixture.get("canonical_fixture_id"),
                "competition_id": fixture.get("competition_id"),
                "competition_name": fixture.get("competition_name"),
                "kickoff_utc": fixture.get("kickoff_utc"),
                "market": pick["market"],
                "line": pick["line"],
                "selection": pick["selection"],
                "model_probability": pick["model_probability"],
                "model_fair_price": pick["model_fair_price"],
                "prediction_id": prediction.get("prediction_id"),
                "model_snapshot_id": prediction.get("model_snapshot_id"),
                "model_version": model_version,
                "prediction_generated_at": prediction.get("generated_at"),
                "model_as_of": prediction.get("model_as_of"),
                "record_origin": RECORD_ORIGIN_LIVE_ISSUED,
                "source_prediction_version_key": prediction.get("version_key"),
                "balance_score": pick["balance_score"],
            }
        )
    return rows


def _load_predictions(dashboard_conn: Any) -> list[dict[str, Any]]:
    rows = dashboard_conn.execute(
        "SELECT prediction_id, fixture_id, status, generated_at, model_as_of, model_snapshot_id, "
        "version_key, payload_json FROM dashboard_predictions"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        try:
            record["payload"] = json.loads(record.pop("payload_json") or "{}")
        except (TypeError, ValueError):
            record["payload"] = {}
        out.append(record)
    return out


def _load_fixtures(dashboard_conn: Any) -> dict[str, dict[str, Any]]:
    rows = dashboard_conn.execute(
        "SELECT fixture_id, competition_id, competition_name, home_team_name, away_team_name, "
        "kickoff_utc, status, home_score, away_score FROM dashboard_fixtures"
    ).fetchall()
    return {str(r["fixture_id"]): dict(r) for r in rows}


def _load_results(dashboard_conn: Any, fixture_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    try:
        rows = dashboard_conn.execute(
            "SELECT fixture_id, home_corners, away_corners, total_corners FROM dashboard_fixture_results"
        ).fetchall()
    except Exception:  # noqa: BLE001 - table may not exist yet
        return out
    for row in rows:
        record = dict(row)
        total = record.get("total_corners")
        if total is None and record.get("home_corners") is not None and record.get("away_corners") is not None:
            total = int(record["home_corners"]) + int(record["away_corners"])
        out[str(record["fixture_id"])] = {"total_corners": total}
    return out


def _canonical_id(fixture: Mapping[str, Any]) -> str | None:
    if not all(fixture.get(k) for k in ("competition_id", "home_team_name", "away_team_name", "kickoff_utc")):
        return None
    from ..storage.football_canonical_fixture import canonical_fixture_id_from_identity

    return canonical_fixture_id_from_identity(
        competition_id=str(fixture["competition_id"]),
        home_team_name=str(fixture["home_team_name"]),
        away_team_name=str(fixture["away_team_name"]),
        kickoff_utc=str(fixture["kickoff_utc"]),
    )


def build_live_issued_ledger(
    dashboard_conn: Any,
    *,
    dry_run: bool = True,
    ledger_conn: Any | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Phase A. Returns a manifest; writes LIVE_ISSUED rows only when dry_run is False."""
    predictions = _load_predictions(dashboard_conn)
    fixtures = _load_fixtures(dashboard_conn)
    results = _load_results(dashboard_conn, fixtures.keys())

    by_fixture: dict[str, list[dict[str, Any]]] = {}
    for prediction in predictions:
        by_fixture.setdefault(str(prediction["fixture_id"]), []).append(prediction)

    proposed: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    pre_kickoff_ready = 0
    post_kickoff_excluded = 0
    duplicate_fixtures = 0
    missing_diag: list[dict[str, Any]] = []

    for fixture_id, fixture in fixtures.items():
        fixture_predictions = by_fixture.get(fixture_id, [])
        ready = [p for p in fixture_predictions if str(p.get("status") or "").upper() == "READY"]
        if len(ready) > 1:
            duplicate_fixtures += 1
        if not ready:
            continue
        valid = _valid_pre_kickoff(fixture_predictions, kickoff_utc=str(fixture.get("kickoff_utc") or ""))
        post_kickoff_excluded += len(ready) - len(valid)
        if not valid:
            diagnostics.append({"fixture_id": fixture_id, "reason": "NO_PRE_KICKOFF_READY_PREDICTION"})
            continue
        pre_kickoff_ready += 1
        chosen = valid[0][2]
        enriched = {**fixture, "canonical_fixture_id": _canonical_id(fixture)}
        rows = _pick_rows_from_prediction(chosen, fixture=enriched)
        if not rows:
            market_rows = (chosen.get("payload") or {}).get("market_rows") or []
            diag = select_model_picks(market_rows)["diagnostics"]
            missing_diag.append({"fixture_id": fixture_id, "reason": "NO_COMPLETE_PAIRED_LINE", "detail": diag})
            continue
        proposed.extend(rows)

    written = 0
    if not dry_run:
        if ledger_conn is None:
            raise ValueError("ledger_conn is required when dry_run is False")
        from ..storage import football_model_pick_ledger as ledger

        ledger.ensure_schema(ledger_conn)
        for pick in proposed:
            ledger.record_model_pick(ledger_conn, pick, now=now, initialize_schema=False)
            written += 1
        ledger_conn.commit()

    return {
        "policy_id": POLICY_ID,
        "policy_version": POLICY_VERSION,
        "dry_run": dry_run,
        "record_origin": RECORD_ORIGIN_LIVE_ISSUED,
        "predictions": len(predictions),
        "ready_predictions": sum(1 for p in predictions if str(p.get("status") or "").upper() == "READY"),
        "blocked_predictions": sum(1 for p in predictions if str(p.get("status") or "").upper() == "BLOCKED"),
        "pre_kickoff_ready": pre_kickoff_ready,
        "post_kickoff_excluded": post_kickoff_excluded,
        "duplicate_fixtures": duplicate_fixtures,
        "proposed_ah_model_picks": sum(1 for p in proposed if p["market"] == "asian_handicap"),
        "proposed_corner_model_picks": sum(1 for p in proposed if p["market"] == "corner_total"),
        "missing_paired_lines": missing_diag,
        "diagnostics": diagnostics,
        "written": written,
        "proposed_picks": proposed,
        "fixtures_with_results": len(results),
    }


# --------------------------------------------------------------------------- timeline
def _value_bets_by_day(paper_conn: Any | None) -> tuple[dict[str, int], str | None, str | None]:
    if paper_conn is None:
        return {}, None, None
    issued: dict[str, int] = {}
    for row in paper_conn.execute("SELECT created_at FROM paper_trade_entries").fetchall():
        day = _day(row["created_at"])
        if day:
            issued[day] = issued.get(day, 0) + 1
    latest_issued = max(issued) if issued else None
    latest_update = None
    try:
        row = paper_conn.execute("SELECT MAX(updated_at) AS m FROM paper_trade_updates").fetchone()
        latest_update = _day(row["m"]) if row and row["m"] else None
    except Exception:  # noqa: BLE001, S110
        pass
    return issued, latest_issued, latest_update


def build_daily_timeline(
    *,
    ledger_rows: Sequence[Mapping[str, Any]],
    predictions: Sequence[Mapping[str, Any]],
    fixtures: Sequence[Mapping[str, Any]],
    value_bets_by_day: Mapping[str, int] | None = None,
    first_day: str | None = None,
    last_day: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Aligned calendar timeline. Every day appears; gaps are never silently zero."""
    value_bets_by_day = dict(value_bets_by_day or {})
    resolved_today = today or datetime.now(timezone.utc).date()

    fixture_days: dict[str, int] = {}
    for fixture in fixtures:
        day = _day(fixture.get("kickoff_utc"))
        if day:
            fixture_days[day] = fixture_days.get(day, 0) + 1

    pred_ready_by_fixture: dict[str, int] = {}
    pred_blocked_by_fixture: dict[str, int] = {}
    for prediction in predictions:
        fid = str(prediction.get("fixture_id") or "")
        if str(prediction.get("status") or "").upper() == "READY":
            pred_ready_by_fixture[fid] = pred_ready_by_fixture.get(fid, 0) + 1
        elif str(prediction.get("status") or "").upper() == "BLOCKED":
            pred_blocked_by_fixture[fid] = pred_blocked_by_fixture.get(fid, 0) + 1

    fixture_kickoff_day: dict[str, str] = {}
    for fixture in fixtures:
        day = _day(fixture.get("kickoff_utc"))
        if day:
            fixture_kickoff_day[str(fixture.get("fixture_id"))] = day

    ledger_by_day: dict[str, list[dict[str, Any]]] = {}
    for row in ledger_rows:
        day = _day(row.get("kickoff_utc")) or _day(row.get("prediction_generated_at"))
        if day:
            ledger_by_day.setdefault(day, []).append(dict(row))

    present = sorted({*fixture_days, *ledger_by_day, *value_bets_by_day})
    start = first_day or (present[0] if present else None)
    end = last_day or (present[-1] if present else None) or resolved_today.isoformat()
    if start is None:
        all_days = []
    else:
        start_dt = _parse(start).date()
        end_dt = _parse(end).date()
        all_days = [
            (start_dt + timedelta(days=offset)).isoformat()
            for offset in range((end_dt - start_dt).days + 1)
        ]

    days: list[dict[str, Any]] = []
    for day in all_days:
        picks = ledger_by_day.get(day, [])
        ah = sum(1 for p in picks if p["market"] == "asian_handicap")
        corner = sum(1 for p in picks if p["market"] == "corner_total")
        states = [p.get("trade_state") for p in picks]
        settled = sum(1 for s in states if s)
        open_count = len(states) - settled
        full_wins = sum(1 for s in states if s in ("FULL_WIN", "WON"))
        half_wins = sum(1 for s in states if s == "HALF_WIN")
        pushes = sum(1 for s in states if s == "PUSH")
        half_losses = sum(1 for s in states if s == "HALF_LOSS")
        full_losses = sum(1 for s in states if s in ("FULL_LOSS", "LOST"))

        fixtures_today = fixture_days.get(day, 0)
        ready_today = sum(pred_ready_by_fixture.get(fid, 0) for fid, d in fixture_kickoff_day.items() if d == day)
        blocked_today = sum(pred_blocked_by_fixture.get(fid, 0) for fid, d in fixture_kickoff_day.items() if d == day)
        vb = value_bets_by_day.get(day, 0)

        if fixtures_today == 0 and not picks:
            origin_status = DAY_NO_FIXTURES
        elif picks:
            origin_status = DAY_LIVE_ISSUED
        elif fixtures_today > 0 and ready_today + blocked_today == 0:
            origin_status = DAY_REPLAY_PENDING
        else:
            origin_status = DAY_NO_LIVE_PREDICTION

        days.append(
            {
                "date": day,
                "fixtures": fixtures_today,
                "ready_predictions": ready_today,
                "blocked_predictions": blocked_today,
                "ah_model_picks": ah,
                "corner_model_picks": corner,
                "settled": settled,
                "open": open_count,
                "full_wins": full_wins,
                "half_wins": half_wins,
                "pushes": pushes,
                "half_losses": half_losses,
                "full_losses": full_losses,
                "value_bets": vb,
                "market_evidence": MARKET_EVIDENCE_AVAILABLE if vb > 0 else NO_MARKET_EVIDENCE,
                "origin_status": origin_status,
            }
        )

    pick_days = [d["date"] for d in days if d["ah_model_picks"] + d["corner_model_picks"] > 0]
    settled_days = [d["date"] for d in days if d["settled"] > 0]
    labels = {
        "latest_model_pick_day": max(pick_days) if pick_days else None,
        "latest_settled_model_pick_day": max(settled_days) if settled_days else None,
    }
    return {"days": days, "labels": labels}


def build_value_bet_labels(paper_conn: Any | None) -> dict[str, Any]:
    issued, latest_issued, latest_update = _value_bets_by_day(paper_conn)
    return {
        "latest_value_bet_issued_day": latest_issued,
        "latest_value_bet_settlement_update": latest_update,
        "value_bets_by_day": issued,
    }
