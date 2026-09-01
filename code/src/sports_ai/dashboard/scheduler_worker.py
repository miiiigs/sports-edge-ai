"""Scheduler-only runtime for the operational football dashboard."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import replace

from .services import DashboardConfig, DashboardServices, RUNTIME_ROLE_SCHEDULER

logger = logging.getLogger(__name__)


def _test_block_seconds() -> float:
    raw = os.environ.get("SPORTS_AI_SCHEDULER_WORKER_TEST_BLOCK_SECONDS", "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, value)


async def run_worker() -> int:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            signal.signal(signum, lambda _signum, _frame: loop.call_soon_threadsafe(stop_event.set))

    config = replace(DashboardConfig.default(), runtime_role=RUNTIME_ROLE_SCHEDULER)
    services = DashboardServices(config=config)
    await services.initialize()

    block_seconds = _test_block_seconds()
    if block_seconds > 0:
        await asyncio.to_thread(time.sleep, block_seconds)

    try:
        await stop_event.wait()
    finally:
        await services.shutdown()
    logger.info("scheduler worker stopped")
    return 0


def main() -> int:
    logging.basicConfig(level=os.environ.get("SPORTS_AI_LOG_LEVEL", "INFO"))
    return asyncio.run(run_worker())


if __name__ == "__main__":
    raise SystemExit(main())
