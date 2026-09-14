from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sports_ai.dashboard import runtime_supervisor


def test_read_linux_process_memory_kib_parses_status(monkeypatch) -> None:
    payload = """Name:\tpython\nVmHWM:\t  204800 kB\nVmRSS:\t  153600 kB\nRssAnon:\t  120000 kB\nRssFile:\t   33000 kB\nRssShmem:\t     600 kB\n"""

    monkeypatch.setattr(Path, "read_text", lambda self, encoding="utf-8": payload)

    result = runtime_supervisor._read_linux_process_memory_kib(1234)

    assert result == {
        "VmHWM": 204800,
        "VmRSS": 153600,
        "RssAnon": 120000,
        "RssFile": 33000,
        "RssShmem": 600,
    }


def test_read_linux_process_memory_kib_is_optional_when_proc_unavailable(monkeypatch) -> None:
    def missing(self, encoding="utf-8"):
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", missing)

    assert runtime_supervisor._read_linux_process_memory_kib(4321) is None


def test_log_child_memory_skips_exited_and_unavailable_children(monkeypatch, caplog) -> None:
    running = SimpleNamespace(pid=11, poll=lambda: None)
    exited = SimpleNamespace(pid=22, poll=lambda: 0)
    unavailable = SimpleNamespace(pid=33, poll=lambda: None)

    def read_memory(pid: int):
        if pid == 11:
            return {
                "VmRSS": 102400,
                "VmHWM": 122880,
                "RssAnon": 81920,
                "RssFile": 19456,
                "RssShmem": 1024,
            }
        return None

    monkeypatch.setattr(runtime_supervisor, "_read_linux_process_memory_kib", read_memory)

    with caplog.at_level("INFO"):
        runtime_supervisor._log_child_memory(
            [("web", running), ("exited", exited), ("scheduler", unavailable)]
        )

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "process=web" in messages[0]
    assert "rss_mib=100.0" in messages[0]
    assert "hwm_mib=120.0" in messages[0]
