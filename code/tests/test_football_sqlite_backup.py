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
        _wal_db(src / "football_research_live.sqlite", "football_events", 70),
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
    for name in fb.DATABASES:
        if name != "sports_edge_auth.sqlite":
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


def test_research_db_is_required_and_backed_up(data_dir, tmp_path):
    assert "football_research_live.sqlite" in fb.DATABASES
    manifest = fb.run_backup(data_dir, tmp_path / "backup")
    assert manifest["databases"]["football_research_live.sqlite"]["row_counts"]["football_events"] == 70
    assert manifest["schema"] == "football_sqlite_backup_v2"
    research = next(r for r in fb.reconcile(tmp_path / "backup", data_dir)["tables"] if r["table"] == "football_events")
    assert research == {"database": "football_research_live.sqlite", "table": "football_events", "backup": 70, "live": 70, "ok": True}


def test_missing_research_db_fails_closed(tmp_path):
    src = tmp_path / "no-research"
    src.mkdir()
    for name in fb.DATABASES:
        if name != "football_research_live.sqlite":
            _wal_db(src / name, "t", 1).close()
    manifest = fb.run_backup(src, tmp_path / "backup")
    assert manifest["missing_required"] == ["football_research_live.sqlite"]
    assert manifest["all_integrity_ok"] is False
    assert fb.verify_backup(tmp_path / "backup")["ok"] is False


def test_unknown_sqlite_file_fails_closed_before_copying(data_dir, tmp_path):
    (data_dir / "nested").mkdir()
    _wal_db(data_dir / "nested" / "mystery.sqlite", "t", 3).close()
    out = tmp_path / "backup"
    with pytest.raises(fb.UnknownDatabaseError, match="nested/mystery.sqlite"):
        fb.run_backup(data_dir, out)
    assert not out.exists()  # nothing copied

    assert fb.main(["backup", "--source-dir", str(data_dir), "--dest-dir", str(out)]) == 1
    assert not out.exists()


def test_unknown_sqlite_file_can_be_included_or_acknowledged(data_dir, tmp_path):
    _wal_db(data_dir / "mystery.sqlite", "t", 3).close()
    included = fb.run_backup(data_dir, tmp_path / "with", include_unknown=True)
    assert included["unknown_included"] == ["mystery.sqlite"]
    assert included["databases"]["mystery.sqlite"]["row_counts"] == {"t": 3}
    assert fb.verify_backup(tmp_path / "with")["ok"] is True

    skipped = fb.run_backup(data_dir, tmp_path / "skip", skip=["mystery.sqlite"])
    assert skipped["skipped_acknowledged"] == ["mystery.sqlite"]
    assert "mystery.sqlite" not in skipped["databases"]
    assert skipped["inventory"]["mystery.sqlite"]["known"] is False
    assert fb.verify_backup(tmp_path / "skip")["ok"] is True

    with pytest.raises(ValueError):
        fb.run_backup(data_dir, tmp_path / "bad", skip=["football_research_live.sqlite"])


def test_inventory_counts_wal_bytes(data_dir):
    found = fb.inventory(data_dir)
    assert found["unknown"] == []
    assert set(found["files"]) == set(fb.DATABASES)
    assert found["files"]["football_paper_trading.sqlite"]["wal_bytes"] > 0


def test_insufficient_space_fails_closed_before_copying(data_dir, tmp_path, monkeypatch):
    real = fb.shutil.disk_usage
    monkeypatch.setattr(fb.shutil, "disk_usage", lambda path: real(path)._replace(free=1024))
    out = tmp_path / "backup"
    with pytest.raises(fb.InsufficientSpaceError):
        fb.run_backup(data_dir, out)
    assert not out.exists()
    assert fb.main(["inventory", "--source-dir", str(data_dir), "--dest-dir", str(out)]) == 1


def test_capacity_is_recorded(data_dir, tmp_path):
    manifest = fb.run_backup(data_dir, tmp_path / "backup")
    capacity = manifest["capacity"]
    expected = sum(f["bytes"] + f["wal_bytes"] for f in manifest["inventory"].values())
    assert capacity["source_bytes"] == expected
    assert capacity["required_free_bytes"] == int(expected * fb.CAPACITY_SAFETY_FACTOR) + fb.CAPACITY_MARGIN_BYTES
    assert capacity["free_bytes"] >= capacity["required_free_bytes"]


def test_checksums_file_matches_copies(data_dir, tmp_path):
    out = tmp_path / "backup"
    manifest = fb.run_backup(data_dir, out)
    raw = (out / fb.CHECKSUMS_NAME).read_bytes()
    assert b"\r" not in raw  # `sha256sum -c` format on every platform
    lines = raw.decode("utf-8").splitlines()
    parsed = dict(reversed(line.split("  ", 1)) for line in lines)
    assert parsed == {name: info["sha256"] for name, info in manifest["databases"].items()}
    for name, digest in parsed.items():
        assert fb._sha256(out / name) == digest


def test_verify_rejects_manifest_without_research_db(data_dir, tmp_path):
    out = tmp_path / "backup"
    fb.run_backup(data_dir, out)
    manifest = json.loads((out / fb.MANIFEST_NAME).read_text(encoding="utf-8"))
    del manifest["databases"]["football_research_live.sqlite"]
    del manifest["inventory"]["football_research_live.sqlite"]
    (out / fb.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    result = fb.verify_backup(out)
    assert result["ok"] is False
    assert "football_research_live.sqlite: required database absent from manifest" in result["problems"]


def test_reconcile_skips_live_integrity_check_by_default(data_dir, tmp_path, monkeypatch):
    out = tmp_path / "backup"
    fb.run_backup(data_dir, out)
    calls: list[bool] = []
    real = fb.describe

    def spy(path, *, integrity_check=True):
        calls.append(integrity_check)
        return real(path, integrity_check=integrity_check)

    monkeypatch.setattr(fb, "describe", spy)
    assert fb.reconcile(out, data_dir)["ok"] is True
    assert calls and not any(calls)
    calls.clear()
    assert fb.reconcile(out, data_dir, integrity_check=True)["ok"] is True
    assert calls and all(calls)


def test_ledger_is_copied_before_the_databases_it_references():
    order = list(fb.DATABASES)
    ledger = order.index("football_model_picks.sqlite")
    assert ledger < order.index("football_dashboard.sqlite")
    assert ledger < order.index("football_paper_trading.sqlite")
