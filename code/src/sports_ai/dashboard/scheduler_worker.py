"""Scheduler-only runtime for the operational football dashboard."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import replace

from . import storage
from .live_production_services import LiveProductionDashboardServices
from .models import utc_now_iso
from .production_services import ProductionDashboardServices
from .scoped_fixture_provider import ScopedApiFootballFixtureProvider
from .services import DashboardConfig, DashboardServices, RUNTIME_ROLE_SCHEDULER

logger = logging.getLogger(__name__)
MODEL_SNAPSHOT_STATE_KEY = "model_snapshot_state"


def _test_block_seconds() -> float:
    raw = os.environ.get("SPORTS_AI_SCHEDULER_WORKER_TEST_BLOCK_SECONDS", "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, value)


def _persist_model_snapshot_state(services: DashboardServices) -> None:
    """Publish exact scheduler-owned model metadata for the read-only web child."""
    production_persist = getattr(services, "_persist_model_snapshot_state", None)
    if callable(production_persist) and isinstance(services, ProductionDashboardServices):
        production_persist()
        return
    snapshot = services.snapshot
    if snapshot is None:
        return
    route_a = dict(snapshot.diagnostics.get("route_a") or {})
    payload = {
        "snapshot_id": snapshot.snapshot_id,
        "model_as_of": snapshot.model_as_of,
        "supported_competitions": list(snapshot.supported_competitions),
        "source_match_cutoff": snapshot.diagnostics.get("model_as_of"),
        "model_version": snapshot.model_version,
        "route_a_status": route_a.get("overall_status"),
        "route_a_artifact_id": route_a.get("artifact_id"),
    }
    updated_at = utc_now_iso()
    with storage.connect(services.config.dashboard_db_path) as conn:
        storage.put_runtime_state(
            conn,
            state_key=MODEL_SNAPSHOT_STATE_KEY,
            state_value=payload,
            updated_at=updated_at,
            initialize_schema=False,
        )


def _production_fixture_provider() -> ScopedApiFootballFixtureProvider | None:
    provider = ScopedApiFootballFixtureProvider()
    return provider if provider.configured() else None


async def run_worker() -> int:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            signal.signal(signum, lambda _signum, _frame: loop.call_soon_threadsafe(stop_event.set))

    config = replace(DashboardConfig.default(), runtime_role=RUNTIME_ROLE_SCHEDULER)
    services = LiveProductionDashboardServices(
        config=config,
        fixture_provider=_production_fixture_provider(),
    )
    await services.initialize()
    _persist_model_snapshot_state(services)

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
