"""Read-only, date-scoped reporting for real LIVE_ISSUED Football Model Picks.

The report is kickoff-date based and reports model accuracy, NOT betting ROI.
It never queries providers, rewrites sporting truth, or mixes replay picks.

Only genuinely forward picks count: ``record_origin = LIVE_ISSUED`` *and*
``created_at`` strictly before kickoff. Early manual CLI runs labelled picks for
already-kicked-off fixtures ``LIVE_ISSUED``; those are reported separately as
``excluded_retrospective`` and never enter the headline.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from ..storage.football_model_pick_ledger import ISSUANCE_TIMING_FORWARD, issuance_timing
from .model_pick_history import _aggregate, _parse, _connect_readonly

MARKETS = ("asian_handicap", "corner_total")


def build_weekend_model_pick_report(
    *, db_path: str | Path, start_date: date, end_date: date,
    timezone_name: str = "Europe/London",
) -> dict[str, object]:
    """Summarize only LIVE_ISSUED rows with UK-local kickoff in inclusive dates."""
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    tz = ZoneInfo(timezone_name)
    db = Path(db_path)
    if not db.exists():
        raise FileNotFoundError("model pick ledger does not exist")

    with closing(_connect_readonly(db)) as conn:
        raw_rows = [dict(row) for row in conn.execute(
            "SELECT * FROM football_model_pick_entries WHERE record_origin = 'LIVE_ISSUED'"
        )]

    selected: list[dict[str, object]] = []
    excluded_bad_kickoff = 0
    excluded_timing: dict[str, int] = {}
    seen_ids: set[str] = set()
    for row in raw_rows:
        kickoff = _parse(row.get("kickoff_utc"))
        if kickoff is None:
            excluded_bad_kickoff += 1
            continue
        local_day = kickoff.astimezone(tz).date()
        if not start_date <= local_day <= end_date:
            continue
        identity = str(row.get("model_pick_id") or "")
        if not identity or identity in seen_ids:
            raise ValueError("duplicate or missing model_pick_id in weekend LIVE_ISSUED ledger")
        seen_ids.add(identity)
        timing = issuance_timing(row)
        if timing != ISSUANCE_TIMING_FORWARD:
            excluded_timing[timing] = excluded_timing.get(timing, 0) + 1
            continue
        selected.append(row)

    total = _aggregate(selected)
    by_market = {
        market: _aggregate([row for row in selected if row.get("market") == market])
        for market in MARKETS
    }
    decisive = (
        total["full_wins"] + total["half_wins"]
        + total["half_losses"] + total["full_losses"]
    )
    return {
        "source": "football_model_pick_entries",
        "record_origin": "LIVE_ISSUED",
        "issuance_timing": ISSUANCE_TIMING_FORWARD,
        "basis": "kickoff_local_day",
        "timezone": timezone_name,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "metric": "weighted_success_rate",
        "metric_definition": "(full_wins + 0.5*half_wins)/(full_wins+half_wins+half_losses+full_losses); pushes and pending excluded",
        "decisive_count": decisive,
        "summary": total,
        "by_market": by_market,
        "excluded_not_forward": excluded_timing,
        "unparseable_live_issued_kickoffs": excluded_bad_kickoff,
        "complete_kickoff_population": excluded_bad_kickoff == 0,
        "excludes": ["CURRENT_SYSTEM_REPLAY", "VALUE_BETS", "PAPER_POSITIONS"],
        "data_as_of": max(
            (str(row.get("updated_at")) for row in selected if row.get("updated_at")),
            default=None,
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Football weekend Model Pick report")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", required=True, type=date.fromisoformat)
    parser.add_argument("--timezone", default="Europe/London")
    args = parser.parse_args(argv)
    try:
        result = build_weekend_model_pick_report(
            db_path=args.db_path, start_date=args.start_date, end_date=args.end_date,
            timezone_name=args.timezone,
        )
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": type(exc).__name__}))
        return 2
    print(json.dumps({"status": "OK", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
