"""Production supervisor that isolates web serving from scheduler work."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .services import DEFAULT_RUNTIME_ROLE_ENV_VAR, RUNTIME_ROLE_SCHEDULER, RUNTIME_ROLE_WEB

WEB_READINESS_TIMEOUT_SECONDS = 30.0
MEMORY_TELEMETRY_FIRST_SAMPLE_SECONDS = 30.0
MEMORY_TELEMETRY_INTERVAL_SECONDS = 300.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: list[str]
    env: dict[str, str]
    readiness_url: str | None = None


def build_child_specs(*, port: str | None = None) -> list[ChildSpec]:
    resolved_port = port or os.environ.get("PORT", "8008")
    base_env = dict(os.environ)
    web_env = {**base_env, DEFAULT_RUNTIME_ROLE_ENV_VAR: RUNTIME_ROLE_WEB}
    scheduler_env = {**base_env, DEFAULT_RUNTIME_ROLE_ENV_VAR: RUNTIME_ROLE_SCHEDULER}
    return [
        ChildSpec(
            name="web",
            argv=[
                sys.executable,
                "-m",
                "uvicorn",
                "sports_ai.dashboard.owner_access:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(resolved_port),
            ],
            env=web_env,
            readiness_url=f"http://127.0.0.1:{resolved_port}/health",
        ),
        ChildSpec(
            name="scheduler",
            argv=[sys.executable, "-m", "sports_ai.dashboard.scheduler_worker"],
            env=scheduler_env,
        ),
    ]


def _terminate_children(children: list[subprocess.Popen]) -> None:
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 10
    for child in children:
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
    for child in children:
        if child.poll() is None:
            child.kill()


def _wait_for_web_readiness(
    child: subprocess.Popen,
    readiness_url: str,
    *,
    timeout_seconds: float = WEB_READINESS_TIMEOUT_SECONDS,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        try:
            with urlopen(readiness_url, timeout=1.0) as response:
                if response.status == 200:
                    return True
        except (OSError, URLError):
            pass
        time.sleep(0.2)
    return False


def _read_linux_process_memory_kib(pid: int) -> dict[str, int] | None:
    """Read a small, side-effect-free Linux memory snapshot for one child process.

    Railway containers expose Linux ``/proc``. Returning ``None`` on other platforms
    or after a child exits keeps this diagnostic strictly optional. Values are kept
    in KiB to match the kernel source without rounding during collection.
    """
    status_path = Path(f"/proc/{pid}/status")
    try:
        lines = status_path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return None

    wanted = {"VmRSS", "VmHWM", "RssAnon", "RssFile", "RssShmem"}
    result: dict[str, int] = {}
    for line in lines:
        key, separator, remainder = line.partition(":")
        if not separator or key not in wanted:
            continue
        value_text = remainder.strip().split()[0]
        try:
            result[key] = int(value_text)
        except (TypeError, ValueError):
            continue
    return result or None


def _log_child_memory(children: list[tuple[str, subprocess.Popen]]) -> None:
    for name, child in children:
        if child.poll() is not None:
            continue
        memory = _read_linux_process_memory_kib(child.pid)
        if memory is None:
            continue
        logger.info(
            "runtime child memory: child=%s pid=%s rss_mib=%.1f hwm_mib=%.1f anon_mib=%.1f file_mib=%.1f shmem_mib=%.1f",
            name,
            child.pid,
            memory.get("VmRSS", 0) / 1024.0,
            memory.get("VmHWM", 0) / 1024.0,
            memory.get("RssAnon", 0) / 1024.0,
            memory.get("RssFile", 0) / 1024.0,
            memory.get("RssShmem", 0) / 1024.0,
        )


def main() -> int:
    specs = build_child_specs()
    children: list[subprocess.Popen] = []
    named_children: list[tuple[str, subprocess.Popen]] = []
    stopping = False

    def _handle_signal(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        _terminate_children(children)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        web_spec = specs[0]
        scheduler_specs = specs[1:]
        web_child = subprocess.Popen(web_spec.argv, env=web_spec.env)
        children.append(web_child)
        named_children.append((web_spec.name, web_child))
        if web_spec.readiness_url is not None and not _wait_for_web_readiness(web_child, web_spec.readiness_url):
            _terminate_children(children)
            return 1
        for spec in scheduler_specs:
            child = subprocess.Popen(spec.argv, env=spec.env)
            children.append(child)
            named_children.append((spec.name, child))

        next_memory_sample = time.monotonic() + MEMORY_TELEMETRY_FIRST_SAMPLE_SECONDS
        while True:
            for child in children:
                return_code = child.poll()
                if return_code is not None:
                    _terminate_children(children)
                    return 0 if stopping else 1
            now = time.monotonic()
            if now >= next_memory_sample:
                _log_child_memory(named_children)
                next_memory_sample = now + MEMORY_TELEMETRY_INTERVAL_SECONDS
            time.sleep(0.5)
    finally:
        _terminate_children(children)


if __name__ == "__main__":
    raise SystemExit(main())
