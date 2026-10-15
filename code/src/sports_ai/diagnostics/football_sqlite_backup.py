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
  restore (also off-volume, on the downloaded copy; ``SHA256SUMS`` lets
  ``sha256sum -c`` confirm the transfer without Python).
* Before copying, every ``*.sqlite`` under the source directory is inventoried.
  A database the tool does not know about fails the backup closed unless it is
  explicitly included (``--include-unknown``) or acknowledged as skipped
  (``--skip NAME``), so nothing is silently left out.
* Before copying, free space at the destination must cover the sources
  (database + ``-wal``) times a safety factor; otherwise nothing is copied.

The databases are separate files with no cross-database transaction, so
the copies are taken back to back in a fixed order; take them while the app is
sleeping or with every writer flag OFF for a quiescent set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "football_sqlite_backup_v2"
CHECKSUMS_NAME = "SHA256SUMS"

# Fixed order: auth first, ledger last (its picks reference fixtures/predictions).
DATABASES: tuple[str, ...] = (
    "sports_edge_auth.sqlite",
    # Persistent model history: seeded from the bundled baseline, then refreshed
    # (CSV divisions) and extended by paid API-Football backfills (e.g. FRA1).
    "football_research_live.sqlite",
    "football_dashboard.sqlite",
    "football_paper_trading.sqlite",
    "football_model_picks.sqlite",
)
OPTIONAL_DATABASES: tuple[str, ...] = ("football_high_confidence_shadow.sqlite",)
SQLITE_SUFFIXES: tuple[str, ...] = (".sqlite", ".sqlite3", ".db")

# Destination free space must cover source bytes x factor + margin before copying.
CAPACITY_SAFETY_FACTOR = 1.2
CAPACITY_MARGIN_BYTES = 64 * 1024 * 1024

# Tables whose counts are reconciled after backup and after a restore.
RECONCILED_TABLES: dict[str, tuple[str, ...]] = {
    "football_dashboard.sqlite": ("dashboard_fixtures", "dashboard_predictions", "dashboard_fixture_results"),
    "football_paper_trading.sqlite": ("paper_trade_entries", "paper_trade_updates", "market_snapshots"),
    "football_model_picks.sqlite": ("football_model_pick_entries",),
    "football_research_live.sqlite": ("football_events",),
}


class InsufficientSpaceError(RuntimeError):
    pass


class UnknownDatabaseError(RuntimeError):
    pass


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


def describe(path: Path, *, integrity_check: bool = True) -> dict[str, Any]:
    """Integrity result and per-table row counts of one SQLite file (read-only)."""
    conn = _ro(path)
    try:
        integrity = (
            [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
            if integrity_check
            else ["skipped"]
        )
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in tables}
    finally:
        conn.close()
    if integrity == ["skipped"]:
        return {"integrity_check": "skipped", "row_counts": counts}
    return {"integrity_check": "ok" if integrity == ["ok"] else integrity[:20], "row_counts": counts}


def _sidecar_bytes(path: Path, suffix: str) -> int:
    sidecar = path.with_name(path.name + suffix)
    return sidecar.stat().st_size if sidecar.exists() else 0


def inventory(source_dir: Path) -> dict[str, Any]:
    """Every SQLite-looking file under ``source_dir`` (recursive) with its size, read-only.

    Names are relative to ``source_dir``; ``unknown`` lists files that are not in
    the required or optional inventory.
    """
    source_dir = Path(source_dir)
    known = set(DATABASES + OPTIONAL_DATABASES)
    files: dict[str, Any] = {}
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SQLITE_SUFFIXES:
            continue
        name = path.relative_to(source_dir).as_posix()
        files[name] = {
            "bytes": path.stat().st_size,
            "wal_bytes": _sidecar_bytes(path, "-wal"),
            "shm_present": path.with_name(path.name + "-shm").exists(),
            "known": name in known,
        }
    return {"files": files, "unknown": [name for name, info in files.items() if not info["known"]]}


def _existing_ancestor(path: Path) -> Path:
    probe = path.resolve()
    while not probe.exists():
        probe = probe.parent
    return probe


def check_capacity(source_bytes: int, destination_dir: Path) -> dict[str, Any]:
    """Fail closed unless the destination filesystem can hold the copies."""
    required = int(source_bytes * CAPACITY_SAFETY_FACTOR) + CAPACITY_MARGIN_BYTES
    free = int(shutil.disk_usage(_existing_ancestor(Path(destination_dir))).free)
    result = {"source_bytes": int(source_bytes), "required_free_bytes": required, "free_bytes": free}
    if free < required:
        raise InsufficientSpaceError(
            f"destination has {free} bytes free; {required} required for {source_bytes} source bytes"
        )
    return result


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


