"""Scheduler-only runtime for the operational football dashboard."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import replace

from ..market_data.five_dollar_shadow_runtime import FiveDollarShadowRuntime
from . import storage
from .live_production_services import LiveProductionDashboardServices
from .models import utc_now_iso
from .production_services import ProductionDashboardServices
from .scoped_fixture_provider import ScopedApiFootballFixtureProvider
from .services import RUNTIME_ROLE_SCHEDULER, DashboardConfig, DashboardServices

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


async def _run_shadow_runtime(runtime: FiveDollarShadowRuntime, stop_event: asyncio.Event) -> None:
    """Isolated, long-lived FRA1 shadow loop. No-op (no calls, no writes) when off."""
    if not runtime.enabled():
        logger.info("5dollar shadow: state=disabled (provider and/or shadow flag off); runtime idle")
        return
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(runtime.run_cycle)
        except Exception:
            logger.exception("5dollar shadow cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=runtime.interval_seconds)
        except asyncio.TimeoutError:
            continue
        else:
            return


async def _run_market_collection_runtime(services: DashboardServices, stop_event: asyncio.Event) -> None:
    """Isolated, recurring collection-only market loop. No-op when disabled.

    Independent of the global automated scheduler (SPORTS_AI_ENABLE_AUTOMATED_SYNC).
    Runs sequentially (never overlapping) and only invokes the collection-only
    market path: no fixture sync, prediction generation, research refresh,
    evaluation, paper entries, portfolio, corner-result sync or settlement.
    """
    if not services.market_collection_scheduler_active():
        logger.info("market collection scheduler: state=disabled")
        return
    interval = max(1, int(services.config.market_collection_interval_seconds))
    logger.info("market collection scheduler started: interval_seconds=%s", interval)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(services.run_market_collection_only_cycle)
        except Exception:
            logger.exception("market collection cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
        else:
            return


async def _run_result_sync_runtime(services: DashboardServices, stop_event: asyncio.Event) -> None:
    """Isolated, recurring current-result (sporting truth) loop. No-op when disabled.

    Independent of the global scheduler and the market collector. Runs
    sequentially and only refreshes provider status/score and authoritative
    corner truth for bounded current/recent fixtures: no fixture sync,
    prediction generation, evaluation, paper entries, portfolio or settlement.
    """
    if not services.result_sync_scheduler_active():
        logger.info("result sync scheduler: state=disabled")
        return
    interval = max(1, int(services.config.result_sync_interval_seconds))
    logger.info("result sync scheduler started: interval_seconds=%s", interval)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(services.run_current_result_sync_cycle)
        except Exception:
            logger.exception("result sync cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
        else:
            return


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

    # Isolated shadow runtime: one long-lived collector for the process lifetime.
    # The DB is opened ONLY when the runtime is enabled, so a disabled runtime
    # performs zero provider calls and zero writes.
    shadow_runtime = FiveDollarShadowRuntime()
    if shadow_runtime.enabled():
        shadow_runtime.attach_connection(services.config.paper_trading_db_path)
    shadow_task = asyncio.create_task(_run_shadow_runtime(shadow_runtime, stop_event))
    market_collection_task = asyncio.create_task(
        _run_market_collection_runtime(services, stop_event)
    )
    result_sync_task = asyncio.create_task(
        _run_result_sync_runtime(services, stop_event)
    )

    block_seconds = _test_block_seconds()
    if block_seconds > 0:
        await asyncio.to_thread(time.sleep, block_seconds)

    try:
        await stop_event.wait()
    finally:
        shadow_task.cancel()
        market_collection_task.cancel()
        result_sync_task.cancel()
        for task in (shadow_task, market_collection_task, result_sync_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        shadow_runtime.close()
        await services.shutdown()
    logger.info("scheduler worker stopped")
    return 0


def main() -> int:
    logging.basicConfig(level=os.environ.get("SPORTS_AI_LOG_LEVEL", "INFO"))
    # httpx logs the full request URL at INFO. Some provider APIs require
    # credentials in query parameters, so allowing those records into production
    # logs can disclose secrets. Keep transport internals at WARNING while leaving
    # application-level provider diagnostics at the configured log level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return asyncio.run(run_worker())


if __name__ == "__main__":
    raise SystemExit(main())
