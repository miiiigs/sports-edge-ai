"""Production supervisor that isolates web serving from scheduler work."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import urlopen

from .services import DEFAULT_RUNTIME_ROLE_ENV_VAR, RUNTIME_ROLE_SCHEDULER, RUNTIME_ROLE_WEB

WEB_READINESS_TIMEOUT_SECONDS = 30.0


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


def main() -> int:
    specs = build_child_specs()
    children: list[subprocess.Popen] = []
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
        if web_spec.readiness_url is not None and not _wait_for_web_readiness(web_child, web_spec.readiness_url):
            _terminate_children(children)
            return 1
        for spec in scheduler_specs:
            children.append(subprocess.Popen(spec.argv, env=spec.env))
        while True:
            for child in children:
                return_code = child.poll()
                if return_code is not None:
                    _terminate_children(children)
                    return 0 if stopping else 1
            time.sleep(0.5)
    finally:
        _terminate_children(children)


if __name__ == "__main__":
    raise SystemExit(main())
