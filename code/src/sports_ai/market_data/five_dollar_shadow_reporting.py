"""Read-only lifecycle and coverage diagnostics for the FRA1 5Dollar shadow.

Everything here is strictly observational and read-only:

* it NEVER writes to ``market_snapshots`` and never rewrites provider phase
  labels;
* it derives our OWN ``last_pre_match_observed`` timestamp (latest
  ``is_pre_match = 1`` capture strictly before kickoff) because the provider's
  early ``closing`` label must NOT be treated as the authoritative final close;
* it reports per-fixture AH/corner coverage and when a market first appeared,
  so we can learn how far before kickoff corner markets become available.

No values produced here feed betting, Manager, portfolio, staking, or model
paths.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

FIVE_DOLLAR_PROVIDER = "5dollar-football-api"
MARKET_ASIAN_HANDICAP = "asian_handicap"
MARKET_CORNER_TOTAL = "corner_total"

_COVERAGE_COLUMNS = (
    "fixture_id",
    "provider_fixture_id",
    "market",
    "selection",
    "line",
    "odds",
    "provider_market_id",
    "captured_at",
    "is_pre_match",
    "kickoff_utc",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _phase(row: Mapping[str, Any]) -> str | None:
    parts = str(row.get("provider_market_id") or "").split(":")
    return parts[3] if len(parts) >= 6 else None


def fetch_shadow_market_rows(
    conn: Any,
    *,
    fixture_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Read all persisted 5Dollar rows (optionally for specific fixtures)."""
    ids = [str(f) for f in (fixture_ids or []) if f]
    columns = ", ".join(_COVERAGE_COLUMNS)
    if ids:
        placeholders = ",".join("?" for _ in ids)
        sql = (
            f"SELECT {columns} FROM market_snapshots "
            f"WHERE provider = ? AND fixture_id IN ({placeholders})"
        )
        rows = conn.execute(sql, [FIVE_DOLLAR_PROVIDER, *ids]).fetchall()
    else:
        sql = f"SELECT {columns} FROM market_snapshots WHERE provider = ?"
        rows = conn.execute(sql, [FIVE_DOLLAR_PROVIDER]).fetchall()
    return [dict(row) for row in rows]


def last_pre_match_observed(conn: Any, *, fixture_id: str | None = None) -> list[dict[str, Any]]:
    """Latest pre-match capture per fixture/market/selection/line.

    ``is_pre_match = 1`` AND ``captured_at < kickoff_utc``. This is our own
    timestamp-based final observed pre-match quote -- never the provider's
    early ``closing`` label. Read-only.
    """
    rows = fetch_shadow_market_rows(conn, fixture_ids=[fixture_id] if fixture_id else None)
    best: dict[tuple[str, str, str, float | None], dict[str, Any]] = {}
    for row in rows:
        if int(row.get("is_pre_match") or 0) != 1:
            continue
        captured = _parse(row.get("captured_at"))
        kickoff = _parse(row.get("kickoff_utc"))
        if captured is None or kickoff is None or captured >= kickoff:
            continue
        key = (str(row["fixture_id"]), str(row["market"]), str(row["selection"]), row.get("line"))
        current = best.get(key)
        if current is None or str(row["captured_at"]) > str(current["last_pre_match_observed"]):
            best[key] = {
                "fixture_id": row["fixture_id"],
                "market": row["market"],
                "selection": row["selection"],
                "line": row.get("line"),
                "last_pre_match_observed": row.get("captured_at"),
                "provider_phase": _phase(row),
            }
    return sorted(best.values(), key=lambda r: (str(r["fixture_id"]), str(r["market"]), str(r["selection"]), str(r["line"])))


def _market_coverage(rows: Sequence[Mapping[str, Any]], market: str, kickoff: datetime | None) -> dict[str, Any]:
    market_rows = [r for r in rows if str(r.get("market")) == market]
    captured = sorted(str(r.get("captured_at")) for r in market_rows if r.get("captured_at"))
    phases = sorted({p for p in (_phase(r) for r in market_rows) if p})
    lines = sorted({r.get("line") for r in market_rows if r.get("line") is not None})
    pre = [r for r in market_rows if int(r.get("is_pre_match") or 0) == 1 and _parse(r.get("captured_at")) is not None]
    pre = [r for r in pre if kickoff is None or _parse(r["captured_at"]) < kickoff]
    last_pre = max((str(r["captured_at"]) for r in pre), default=None)
    first_seen = captured[0] if captured else None
    hours_before = None
    if kickoff is not None and first_seen is not None:
        first_dt = _parse(first_seen)
        if first_dt is not None:
            hours_before = round((kickoff - first_dt).total_seconds() / 3600.0, 2)
    return {
        "available": bool(market_rows),
        "phases": phases,
        "lines": lines,
        "first_capture": first_seen,
        "latest_capture": captured[-1] if captured else None,
        "first_seen_at": first_seen,
        "hours_before_kickoff_when_first_seen": hours_before,
        "last_pre_match_observed": last_pre,
        "observation_count": len(market_rows),
    }


