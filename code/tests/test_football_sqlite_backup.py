from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from sports_ai.diagnostics import football_sqlite_backup as fb


def _wal_db(path: Path, table: str, rows: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")  # keep committed rows in the -wal file
    conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany(f"INSERT INTO {table} (v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
    conn.commit()
    return conn


@pytest.fixture
def data_dir(tmp_path):
    src = tmp_path / "data"
    src.mkdir()
    conns = [
        _wal_db(src / "sports_edge_auth.sqlite", "users", 2),
        _wal_db(src / "football_dashboard.sqlite", "dashboard_fixtures", 50),
        _wal_db(src / "football_paper_trading.sqlite", "market_snapshots", 400),
        _wal_db(src / "football_model_picks.sqlite", "football_model_pick_entries", 30),
    ]
    for table, db in (("dashboard_predictions", "football_dashboard.sqlite"), ("dashboard_fixture_results", "football_dashboard.sqlite"),
                      ("paper_trade_entries", "football_paper_trading.sqlite"), ("paper_trade_updates", "football_paper_trading.sqlite")):
        c = sqlite3.connect(src / db)
        c.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        c.execute(f"INSERT INTO {table} DEFAULT VALUES")
        c.commit()
        c.close()
    yield src
    for conn in conns:
        conn.close()


def _source_bytes(src: Path) -> dict[str, bytes]:
    # -shm is shared memory that readers legitimately touch; db and -wal must not change.
    return {p.name: p.read_bytes() for p in sorted(src.iterdir()) if not p.name.endswith("-shm")}


def test_backup_includes_committed_wal_rows_and_never_touches_sources(data_dir, tmp_path):
    assert (data_dir / "football_dashboard.sqlite-wal").stat().st_size > 0
    before = _source_bytes(data_dir)

    manifest = fb.run_backup(data_dir, tmp_path / "backup")

    assert _source_bytes(data_dir) == before
    assert manifest["all_integrity_ok"] is True
    assert manifest["missing_required"] == []
    assert manifest["databases"]["football_dashboard.sqlite"]["row_counts"]["dashboard_fixtures"] == 50
    assert manifest["databases"]["football_paper_trading.sqlite"]["row_counts"]["market_snapshots"] == 400
    out = tmp_path / "backup"
    assert not any(p.name.endswith(("-wal", "-shm")) for p in out.iterdir())  # self-contained copies
    assert "football_high_confidence_shadow.sqlite" not in manifest["databases"]  # optional, absent
    assert fb.verify_backup(out) == {"ok": True, "problems": []}


def test_manifest_holds_counts_and_hashes_only(data_dir, tmp_path):
    fb.run_backup(data_dir, tmp_path / "backup")
    text = (tmp_path / "backup" / fb.MANIFEST_NAME).read_text(encoding="utf-8")
    assert str(data_dir) not in text and str(tmp_path) not in text
    assert "r1" not in text  # no row contents


def test_backup_is_consistent_under_a_concurrent_writer(data_dir, tmp_path):
    stop = threading.Event()

    def writer():
        conn = sqlite3.connect(data_dir / "football_paper_trading.sqlite", timeout=30)
        while not stop.is_set():
            conn.execute("INSERT INTO market_snapshots (v) VALUES ('w')")
            conn.commit()
        conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        manifest = fb.run_backup(data_dir, tmp_path / "backup")
    finally:
        stop.set()
        thread.join()
    assert manifest["all_integrity_ok"] is True
    assert manifest["databases"]["football_paper_trading.sqlite"]["row_counts"]["market_snapshots"] >= 400
    assert fb.verify_backup(tmp_path / "backup")["ok"] is True


def test_verify_detects_tampering_and_missing_files(data_dir, tmp_path):
    out = tmp_path / "backup"
    fb.run_backup(data_dir, out)
    conn = sqlite3.connect(out / "football_model_picks.sqlite")
    conn.execute("DELETE FROM football_model_pick_entries WHERE id = 1")
    conn.commit()
    conn.close()
    (out / "sports_edge_auth.sqlite").unlink()
    result = fb.verify_backup(out)
    assert result["ok"] is False
    assert "football_model_picks.sqlite: sha256 mismatch" in result["problems"]
    assert "football_model_picks.sqlite: row counts differ" in result["problems"]
    assert "sports_edge_auth.sqlite: missing" in result["problems"]


def test_missing_required_database_fails_the_backup(tmp_path):
    src = tmp_path / "partial"
    src.mkdir()
    for name in fb.DATABASES[1:]:
        _wal_db(src / name, "t", 1).close()
    manifest = fb.run_backup(src, tmp_path / "backup")
    assert manifest["missing_required"] == ["sports_edge_auth.sqlite"]
    assert manifest["all_integrity_ok"] is False
    assert fb.verify_backup(tmp_path / "backup")["ok"] is False


def test_refuses_destination_inside_source_or_existing(data_dir, tmp_path):
    with pytest.raises(ValueError):
        fb.run_backup(data_dir, data_dir / "backups")
    (tmp_path / "exists").mkdir()
    with pytest.raises(FileExistsError):
        fb.run_backup(data_dir, tmp_path / "exists")


def test_reconcile_accepts_forward_growth_and_flags_loss(data_dir, tmp_path):
    out = tmp_path / "backup"
    fb.run_backup(data_dir, out)
    conn = sqlite3.connect(data_dir / "football_model_picks.sqlite")
    conn.execute("INSERT INTO football_model_pick_entries (v) VALUES ('new')")
    conn.commit()
    conn.close()
    assert fb.reconcile(out, data_dir)["ok"] is True
    conn = sqlite3.connect(data_dir / "football_dashboard.sqlite")
    conn.execute("DELETE FROM dashboard_fixtures WHERE id <= 3")
    conn.commit()
    conn.close()
    result = fb.reconcile(out, data_dir)
    assert result["ok"] is False
    bad = [r for r in result["tables"] if not r["ok"]]
    assert bad == [{"database": "football_dashboard.sqlite", "table": "dashboard_fixtures", "backup": 50, "live": 47, "ok": False}]


def test_cli_exit_codes(data_dir, tmp_path, capsys):
    out = tmp_path / "backup"
    assert fb.main(["backup", "--source-dir", str(data_dir), "--dest-dir", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["all_integrity_ok"] is True
    assert fb.main(["verify", "--backup-dir", str(out)]) == 0
    capsys.readouterr()
    assert fb.main(["reconcile", "--backup-dir", str(out), "--live-dir", str(data_dir)]) == 0
