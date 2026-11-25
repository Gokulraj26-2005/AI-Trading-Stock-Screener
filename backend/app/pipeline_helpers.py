# backend/app/pipeline_helpers.py
"""
Pipeline lifecycle helpers shared by routers and the app assembler.

Extracted from main.py to avoid circular imports.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict

from fastapi import FastAPI

from app_config import DHAN_ENABLED, USE_DHAN_MOCK
from logger import logger
from config.symbols import TRADING_SYMBOLS


def is_pipeline_task_running(app: FastAPI) -> bool:
    task = getattr(app.state, "orchestrator_task", None)
    return bool(task and not task.done())


def runtime_data_source_mode(app: FastAPI) -> str:
    if DHAN_ENABLED and not USE_DHAN_MOCK and is_pipeline_task_running(app):
        return "real"
    if USE_DHAN_MOCK:
        return "mock"
    if not DHAN_ENABLED:
        return "disabled"
    return "unavailable"


def _handle_orchestrator_task_done(task: asyncio.Task) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        logger.info("Orchestrator background task was cancelled")
        return
    if exc is not None:
        logger.error("Orchestrator background task exited with error: %s", exc)
    else:
        logger.warning("Orchestrator background task completed")


async def start_pipeline_background(app: FastAPI) -> bool:
    if is_pipeline_task_running(app):
        return False

    from services.orchestrator import start_production_pipeline, get_orchestrator

    config = _build_pipeline_config()
    try:
        orchestrator = get_orchestrator(config)
        if hasattr(orchestrator, "register_signal_handlers"):
            orchestrator.register_signal_handlers()
    except Exception as exc:
        logger.warning("Unable to register orchestrator signal handlers: %s", exc)

    task = asyncio.create_task(start_production_pipeline(config))
    task.add_done_callback(_handle_orchestrator_task_done)
    app.state.orchestrator_task = task
    return True


async def stop_pipeline(app: FastAPI) -> None:
    from services.orchestrator import stop_production_pipeline
    try:
        await stop_production_pipeline()
    except Exception as exc:
        logger.warning("Pipeline stop raised: %s", exc)

    # Await the orchestrator task to complete so that a restart does not race
    task = getattr(app.state, "orchestrator_task", None)
    if task and not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Orchestrator task did not stop within 5s; cancelling.")
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        except (asyncio.CancelledError, Exception):
            pass
    app.state.orchestrator_task = None


async def restart_pipeline_background(app: FastAPI) -> bool:
    await stop_pipeline(app)
    return await start_pipeline_background(app)


async def maybe_autostart_pipeline(
    app: FastAPI, token_present: bool, trigger: str
) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "attempted": False,
        "started": False,
        "running": is_pipeline_task_running(app),
        "reason": None,
    }

    if not (DHAN_ENABLED and not USE_DHAN_MOCK):
        from app_config import PRODUCTION_MODE
        state["reason"] = "production-misconfigured" if PRODUCTION_MODE else "live-mode-disabled"
        return state

    if not token_present:
        state["reason"] = "token-missing"
        return state

    if state["running"]:
        state["reason"] = "already-running"
        return state

    cooldown_seconds = 20.0
    now_ts = datetime.utcnow().timestamp()
    last_attempt_ts = float(getattr(app.state, "pipeline_start_attempt_ts", 0.0) or 0.0)
    elapsed = now_ts - last_attempt_ts
    if elapsed < cooldown_seconds:
        state["reason"] = "cooldown"
        state["cooldown_remaining_s"] = round(cooldown_seconds - elapsed, 2)
        return state

    app.state.pipeline_start_attempt_ts = now_ts
    state["attempted"] = True

    try:
        state["started"] = await start_pipeline_background(app)
        state["running"] = is_pipeline_task_running(app)
        state["reason"] = "started" if state["running"] else "start-not-running"
        if state["running"]:
            logger.info("Pipeline auto-start triggered from %s after token detection", trigger)
        else:
            logger.warning(
                "Pipeline auto-start attempt from %s did not result in running task", trigger
            )
    except Exception as exc:
        state["reason"] = "start-error"
        state["error"] = str(exc)
        logger.error("Pipeline auto-start failed from %s: %s", trigger, exc)

    return state


def _build_pipeline_config() -> dict:
    import os
    from app_config import DHAN_ENABLED, USE_DHAN_MOCK, PRODUCTION_MODE, FAIL_CLOSED_ON_LIVE_ERROR
    from config.symbols import TRADING_SYMBOLS, INDEX_SYMBOLS
    use_mock = USE_DHAN_MOCK or os.getenv("USE_DHAN_MOCK", "false").lower() == "true"
    return {
        "dhan_enabled": DHAN_ENABLED,
        "use_dhan_mock": use_mock,
        "production_mode": False if use_mock else PRODUCTION_MODE,
        "fail_closed_on_live_error": False if use_mock else FAIL_CLOSED_ON_LIVE_ERROR,
        "health_check_enabled": True,
        "auto_recovery_enabled": True,
        "metrics_enabled": True,
        "alerting_enabled": True,
        "dhan_client_id": (
            os.getenv("DHAN_USER_ID")
            or os.getenv("DHAN_CLIENT_ID")
            or os.getenv("DHAN_API_KEY")
        ),
        "dhan_access_token": (
            __import__("services.dhan_token_service", fromlist=["get_dhan_token"]).get_dhan_token()
            or os.getenv("DHAN_ACCESS_TOKEN")
            or os.getenv("DHAN_API_SECRET")
        ),
        "symbols": TRADING_SYMBOLS,
        "index_symbols": INDEX_SYMBOLS,
    }
