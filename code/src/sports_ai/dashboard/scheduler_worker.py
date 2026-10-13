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


async def _run_model_pick_settlement_runtime(services: DashboardServices, stop_event: asyncio.Event) -> None:
    """Isolated, recurring, DB-only Model Pick settlement loop. No-op when disabled.

    Consumes already-persisted verified sporting truth. Zero provider requests,
    zero paper-ledger writes, zero Value Bets. Runs sequentially.
    """
    if not services.model_pick_settlement_scheduler_active():
        logger.info("model pick settlement scheduler: state=disabled")
        return
    interval = max(1, int(services.config.model_pick_settlement_interval_seconds))
    logger.info("model pick settlement scheduler started: interval_seconds=%s", interval)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(services.run_model_pick_settlement_cycle)
        except Exception:
            logger.exception("model pick settlement cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
        else:
            return


async def _run_value_bet_evaluation_runtime(services: DashboardServices, stop_event: asyncio.Event) -> None:
    """Isolated, recurring, DB-only frozen-Model-Pick value evaluation loop.

    Evaluation only: no writes of any kind (issuance stays disabled in this lane).
    Zero provider requests. Runs sequentially.
    """
    if not services.value_bet_evaluation_scheduler_active():
        logger.info("value evaluation scheduler: state=disabled")
        return
    interval = max(1, int(services.config.value_bet_evaluation_interval_seconds))
    logger.info("value evaluation scheduler started: interval_seconds=%s", interval)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(services.run_value_bet_evaluation_cycle)
        except Exception:
            logger.exception("value evaluation cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
        else:
            return


async def _wait_for_startup_sync(services: DashboardServices, stop_event: asyncio.Event) -> None:
    """Let the startup fixture/prediction sync finish before DB-only consumers run."""
    task = getattr(services, "_startup_sync_task", None)
    if task is None or task.done():
        return
    stop_wait = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait({task, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_wait.cancel()


async def _run_model_pick_issuance_runtime(services: DashboardServices, stop_event: asyncio.Event) -> None:
    """Isolated, recurring, DB-only forward Model Pick issuance. No-op when disabled.

    Issues LIVE_ISSUED picks only for fixtures that have not kicked off, from the
    first pre-kickoff READY prediction. Zero provider requests. The first cycle
    runs as soon as the startup sync completes so short-lived wakes still issue.
    """
    if not services.model_pick_issuance_scheduler_active():
        logger.info("model pick issuance scheduler: state=disabled")
        return
    interval = max(1, int(services.config.model_pick_issuance_interval_seconds))
    logger.info("model pick issuance scheduler started: interval_seconds=%s", interval)
    await _wait_for_startup_sync(services, stop_event)
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(services.run_model_pick_issuance_cycle)
        except Exception:
            logger.exception("model pick issuance cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue
        else:
            return


async def _run_prediction_refresh_runtime(services: DashboardServices, stop_event: asyncio.Event) -> None:
    """Isolated, recurring fixture + prediction refresh. No-op when disabled.

    Runs the standard fixture sync without its market-sync step (the dedicated
    market collector owns market requests), then an issuance pass so new READY
    predictions become forward Model Picks promptly. The first cycle waits one
    interval because startup already performed a full sync.
    """
    if not services.prediction_refresh_scheduler_active():
        logger.info("prediction refresh scheduler: state=disabled")
        return
    interval = max(1, int(services.config.prediction_refresh_interval_seconds))
    logger.info("prediction refresh scheduler started: interval_seconds=%s", interval)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return
        try:
            await services.run_prediction_refresh_cycle()
            if services.model_pick_issuance_scheduler_active():
                await asyncio.to_thread(services.run_model_pick_issuance_cycle)
        except Exception:
            logger.exception("prediction refresh cycle failed")


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
    model_pick_settlement_task = asyncio.create_task(
        _run_model_pick_settlement_runtime(services, stop_event)
    )
    value_evaluation_task = asyncio.create_task(
        _run_value_bet_evaluation_runtime(services, stop_event)
    )
    model_pick_issuance_task = asyncio.create_task(
        _run_model_pick_issuance_runtime(services, stop_event)
    )
    prediction_refresh_task = asyncio.create_task(
        _run_prediction_refresh_runtime(services, stop_event)
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
        model_pick_settlement_task.cancel()
        value_evaluation_task.cancel()
        model_pick_issuance_task.cancel()
        prediction_refresh_task.cancel()
        for task in (
            shadow_task,
            market_collection_task,
            result_sync_task,
            model_pick_settlement_task,
            value_evaluation_task,
            model_pick_issuance_task,
            prediction_refresh_task,
        ):
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
