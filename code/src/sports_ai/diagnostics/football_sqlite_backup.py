"""Consistent, read-only backup of the Football operational SQLite databases.

Run by the owner before a deploy or flag change (docs/FOOTBALL_M1_BACKUP_RUNBOOK.md).

* Each source is opened ``mode=ro`` and copied with SQLite's online backup API,
  so committed WAL content is included and a concurrent writer can never leave
  a torn copy (the backup restarts if the source changes mid-copy). Nothing is
  written next to the sources: no ``-wal`` checkpoint, no schema creation.
* Every copy is checked with ``PRAGMA integrity_check`` and gets row counts
  and a sha256 in ``manifest.json``. The manifest holds counts and hashes only,
  never row contents, paths outside the destination, or credentials.
* ``--verify`` re-checks a backup directory against its manifest before any
  restore.

The four databases are separate files with no cross-database transaction, so
the copies are taken back to back in a fixed order; take them while the app is
sleeping or with every writer flag OFF for a quiescent set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "football_sqlite_backup_v1"

# Fixed order: auth first, ledger last (its picks reference fixtures/predictions).
DATABASES: tuple[str, ...] = (
    "sports_edge_auth.sqlite",
    "football_dashboard.sqlite",
    "football_paper_trading.sqlite",
    "football_model_picks.sqlite",
)
OPTIONAL_DATABASES: tuple[str, ...] = ("football_high_confidence_shadow.sqlite",)

# Tables whose counts are reconciled after backup and after a restore.
RECONCILED_TABLES: dict[str, tuple[str, ...]] = {
    "football_dashboard.sqlite": ("dashboard_fixtures", "dashboard_predictions", "dashboard_fixture_results"),
    "football_paper_trading.sqlite": ("paper_trade_entries", "paper_trade_updates", "market_snapshots"),
    "football_model_picks.sqlite": ("football_model_pick_entries",),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=60)


def describe(path: Path) -> dict[str, Any]:
    """Integrity result and per-table row counts of one SQLite file (read-only)."""
    conn = _ro(path)
    try:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in tables}
    finally:
        conn.close()
    return {"integrity_check": "ok" if integrity == ["ok"] else integrity[:20], "row_counts": counts}


def backup_database(source: Path, destination: Path) -> dict[str, Any]:
    """Copy one database with the online backup API from a ``mode=ro`` source."""
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite {destination.name}")
    src = _ro(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    # A standalone file: no -wal sidecar, so the copy is self-contained.
    conn = sqlite3.connect(destination)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()
    info = describe(destination)
    return {
        "file": destination.name,
        "bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
        **info,
    }


def run_backup(source_dir: Path, destination_dir: Path) -> dict[str, Any]:
    source_dir = Path(source_dir)
    destination_dir = Path(destination_dir)
    if destination_dir.resolve() == source_dir.resolve() or source_dir.resolve() in destination_dir.resolve().parents:
        raise ValueError("destination must be outside the source data directory")
    destination_dir.mkdir(parents=True, exist_ok=False)
    started = _utc_now()
    databases: dict[str, Any] = {}
    missing: list[str] = []
    for name in DATABASES + OPTIONAL_DATABASES:
        source = source_dir / name
        if not source.exists():
            if name in DATABASES:
                missing.append(name)
            continue
        databases[name] = {"copied_at": _utc_now(), **backup_database(source, destination_dir / name)}
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "started_at": started,
        "completed_at": _utc_now(),
        "databases": databases,
        "missing_required": missing,
        "all_integrity_ok": all(d["integrity_check"] == "ok" for d in databases.values()) and not missing,
    }
    (destination_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    """Re-check every file in a backup directory against its manifest."""
    backup_dir = Path(backup_dir)
    manifest = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    problems: list[str] = []
    for name, expected in manifest["databases"].items():
        path = backup_dir / name
        if not path.exists():
            problems.append(f"{name}: missing")
            continue
        if _sha256(path) != expected["sha256"]:
            problems.append(f"{name}: sha256 mismatch")
        actual = describe(path)
        if actual["integrity_check"] != "ok":
            problems.append(f"{name}: integrity_check failed")
        if actual["row_counts"] != expected["row_counts"]:
            problems.append(f"{name}: row counts differ")
    problems.extend(f"{name}: not in backup" for name in manifest.get("missing_required", []))
    return {"ok": not problems, "problems": problems}


def reconcile(backup_dir: Path, live_dir: Path) -> dict[str, Any]:
    """Compare reconciled-table counts of a backup with a live (or restored) directory, read-only.

    Live counts may only be >= the backup's (forward-only writes after the backup).
    """
    manifest = json.loads((Path(backup_dir) / MANIFEST_NAME).read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for name, tables in RECONCILED_TABLES.items():
        backed = manifest["databases"].get(name, {}).get("row_counts", {})
        live_path = Path(live_dir) / name
        live = describe(live_path)["row_counts"] if live_path.exists() else {}
        for table in tables:
            b, l = backed.get(table), live.get(table)
            rows.append({"database": name, "table": table, "backup": b, "live": l,
                         "ok": b is not None and l is not None and l >= b})
    return {"ok": all(r["ok"] for r in rows), "tables": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sports_ai.diagnostics.football_sqlite_backup", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", help="online backup of every Football DB into a new directory")
    b.add_argument("--source-dir", required=True)
    b.add_argument("--dest-dir", required=True)
    v = sub.add_parser("verify", help="re-check a backup directory against its manifest")
    v.add_argument("--backup-dir", required=True)
    r = sub.add_parser("reconcile", help="compare backup row counts with a live/restored directory")
    r.add_argument("--backup-dir", required=True)
    r.add_argument("--live-dir", required=True)
    args = parser.parse_args(argv)
    if args.command == "backup":
        result = run_backup(Path(args.source_dir), Path(args.dest_dir))
        ok = result["all_integrity_ok"]
    elif args.command == "verify":
        result = verify_backup(Path(args.backup_dir))
        ok = result["ok"]
    else:
        result = reconcile(Path(args.backup_dir), Path(args.live_dir))
        ok = result["ok"]
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
