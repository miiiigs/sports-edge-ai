"""Production supervisor that isolates web serving from scheduler work."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

from .services import DEFAULT_RUNTIME_ROLE_ENV_VAR, RUNTIME_ROLE_SCHEDULER, RUNTIME_ROLE_WEB


@dataclass(frozen=True)
class ChildSpec:
    name: str
    argv: list[str]
    env: dict[str, str]


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
                "sports_ai.dashboard.app:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(resolved_port),
            ],
            env=web_env,
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
        for spec in specs:
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