def build_coverage_report(
    conn: Any,
    fixtures: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Diagnostic coverage report for a set of discovered FRA1 fixtures."""
    resolved_now = now or _now()
    discovered = list(fixtures)
    fixture_ids = [str(f.get("fixture_id")) for f in discovered if f.get("fixture_id")]
    try:
        rows = fetch_shadow_market_rows(conn, fixture_ids=fixture_ids)
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return {"error": "shadow_market_read_failed", "fixtures": [], "summary": {}}

    by_fixture: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_fixture.setdefault(str(row["fixture_id"]), []).append(row)

    report_fixtures: list[dict[str, Any]] = []
    ever = collected_24h = ah_count = corner_count = opening = closing = inplay = 0
    missing_ah: list[str] = []
    missing_corners: list[str] = []
    for fixture in discovered:
        fixture_id = str(fixture.get("fixture_id") or "")
        home = fixture.get("home_team_name") or fixture.get("home")
        away = fixture.get("away_team_name") or fixture.get("away")
        kickoff = _parse(fixture.get("kickoff_utc"))
        fixture_rows = by_fixture.get(fixture_id, [])
        ah = _market_coverage(fixture_rows, MARKET_ASIAN_HANDICAP, kickoff)
        corner = _market_coverage(fixture_rows, MARKET_CORNER_TOTAL, kickoff)
        if fixture_rows:
            ever += 1
        latest = max((str(r.get("captured_at")) for r in fixture_rows if r.get("captured_at")), default=None)
        latest_dt = _parse(latest)
        if latest_dt is not None and (resolved_now - latest_dt) <= timedelta(hours=24):
            collected_24h += 1
        if ah["available"]:
            ah_count += 1
        else:
            missing_ah.append(fixture_id)
        if corner["available"]:
            corner_count += 1
        else:
            missing_corners.append(fixture_id)
        all_phases = set(ah["phases"]) | set(corner["phases"])
        if "opening" in all_phases:
            opening += 1
        if "closing" in all_phases:
            closing += 1
        if "inplay" in all_phases:
            inplay += 1
        last_pre = [ah["last_pre_match_observed"], corner["last_pre_match_observed"]]
        last_pre = [p for p in last_pre if p]
        report_fixtures.append(
            {
                "fixture_id": fixture_id,
                "home": home,
                "away": away,
                "kickoff_utc": fixture.get("kickoff_utc"),
                "ah": ah,
                "corner": corner,
                "last_pre_match_observed": max(last_pre) if last_pre else None,
            }
        )

    return {
        "generated_at": resolved_now.isoformat(timespec="seconds"),
        "summary": {
            "fixtures_discovered": len(discovered),
            "fixtures_ever_collected": ever,
            "fixtures_collected_last_24h": collected_24h,
            "ah_coverage_count": ah_count,
            "corner_coverage_count": corner_count,
            "opening_coverage": opening,
            "closing_labelled_coverage": closing,
            "inplay_coverage": inplay,
            "fixtures_missing_ah": missing_ah,
            "fixtures_missing_corners": missing_corners,
        },
        "fixtures": report_fixtures,
    }


def coverage_log_fields(report: Mapping[str, Any]) -> dict[str, Any]:
    """Compact, secret-free fields for structured logging."""
    summary = dict(report.get("summary") or {})
    return {
        "discovered": summary.get("fixtures_discovered", 0),
        "ever_collected": summary.get("fixtures_ever_collected", 0),
        "collected_24h": summary.get("fixtures_collected_last_24h", 0),
        "ah": summary.get("ah_coverage_count", 0),
        "corner": summary.get("corner_coverage_count", 0),
        "missing_corner": len(summary.get("fixtures_missing_corners") or []),
    }