def run_backup(
    source_dir: Path,
    destination_dir: Path,
    *,
    include_unknown: bool = False,
    skip: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    source_dir = Path(source_dir)
    destination_dir = Path(destination_dir)
    if destination_dir.resolve() == source_dir.resolve() or source_dir.resolve() in destination_dir.resolve().parents:
        raise ValueError("destination must be outside the source data directory")
    if destination_dir.exists():
        raise FileExistsError(f"refusing to reuse existing directory {destination_dir.name}")
    skipped = sorted(set(skip))
    if set(skipped) & set(DATABASES):
        raise ValueError("required databases cannot be skipped")
    found = inventory(source_dir)
    unknown = [name for name in found["unknown"] if name not in skipped]
    if unknown and not include_unknown:
        raise UnknownDatabaseError(
            "unrecognised SQLite files in the source directory: "
            + ", ".join(unknown)
            + " (back them up with --include-unknown or acknowledge with --skip NAME)"
        )
    to_copy = [name for name in DATABASES + OPTIONAL_DATABASES if name in found["files"] and name not in skipped]
    to_copy += unknown
    source_bytes = sum(found["files"][name]["bytes"] + found["files"][name]["wal_bytes"] for name in to_copy)
    capacity = check_capacity(source_bytes, destination_dir)

    destination_dir.mkdir(parents=True, exist_ok=False)
    started = _utc_now()
    databases: dict[str, Any] = {}
    missing = [name for name in DATABASES if name not in found["files"]]
    for name in to_copy:
        target = destination_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        databases[name] = {"copied_at": _utc_now(), **backup_database(source_dir / name, target)}
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "started_at": started,
        "completed_at": _utc_now(),
        "databases": databases,
        "missing_required": missing,
        "inventory": found["files"],
        "unknown_included": unknown,
        "skipped_acknowledged": skipped,
        "capacity": capacity,
        "all_integrity_ok": all(d["integrity_check"] == "ok" for d in databases.values()) and not missing,
    }
    (destination_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    # `sha256sum -c SHA256SUMS` re-checks a downloaded copy without Python.
    with (destination_dir / CHECKSUMS_NAME).open("w", encoding="utf-8", newline="\n") as handle:
        handle.writelines(f"{info['sha256']}  {name}\n" for name, info in sorted(databases.items()))
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
    # A manifest from an older inventory (e.g. v1, without the research DB) is incomplete.
    problems.extend(
        f"{name}: required database absent from manifest"
        for name in DATABASES
        if name not in manifest["databases"] and name not in manifest.get("missing_required", [])
    )
    for name in manifest.get("inventory", {}):
        if name not in manifest["databases"] and name not in manifest.get("skipped_acknowledged", []):
            problems.append(f"{name}: inventoried but not copied")
    return {"ok": not problems, "problems": problems}


def reconcile(backup_dir: Path, live_dir: Path, *, integrity_check: bool = False) -> dict[str, Any]:
    """Compare reconciled-table counts of a backup with a live (or restored) directory, read-only.

    Live counts may only be >= the backup's (forward-only writes after the backup).
    The live side gets row counts only; ``integrity_check`` (a full read of every
    page) is opt-in because it is heavy on a production volume.
    """
    manifest = json.loads((Path(backup_dir) / MANIFEST_NAME).read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for name, tables in RECONCILED_TABLES.items():
        backed = manifest["databases"].get(name, {}).get("row_counts", {})
        live_path = Path(live_dir) / name
        live = describe(live_path, integrity_check=integrity_check)["row_counts"] if live_path.exists() else {}
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
    b.add_argument("--include-unknown", action="store_true", help="also copy unrecognised SQLite files")
    b.add_argument("--skip", action="append", default=[], help="acknowledge an unrecognised file as not backed up")
    i = sub.add_parser("inventory", help="list every SQLite file under a directory with sizes (read-only)")
    i.add_argument("--source-dir", required=True)
    i.add_argument("--dest-dir", help="also check that this destination has enough free space")
    v = sub.add_parser("verify", help="re-check a backup directory against its manifest")
    v.add_argument("--backup-dir", required=True)
    r = sub.add_parser("reconcile", help="compare backup row counts with a live/restored directory")
    r.add_argument("--backup-dir", required=True)
    r.add_argument("--live-dir", required=True)
    r.add_argument("--integrity-check", action="store_true", help="also run PRAGMA integrity_check on the live side")
    args = parser.parse_args(argv)
    if args.command == "backup":
        try:
            result = run_backup(
                Path(args.source_dir), Path(args.dest_dir), include_unknown=args.include_unknown, skip=args.skip
            )
        except (InsufficientSpaceError, UnknownDatabaseError) as exc:
            result = {"all_integrity_ok": False, "error": str(exc)}
        ok = bool(result["all_integrity_ok"])
    elif args.command == "inventory":
        result = inventory(Path(args.source_dir))
        ok = True
        if args.dest_dir:
            source_bytes = sum(f["bytes"] + f["wal_bytes"] for f in result["files"].values())
            try:
                result["capacity"] = check_capacity(source_bytes, Path(args.dest_dir))
            except InsufficientSpaceError as exc:
                result["capacity"] = {"error": str(exc)}
                ok = False
    elif args.command == "verify":
        result = verify_backup(Path(args.backup_dir))
        ok = result["ok"]
    else:
        result = reconcile(Path(args.backup_dir), Path(args.live_dir), integrity_check=args.integrity_check)
        ok = result["ok"]
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
