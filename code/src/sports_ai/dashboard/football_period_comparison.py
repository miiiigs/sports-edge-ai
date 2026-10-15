"""Read-only, offline September/October comparison of genuine forward Football Model Picks.

Requires a CONSISTENT SQLite-online-backup copy of football_model_picks.sqlite;
never open a live WAL database with immutable=1. No provider calls or writes.
Paper selections, retrospective rows, and Value Bets are deliberately excluded.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

POLICY_ID = "football_model_pick_balanced_line_v1"
MARKETS = ("asian_handicap", "corner_total")
RESULTS = ("FULL_WIN", "HALF_WIN", "PUSH", "HALF_LOSS", "FULL_LOSS")
ALIASES = {"WON": "FULL_WIN", "LOST": "FULL_LOSS"}
PENDING = {"", "PENDING", "OPEN", "UNSETTLED", "RESULT_PENDING"}
PERIODS = {
    "september_2026": ("2026-09-01", "2026-09-30"),
    "october_1_9": ("2026-10-01", "2026-10-09"),
    "weekend_october_10_11": ("2026-10-10", "2026-10-11"),
    "october_12_14": ("2026-10-12", "2026-10-14"),
    "october_1_14_overlapping_total": ("2026-10-01", "2026-10-14"),
}


def instant(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def read_offline_copy(path: Path) -> list[dict]:
    """Read only a completed, standalone offline backup; no schema changes."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError("OFFLINE_LEDGER_COPY_MISSING")
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise ValueError("NOT_STANDALONE_BACKUP: WAL/SHM sidecars present")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("INVALID_LEDGER_INTEGRITY")
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM football_model_pick_entries ORDER BY created_at, model_pick_id"
            )
        ]
    finally:
        conn.close()


def summary(rows: list[dict]) -> dict:
    states = Counter(row["state"] for row in rows)
    fw, hw, push, hl, fl = (states[state] for state in RESULTS)
    won, lost = fw + 0.5 * hw, fl + 0.5 * hl
    lead_hours = [row["lead_hours"] for row in rows]
    return {
        "issued": len(rows), "settled": fw + hw + push + hl + fl,
        "pending": sum(states[s] for s in PENDING),
        # Split by the attested snapshot time: kickoff still ahead vs result overdue
        # (stale sporting truth, cancelled/postponed fixtures). Unclassified when no
        # snapshot time is supplied.
        "pending_upcoming": sum(row.get("pending_kind") == "upcoming" for row in rows),
        "pending_overdue": sum(row.get("pending_kind") == "overdue" for row in rows),
        "pending_unclassified": sum(row.get("pending_kind") == "unclassified" for row in rows),
        "void": states["VOID"], "invalid_result_states": {k: v for k, v in states.items() if k not in RESULTS and k not in PENDING and k != "VOID"},
        "full_wins": fw, "half_wins": hw, "pushes": push,
        "half_losses": hl, "full_losses": fl,
        "decisive_count": fw + hw + hl + fl,
        "effective_decisive_stake": won + lost,
        "stake_weighted_win_share": round(won / (won + lost), 6) if won + lost else None,
        "weighted_success_rate": round(won / (fw + hw + hl + fl), 6) if fw + hw + hl + fl else None,
        "median_hours_issued_before_kickoff": round(median(lead_hours), 2) if lead_hours else None,
        "issued_48h_or_more_before_kickoff": sum(h >= 48 for h in lead_hours),
    }


def audit(rows: list[dict], timezone_name="Europe/London", as_of: datetime | None = None) -> dict:
    tz = ZoneInfo(timezone_name)
    # Deterministic: when two rows collide on canonical match+market, the
    # earliest-issued one (then lowest id) is kept regardless of input order.
    rows = sorted(rows, key=lambda row: (str(row.get("created_at") or ""), str(row.get("model_pick_id") or "")))
    accepted = []
    excluded = Counter()
    failures = []
    seen_ids = set()
    seen_canonical = set()

    for raw in rows:
        if raw.get("record_origin") != "LIVE_ISSUED":
            excluded["not_live_issued"] += 1
            continue
        if raw.get("policy_id") not in (POLICY_ID, None):
            excluded["different_policy"] += 1
            continue
        kickoff = instant(raw.get("kickoff_utc"))
        created = instant(raw.get("created_at"))
        if kickoff is None:
            failures.append({"id": str(raw.get("model_pick_id")), "reason": "BAD_KICKOFF"})
            continue
        if created is None:
            excluded["missing_created_at"] += 1
            continue
        if created >= kickoff:
            excluded["retrospective_created_at"] += 1
            continue
        market = raw.get("market")
        if market not in MARKETS:
            excluded["different_market"] += 1
            continue

        pick_id = str(raw.get("model_pick_id") or "")
        if not pick_id or pick_id in seen_ids:
            failures.append({"id": pick_id, "reason": "DUPLICATE_OR_MISSING_PICK_ID"})
            continue
        seen_ids.add(pick_id)
        canonical = str(raw.get("canonical_fixture_id") or "")
        if canonical:
            ckey = (POLICY_ID, canonical, market)
            if ckey in seen_canonical:
                failures.append({"id": pick_id, "reason": "DUPLICATE_CANONICAL_MATCH_MARKET"})
                continue
            seen_canonical.add(ckey)

        generated = instant(raw.get("prediction_generated_at"))
        model_as_of = instant(raw.get("model_as_of"))
        state = ALIASES.get(str(raw.get("trade_state") or ""), str(raw.get("trade_state") or ""))
        row_failures = []
        if generated is not None and generated >= kickoff:
            row_failures.append("PREDICTION_NOT_PREKICKOFF")
        if model_as_of is not None and model_as_of >= kickoff:
            row_failures.append("MODEL_STATE_NOT_PREKICKOFF")
        if state not in RESULTS and state not in PENDING and state != "VOID":
            row_failures.append("UNKNOWN_RESULT_STATE")
        if row_failures:
            # Fail closed: an integrity-failed row is reported, never counted.
            failures.extend({"id": pick_id, "reason": reason} for reason in row_failures)
            continue
        pending_kind = None
        if state in PENDING:
            pending_kind = "unclassified" if as_of is None else ("upcoming" if kickoff > as_of else "overdue")
        accepted.append({
            "id": pick_id, "market": market,
            "competition": str(raw.get("competition_id") or "UNKNOWN"),
            "kickoff_day": kickoff.astimezone(tz).date().isoformat(),
            "state": state, "lead_hours": (kickoff - created).total_seconds() / 3600,
            "pending_kind": pending_kind,
        })

    def cohort(data):
        return {
            "summary": summary(data),
            "by_market": {m: summary([r for r in data if r["market"] == m]) for m in MARKETS},
            "by_league": {
                comp: {m: summary([r for r in data if r["market"] == m and r["competition"] == comp]) for m in MARKETS}
                for comp in sorted({r["competition"] for r in data})
            },
            "by_kickoff_day": {
                day: {m: summary([r for r in data if r["market"] == m and r["kickoff_day"] == day]) for m in MARKETS}
                for day in sorted({r["kickoff_day"] for r in data})
            },
            "result_completeness": (
                "NO_FORWARD_PICKS_IN_LEDGER_COPY" if not data
                else "PENDING_OR_UNGRADED" if summary(data)["pending"] or summary(data)["void"]
                else "ALL_SETTLED"
            ),
        }

    result = {name: {"start": start, "end": end, **cohort([r for r in accepted if start <= r["kickoff_day"] <= end])}
              for name, (start, end) in PERIODS.items()}
    return {
        "schema": "football_forward_month_comparison_v1",
        "status": "INTEGRITY_BLOCKED" if failures else "RECONCILED_OFFLINE_COPY",
        "timezone": timezone_name,
        "basis": "kickoff_local_day",
        "source": "football_model_pick_entries",
        "definition": "LIVE_ISSUED, created_at strictly before kickoff, balanced-line AH and corners",
        "source_rows": len(rows), "forward_rows_all_dates": len(accepted),
        "excluded": dict(sorted(excluded.items())),
        "integrity_failures": failures[:100],
        "disclaimers": [
            "Forward accuracy only, not paper ROI, Strong Value Bets, CLV or betting profitability",
            "A month with zero rows may reflect missing issuance coverage; it is not a 0% win rate",
            "Oct 1-14 total overlaps its individual subperiods and must not be added twice",
        ],
        "periods": result,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger-copy", type=Path, required=True)
    parser.add_argument("--snapshot-as-of", required=True, help="Actual verified backup timestamp, not an assumed date")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if instant(args.snapshot_as_of) is None:
        print(json.dumps({"status": "BLOCKED", "reason": "INVALID_SNAPSHOT_TIMESTAMP"}))
        return 2
    try:
        report = audit(read_offline_copy(args.ledger_copy), as_of=instant(args.snapshot_as_of))
    except (OSError, sqlite3.Error, ValueError) as err:
        print(json.dumps({"status": "BLOCKED", "reason": type(err).__name__}))
        return 2
    report["snapshot_as_of_operator_attested"] = args.snapshot_as_of
    output = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 3 if report["status"] == "INTEGRITY_BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
