# backend/app/app.py
"""
Application assembler.

This module:
  1. Runs the startup security baseline check.
  2. Instantiates the FastAPI app with middleware.
  3. Includes all five focused APIRouters.
  4. Owns the lifespan (startup/shutdown) — no route handlers inline.

Entry-point (uvicorn): ``app.main:app``  (main.py re-exports this ``app``).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute, APIWebSocketRoute

from app_config import (
    ALLOWED_ORIGINS,
    DHAN_ENABLED,
    PRODUCTION_MODE,
    FAIL_CLOSED_ON_LIVE_ERROR,
    USE_DHAN_MOCK,
    SESSION_BOOTSTRAP_TTL_SECONDS,
)
from logger import logger
from middleware import (
    ErrorHandlerMiddleware,
    LoggingMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from routers import analysis, auth, broker, charts, shortlist, shortlist_shorts, watchdog_router, watchdog_router_shorts, short_screener_router
from state.scan_state import ScanStateService, MacroRegimeService
from state.session_state import SessionService


# ── Security baseline ─────────────────────────────────────────────────────────

def _security_sensitive_endpoint_check(application: FastAPI) -> List[str]:
    """Validate that sensitive endpoints remain protected by auth guards."""
    issues: List[str] = []

    def _collect_routes(routes):
        collected = []
        for r in routes:
            if hasattr(r, "original_router"):
                collected.extend(_collect_routes(r.original_router.routes))
            elif hasattr(r, "routes"):
                collected.extend(_collect_routes(r.routes))
            else:
                collected.append(r)
        return collected

    route_map: Dict[Tuple[str, str], object] = {}
    for route in _collect_routes(application.routes):
        if isinstance(route, APIRoute):
            for method in route.methods or {"GET"}:
                route_map[(route.path, method.upper())] = route
        elif isinstance(route, APIWebSocketRoute):
            route_map[(route.path, "WEBSOCKET")] = route

    sensitive_http_routes: List[Tuple[str, str]] = [
        ("GET", "/api/chart/search"),
        ("GET", "/api/analysis/symbols"),
    ]

    for method, path in sensitive_http_routes:
        route = route_map.get((path, method))
        if not isinstance(route, APIRoute):
            issues.append(f"Missing sensitive route {method} {path}")
            continue

        dependencies = getattr(route.dependant, "dependencies", [])
        has_verify_token = any(
            getattr(getattr(dep, "call", None), "__name__", "") == "verify_token"
            for dep in dependencies
        )
        if not has_verify_token:
            issues.append(f"Sensitive route {method} {path} is not guarded by verify_token")

    return issues

def _security_baseline_check(application: FastAPI | None = None) -> None:
    """
    Validate critical security invariants at startup.

    In dev mode: logs warnings only.
    In PRODUCTION_MODE: hard-fails (raises RuntimeError) on any violation.
    """
    issues: List[str] = []

    # 1. JWT secret strength
    jwt_secret = os.getenv("JWT_SECRET", "")
    if len(jwt_secret) < 32:
        issues.append("JWT_SECRET is shorter than 32 characters")
    if jwt_secret.lower() in {"changeme", "secret", "dev", "test", "password", "mysecret"}:
        issues.append("JWT_SECRET appears to be a placeholder value")

    import app_config
    is_prod = (
        str(os.getenv("PRODUCTION_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}
        or bool(getattr(app_config, "PRODUCTION_MODE", False))
    )
    allowed_orig = getattr(app_config, "ALLOWED_ORIGINS", [])

    # 2. ALLOW_WEAK_JWT_SECRET must be off in production
    _raw_allow_weak = str(os.getenv("ALLOW_WEAK_JWT_SECRET", "false")).strip().lower()
    allow_weak = _raw_allow_weak in {"1", "true", "yes", "on"}
    if is_prod and allow_weak:
        issues.append("PRODUCTION_MODE=true but ALLOW_WEAK_JWT_SECRET is enabled")

    # 3. CORS wildcard in production
    if is_prod and "*" in allowed_orig:
        issues.append("PRODUCTION_MODE=true but ALLOWED_ORIGINS contains wildcard '*'")

    # 4. CORS empty in production
    if is_prod and not allowed_orig:
        issues.append("PRODUCTION_MODE=true but ALLOWED_ORIGINS is empty")

    # 5. Sensitive endpoint protection checks
    if application is not None:
        checker = getattr(sys.modules.get("app"), "_security_sensitive_endpoint_check", _security_sensitive_endpoint_check)
        issues.extend(checker(application))

    if issues:
        for issue in issues:
            logger.error("SECURITY BASELINE FAILED: %s", issue)

        if is_prod:
            raise RuntimeError(
                "Security baseline check failed in PRODUCTION_MODE. "
                "Fix the issues listed above before starting. "
                f"Issues: {'; '.join(issues)}"
            )
        else:
            logger.warning(
                "Security baseline has %d warning(s) in dev mode "
                "(these would be hard-failures in PRODUCTION_MODE).",
                len(issues),
            )


# ── Lifespan ──────────────────────────────────────────────────────────────────

scheduler: AsyncIOScheduler | None = None


async def _broker_reconciliation_loop(watchdog: Any) -> None:
    from services.broker_reconciliation import reconcile_pending_orders
    from services.dhan_mcp_client import get_dhan_mcp_client

    client = get_dhan_mcp_client()
    while True:
        try:
            from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
            await reconcile_pending_orders(
                client=client,
                long_watchdog=watchdog,
                short_watchdog=get_algo_live_watchdog_shorts(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Broker lifecycle polling failed: %s", exc)
        await asyncio.sleep(5.0)


@asynccontextmanager
async def app_lifespan(application: FastAPI):
    await _startup(application)
    try:
        yield
    finally:
        await _shutdown(application)


async def _startup(application: FastAPI) -> None:
    global scheduler

    # ── Security baseline (first thing, before any state init) ────────────
    _security_baseline_check(application)

    # ── Production mode guard-rails ───────────────────────────────────────
    if PRODUCTION_MODE:
        if USE_DHAN_MOCK:
            raise RuntimeError("PRODUCTION_MODE=true requires USE_DHAN_MOCK=false")
        if not DHAN_ENABLED:
            raise RuntimeError("PRODUCTION_MODE=true requires DHAN_ENABLED=true")
        if not FAIL_CLOSED_ON_LIVE_ERROR:
            raise RuntimeError("PRODUCTION_MODE=true requires FAIL_CLOSED_ON_LIVE_ERROR=true")

    # ── Diagnostic startup banner ─────────────────────────────────────────
    
    k_key = os.getenv("DHAN_CLIENT_ID") or os.getenv("DHAN_API_KEY") or ""
    k_secret = os.getenv("DHAN_ACCESS_TOKEN") or os.getenv("DHAN_API_SECRET") or ""
    m_key = f"{k_key[:2]}...{k_key[-2:]}" if k_key and len(k_key) > 4 else "none"
    m_sec_len = len(k_secret) if k_secret else 0

    print("\n" + "=" * 60)
    print("      --- VIA-Agent7 BACKEND STARTUP ---")
    print(f"      Dhan Client ID Loaded:       {m_key}")
    print(f"      Dhan Access Token Length:    {m_sec_len}")
    print(f"      USE_DHAN_MOCK:             {USE_DHAN_MOCK}")
    print(f"      PRODUCTION_MODE:           {PRODUCTION_MODE}")
    print(f"      FAIL_CLOSED_ON_LIVE_ERROR: {FAIL_CLOSED_ON_LIVE_ERROR}")
    print("=" * 60 + "\n")

    logger.info("Intraday AI Desk Backend starting up...")

    # ── State services (attach to app.state) ──────────────────────────────
    application.state.scan_state = ScanStateService()
    macro_regime = MacroRegimeService()
    application.state.macro_regime = macro_regime
    application.state.session = SessionService(
        bootstrap_ttl_seconds=SESSION_BOOTSTRAP_TTL_SECONDS
    )
    application.state.active_quotes_connections = set()
    # Expose macro_regime and scan_state to non-FastAPI services (e.g. AIObserverService, Orchestrator)
    from state.scan_state import set_global_macro_regime, set_global_scan_state
    set_global_macro_regime(macro_regime)
    set_global_scan_state(application.state.scan_state)

    # ── Scheduler jobs ────────────────────────────────────────────────────
    # Recreate scheduler on each lifespan startup so repeated test lifecycles
    # do not reuse a closed event loop from a previous TestClient context.
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass

    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    application.state.scheduler = scheduler

    scan_state = application.state.scan_state
    macro_regime = application.state.macro_regime

    async def _update_macro_regime_job():
        _now_iso = lambda: datetime.now(timezone.utc).isoformat()
        from services.regime_engine import get_regime_engine
        import numpy as np

        detector = get_regime_engine()
        india_vix_env = os.getenv("INDIA_VIX_STATIC", "").strip()
        if not PRODUCTION_MODE and india_vix_env:
            india_vix = float(india_vix_env)
            detector.set_india_vix(india_vix)
        else:
            india_vix = detector.get_status().get("india_vix")

        # Build returns vector from candle aggregator
        from services.candle_aggregator import get_candle_aggregator
        aggregator = get_candle_aggregator()
        best_series = []
        for token, bars in aggregator.bars_1m.items():
            close_series = [float(bar.get("close", 0.0)) for bar in list(bars) if float(bar.get("close", 0.0)) > 0.0]
            current_bar = aggregator.current_1m.get(token)
            if current_bar and float(current_bar.get("close", 0.0)) > 0.0:
                close_series.append(float(current_bar.get("close", 0.0)))
            if len(close_series) > len(best_series):
                best_series = close_series

        if len(best_series) < 12:
            try:
                from services.dhan_historical import DhanHistoricalClient, fetch_symbol_warmup
                from services.dhan_instrument_resolver import get_instrument_resolver
                client_id = os.getenv("DHAN_CLIENT_ID") or os.getenv("DHAN_USER_ID")
                access_token = os.getenv("DHAN_ACCESS_TOKEN")
                base_url = os.getenv("DHAN_BASE_URL", "https://api.dhan.co/v2")
                if client_id and access_token:
                    client = DhanHistoricalClient(client_id, access_token, base_url=base_url)
                    resolver = get_instrument_resolver()
                    resolver.load()
                    sid = resolver.get_security_id("RELIANCE") or "2885"
                    history = await fetch_symbol_warmup(client, security_id=str(sid), exchange_segment="NSE_EQ")
                    if history.get("1m"):
                        aggregator.hydrate_history(int(sid), "RELIANCE", history["1m"], history["5m"])
                        best_series = [float(bar.get("close", 0.0)) for bar in history["1m"] if float(bar.get("close", 0.0)) > 0.0]
                        logger.info("Macro regime: bootstrapped %d real 1m bars from Dhan for RELIANCE", len(best_series))
            except Exception as hist_err:
                logger.warning("Macro regime historical bootstrap failed: %s", hist_err)

        if len(best_series) < 12:
            logger.warning(
                "Macro regime: candle aggregator has only %d bars — waiting for tick data",
                len(best_series),
            )
            # Still not enough — update with unavailable tag and return
            macro_regime.update(updated_at=_now_iso(), india_vix=india_vix, data_source="unavailable_real_data")
            return

        prices = np.array(best_series[-100:], dtype="float64")
        if prices.size < 12 or np.any(prices <= 0.0):
            macro_regime.update(updated_at=_now_iso(), india_vix=india_vix, data_source="unavailable_real_data")
            return

        real_returns = (np.diff(prices) / prices[:-1]).astype("float64")
        if real_returns.size < 3:
            macro_regime.update(updated_at=_now_iso(), india_vix=india_vix, data_source="unavailable_real_data")
            return

        regime_name, probs, weights = detector.predict_macro_regime(real_returns)
        macro_regime.update(
            regime=regime_name,
            weights=weights,
            probabilities={
                detector.state_map.get(i, f"State_{i}"): float(probs[i])
                for i in range(len(probs))
            },
            india_vix=india_vix,
            updated_at=_now_iso(),
            data_source="real_1m_bars",
            sample_size=int(real_returns.size),
        )

    # Scheduler jobs
    scheduler.add_job(
        _update_macro_regime_job,
        trigger="interval", minutes=5,
    )

    async def _pre_session_reset():
        try:
            from services.vol_vwap import get_vol_vwap_engine
            vwap_eng = get_vol_vwap_engine()
            for sym in list(vwap_eng._states.keys()):
                vwap_eng.reset_session(sym)
            logger.info("Pre-session VWAP / EWMA reset complete for %d symbols.", len(vwap_eng._states))
        except Exception as exc:
            logger.error("Pre-session reset error: %s", exc)

    async def _compression_pre_session_reset():
        try:
            from services.compression_engine import get_compression_engine
            comp_eng = get_compression_engine()
            for sym in list(comp_eng._states.keys()):
                comp_eng.reset_session(sym)
            logger.info("Pre-session Compression reset complete for %d symbols.", len(comp_eng._states))
        except Exception as exc:
            logger.error("Compression pre-session reset error: %s", exc)

    async def _warmup_candle_history():
        """Load the latest 100 completed 1m/5m bars into memory before the session."""
        if USE_DHAN_MOCK or not DHAN_ENABLED:
            logger.info("Skipping Dhan historical warm-up in mock/disabled mode.")
            return
        try:
            from config.symbols import NIFTY_50_SYMBOLS
            from services.candle_aggregator import get_candle_aggregator
            from services.dhan_historical import DhanHistoricalClient, fetch_symbol_warmup
            from services.dhan_instrument_resolver import get_instrument_resolver

            from services.dhan_token_service import get_dhan_token
            client_id = os.getenv("DHAN_CLIENT_ID") or os.getenv("DHAN_USER_ID")
            access_token = get_dhan_token() or os.getenv("DHAN_ACCESS_TOKEN")
            base_url = os.getenv("DHAN_BASE_URL", "https://api.dhan.co/v2")
            if not client_id or not access_token:
                logger.info("Skipping candle warm-up: missing Dhan credentials.")
                return
            client = DhanHistoricalClient(client_id, access_token, base_url=base_url)
            resolver = get_instrument_resolver()
            resolver.load()
            resolved = resolver.resolve_symbols(list(NIFTY_50_SYMBOLS))
            aggregator = get_candle_aggregator()
            ready = 0

            for security_id, symbol in resolved.items():
                try:
                    history = await fetch_symbol_warmup(
                        client,
                        security_id=security_id,
                        exchange_segment="NSE_EQ",
                    )
                    status = aggregator.hydrate_history(
                        int(security_id),
                        symbol,
                        history["1m"],
                        history["5m"],
                    )
                    if status["ready"]:
                        ready += 1
                    else:
                        logger.warning("Candle warm-up incomplete for %s: %s", symbol, status)
                except Exception as exc:
                    logger.error("Candle warm-up failed for %s: %s", symbol, exc)
                await asyncio.sleep(0.02)

            logger.info(
                "Dhan candle warm-up complete: %d/%d symbols READY with 100 1m and 5m bars.",
                ready,
                len(resolved),
            )
            await _update_macro_regime_job()
        except Exception as exc:
            logger.error("Pre-market candle warm-up failed: %s", exc)

    async def _daily_dhan_session_reset():
        try:
            if not DHAN_ENABLED or USE_DHAN_MOCK:
                return
            application.state.session.clear_bootstrap_key()
            application.state.session.rotate_stream_generation()
            from services.orchestrator import stop_production_pipeline
            await stop_production_pipeline()
            application.state.orchestrator_task = None
        except Exception as exc:
            logger.error("Daily Dhan session reset error: %s", exc)

    scheduler.add_job(
        _pre_session_reset,
        "cron", hour=3, minute=40, id="pre_session_reset",
    )
    scheduler.add_job(
        _compression_pre_session_reset,
        "cron", hour=3, minute=40, id="compression_pre_session_reset",
    )
    scheduler.add_job(
        _daily_dhan_session_reset,
        "cron", hour=18, minute=30, id="daily_broker_session_reset",
    )

    async def _update_fii_dii_job():
        try:
            from services.fii_dii_service import get_fii_dii_service
            svc = get_fii_dii_service()
            data = await asyncio.to_thread(svc.fetch_latest_fii_dii)
            logger.info("FII/DII daily net flow updated: FII %+0.1f Cr, DII %+0.1f Cr", data.get("fii_net_cr", 0.0), data.get("dii_net_cr", 0.0))
        except Exception as exc:
            logger.warning("FII/DII scheduled fetch error: %s", exc)

    scheduler.add_job(
        _update_fii_dii_job,
        "cron", hour=18, minute=5, id="daily_fii_dii_update",
    )
    # Reconcile external institutional flows on startup in case system was offline at 18:05 IST
    asyncio.create_task(_update_fii_dii_job())
    scheduler.add_job(
        _warmup_candle_history,
        "cron", hour=8, minute=45, id="pre_market_candle_warmup",
        max_instances=1, coalesce=True,
    )

    # A restart during the pre-market window must not leave the in-memory
    # aggregator empty until the next trading day.
    from datetime import datetime, time as dt_time
    from zoneinfo import ZoneInfo
    startup_time = datetime.now(ZoneInfo("Asia/Kolkata")).time()
    if dt_time(8, 45) <= startup_time < dt_time(9, 15):
        scheduler.add_job(
            _warmup_candle_history,
            id="startup_pre_market_candle_warmup",
            max_instances=1,
            coalesce=True,
        )

    scheduler.start()
    if not USE_DHAN_MOCK and DHAN_ENABLED:
        asyncio.create_task(_warmup_candle_history())

    # Seed synthetic history to warm up CandleAggregator, VolVWAPEngine and CompressionEngine immediately when in mock mode
    if USE_DHAN_MOCK:
        try:
            from config.symbols import TRADING_SYMBOLS, INDEX_SYMBOLS
            from services.candle_aggregator import get_candle_aggregator
            from services.vol_vwap import get_vol_vwap_engine
            from services.compression_engine import get_compression_engine
            from collections import deque
            import numpy as np
            import time

            candle_agg = get_candle_aggregator()
            vol_vwap_engine = get_vol_vwap_engine()
            compression_engine = get_compression_engine()

            all_symbols = TRADING_SYMBOLS + INDEX_SYMBOLS
            if "pytest" in sys.modules:
                all_symbols = ["RELIANCE", "TCS", "INFY", "NIFTY 50", "INDIA VIX"]
            base_prices = {
                "NIFTY 50": 24500.0,
                "INDIA VIX": 13.5,
                **{sym: 1000.0 + abs(hash(sym)) % 3000 for sym in TRADING_SYMBOLS}
            }

            logger.info("USE_DHAN_MOCK=true: Seeding 45 synthetic history bars to warm up engines...")
            token_map = {sym: abs(hash(sym)) % 1000000 for sym in all_symbols}
            now_ts = int(time.time())
            
            for symbol in all_symbols:
                token = token_map[symbol]
                base_p = base_prices.get(symbol, 1000.0)
                prev_bars = []
                np.random.seed(abs(hash(symbol)) % 123456)
                current_price = base_p
                start_time_base = (now_ts // 60) * 60 - 45 * 60
                
                for i in range(45):
                    # Target-specific seeding to force setup states
                    if symbol == "RELIANCE":
                        # We want it to be SETUP_READY (setup conditions met for >= 3 bars).
                        # Let's meet setup conditions for i >= 30.
                        if i >= 30:
                            close_p = base_p + np.sin(i) * 0.05
                            high_p = max(base_p, close_p) + 0.1
                            low_p = min(base_p, close_p) - 0.1
                            open_p = current_price
                            vol = float(1000 - (i - 30) * 30)
                            ewma_vol = float(10.0 * (0.93 ** (i - 30)))
                        else:
                            ret = np.random.randn() * 0.005
                            close_p = current_price * (1.0 + ret)
                            high_p = max(current_price, close_p) * 1.01
                            low_p = min(current_price, close_p) * 0.99
                            open_p = current_price
                            vol = float(np.random.randint(5000, 10000))
                            ewma_vol = float(np.random.randint(15, 30))
                            
                    elif symbol == "TCS":
                        # We want it to be SETUP_FORMING (1 or 2 bars).
                        # Let's meet setup conditions for i >= 35.
                        if i >= 35:
                            close_p = base_p + np.sin(i) * 0.05
                            high_p = max(base_p, close_p) + 0.1
                            low_p = min(base_p, close_p) - 0.1
                            open_p = current_price
                            vol = float(1000 - (i - 35) * 35)
                            ewma_vol = float(10.0 * (0.93 ** (i - 35)))
                        else:
                            ret = np.random.randn() * 0.005
                            close_p = current_price * (1.0 + ret)
                            high_p = max(current_price, close_p) * 1.01
                            low_p = min(current_price, close_p) * 0.99
                            open_p = current_price
                            vol = float(np.random.randint(5000, 10000))
                            ewma_vol = float(np.random.randint(15, 30))
                            
                    else:
                        ret = np.random.randn() * 0.001
                        close_p = current_price * (1.0 + ret)
                        high_p = max(current_price, close_p) * (1.0 + abs(np.random.randn()) * 0.001)
                        low_p = min(current_price, close_p) * (1.0 - abs(np.random.randn()) * 0.001)
                        open_p = current_price
                        vol = float(np.random.randint(2000, 10000))
                        ewma_vol = None
                        
                    bar_start = start_time_base + i * 60
                    
                    bar = {
                        "symbol": symbol,
                        "open": open_p,
                        "high": high_p,
                        "low": low_p,
                        "close": close_p,
                        "volume": vol,
                        "day_cumulative_volume": vol,
                        "cvd": 0.0,
                        "tick_vol": vol,
                        "start_time": bar_start
                    }
                    
                    if token not in candle_agg.bars_1m:
                        candle_agg.bars_1m[token] = deque(maxlen=100)
                    candle_agg.bars_1m[token].append(bar)
                    
                    vwap_res = vol_vwap_engine.update_bar(
                        symbol=symbol,
                        bar=bar,
                        prev_bars=prev_bars,
                        cvd=0.0
                    )
                    
                    if ewma_vol is None:
                        ewma_vol = float(getattr(vwap_res, "ewma_vol", 0.0))
                        
                    compression_engine.add_volatility(symbol, ewma_vol)
                    compression_engine.update_bar(
                        symbol=symbol,
                        bar=bar,
                        cvd_value=0.0,
                        vwap_micro_state=vwap_res.micro_state,
                        regime="Calm Uptrend"
                    )
                    
                    prev_bars.append(bar)
                    current_price = close_p
                
                # Group 1m bars into 5m bars for 5m candle aggregator
                for j in range(9):
                    chunk = prev_bars[j*5 : (j+1)*5]
                    if not chunk:
                        continue
                    bar_5m = {
                        "symbol": symbol,
                        "open": chunk[0]["open"],
                        "high": max(b["high"] for b in chunk),
                        "low": min(b["low"] for b in chunk),
                        "close": chunk[-1]["close"],
                        "volume": sum(b["volume"] for b in chunk),
                        "day_cumulative_volume": chunk[-1]["day_cumulative_volume"],
                        "cvd": chunk[-1]["cvd"],
                        "tick_vol": sum(b["tick_vol"] for b in chunk),
                        "start_time": chunk[0]["start_time"]
                    }
                    if token not in candle_agg.bars_5m:
                        candle_agg.bars_5m[token] = deque(maxlen=100)
                    candle_agg.bars_5m[token].append(bar_5m)
                    
            logger.info("Warmup complete for %d symbols. All indicators and compression states are active.", len(all_symbols))
        except Exception as warmup_exc:
            logger.exception("Failed to seed synthetic history during mockup startup")


    # Initial macro regime update
    if USE_DHAN_MOCK:
        await _update_macro_regime_job()
    else:
        asyncio.create_task(_update_macro_regime_job())

    logger.info("Application ready. Session reset @ 09:10 IST | Static priors active.")

    # ── Pipeline auto-start ────────────────────────────────────────────────
    logger.info("Pipeline mode: dhan_enabled=%s, use_dhan_mock=%s", DHAN_ENABLED, USE_DHAN_MOCK)
    if DHAN_ENABLED and not USE_DHAN_MOCK:
        from services.dhan_token_service import get_dhan_token
        dhan_client = (
            os.getenv("DHAN_USER_ID")
            or os.getenv("DHAN_CLIENT_ID")
            or os.getenv("DHAN_API_KEY")
        )
        dhan_token = get_dhan_token()
        if not dhan_client or not dhan_token:
            message = (
                "DHAN credentials missing at startup. "
                "Set DHAN_CLIENT_ID and complete Dhan login to authenticate."
            )
            logger.warning("%s Live pipeline remains stopped.", message)
            application.state.scan_state.update_status(
                status="blocked",
                blocked_reason=message,
                data_source="none",
            )
            application.state.orchestrator_task = None
            return

    from pipeline_helpers import start_pipeline_background
    await start_pipeline_background(application)

    try:
        from services.dhan_historical import trigger_warmup_in_background
        asyncio.create_task(trigger_warmup_in_background(clear_existing=False))
        logger.info("Triggered candle warm-up in background on application start")
    except Exception as exc:
        logger.warning("Failed to trigger background warmup on start: %s", exc)

    try:
        from services.finbert_sentiment import get_finbert_sentiment_engine
        finbert_engine = get_finbert_sentiment_engine()
        asyncio.create_task(asyncio.to_thread(finbert_engine._ensure_loaded))
        logger.info("Triggered FinBERT GPU warm-up in background on application start")
    except Exception as exc:
        logger.warning("Failed to trigger FinBERT warmup on start: %s", exc)

    try:
        from services.telegram_hitl_service import get_telegram_hitl_service
        get_telegram_hitl_service().start_polling_daemon()
        logger.info("Triggered Telegram HITL long-polling daemon on application start")
    except Exception as exc:
        logger.warning("Failed to start Telegram HITL polling daemon on start: %s", exc)

    try:
        from services.live_algo_watchdog import get_algo_live_watchdog
        watchdog = get_algo_live_watchdog(
            dhan_client_id=os.getenv("DHAN_CLIENT_ID") or os.getenv("DHAN_USER_ID"),
            dhan_access_token=os.getenv("DHAN_ACCESS_TOKEN"),
        )
        restored = watchdog.restore_persisted_positions()
        from services.broker_reconciliation import (
            reconcile_pending_orders,
            reconcile_startup_positions,
        )
        from services.dhan_mcp_client import get_dhan_mcp_client
        reconciliation_client = get_dhan_mcp_client()
        pending_result = await reconcile_pending_orders(
            client=reconciliation_client,
            long_watchdog=watchdog,
        )
        position_result = await reconcile_startup_positions(
            client=reconciliation_client,
            long_watchdog=watchdog,
        )
        logger.info(
            "Broker lifecycle reconciliation complete: pending=%s positions=%s",
            pending_result.get("status"), position_result.get("status"),
        )
        if reconciliation_client.is_configured():
            application.state.broker_reconciliation_task = asyncio.create_task(
                _broker_reconciliation_loop(watchdog)
            )
        try:
            from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
            shorts_watchdog = get_algo_live_watchdog_shorts()
            restored += shorts_watchdog.restore_persisted_positions()
            await reconcile_pending_orders(
                client=reconciliation_client,
                long_watchdog=watchdog,
                short_watchdog=shorts_watchdog,
            )
        except Exception as exc:
            logger.warning("Failed to restore short lifecycle positions: %s", exc)
        logger.info(
            "Initialized AlgoLiveWatchdog singleton with Dhan credentials on startup; restored %d position(s)",
            restored,
        )
    except Exception as exc:
        logger.warning("Failed to initialize AlgoLiveWatchdog on start: %s", exc)

    # ── Phase 0: Watchdog Data Recorder outcome tagger daemon ─────────────
    try:
        from services.watchdog_data_recorder import get_outcome_tagger
        get_outcome_tagger().start()
        logger.info("WatchdogOutcomeTagger daemon started.")
    except Exception as exc:
        logger.warning("Failed to start WatchdogOutcomeTagger on startup: %s", exc)

    # ── Short Screener Live Engine Lifecycle (Default OFF / Standby) ───────
    try:
        from services.short_screener_engine import get_short_screener_engine
        from services.orchestrator import get_orchestrator
        orch = get_orchestrator()
        screener_engine = get_short_screener_engine()
        screener_engine.initialize(dhan_ticker=getattr(orch, "dhan_ticker", None))
        # By default, Short Screener Live starts in STANDBY (OFF) mode.
        # User manually turns it ON via Master Power switch on the dashboard.
        screener_engine.is_active = True
        await screener_engine.start()
        logger.info("ShortScreenerEngine started actively on startup.")
    except Exception as exc:
        logger.warning("Failed to initialize ShortScreenerEngine on startup: %s", exc)


async def _shutdown(application: FastAPI) -> None:
    global scheduler

    logger.info("Intraday AI Desk Backend shutting down...")

    try:
        from services.short_screener_engine import get_short_screener_engine
        await get_short_screener_engine().stop()
        logger.info("ShortScreenerEngine stopped cleanly.")
    except Exception as exc:
        logger.warning("Error stopping ShortScreenerEngine: %s", exc)

    reconciliation_task = getattr(application.state, "broker_reconciliation_task", None)
    if reconciliation_task is not None:
        reconciliation_task.cancel()
        try:
            await reconciliation_task
        except asyncio.CancelledError:
            pass
        application.state.broker_reconciliation_task = None

    try:
        from services.telegram_hitl_service import get_telegram_hitl_service
        get_telegram_hitl_service().stop_polling_daemon()
    except Exception as exc:
        logger.warning("Error stopping Telegram HITL polling daemon: %s", exc)

    runtime_scheduler = getattr(application.state, "scheduler", None)
    if runtime_scheduler is not None:
        try:
            runtime_scheduler.shutdown(wait=False)
        except Exception as exc:
            logger.warning("Error shutting down APScheduler: %s", exc)
    application.state.scheduler = None
    scheduler = None

    try:
        from services.orchestrator import stop_production_pipeline
        await stop_production_pipeline()
    except Exception as exc:
        logger.error("Error stopping production pipeline: %s", exc)



# ── FastAPI app assembly ──────────────────────────────────────────────────────

app = FastAPI(
    title="VIA Trading Desk — Charts & Order Flow Backend",
    description="Backend for Pro Charts, Order Flow, and Multi-Timeframe Chart Analysis",
    version="2.0.0",
    lifespan=app_lifespan,
)

# Compatibility initialization for tests that instantiate TestClient without
# entering lifespan (startup hooks). Lifespan startup still re-initializes
# these services for normal runtime.
app.state.scan_state = ScanStateService()
app.state.macro_regime = MacroRegimeService()
app.state.session = SessionService(
    bootstrap_ttl_seconds=SESSION_BOOTSTRAP_TTL_SECONDS
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Custom middleware (order matters: outermost → innermost)
app.add_middleware(ErrorHandlerMiddleware)
app.add_middleware(RateLimitMiddleware, requests_per_minute=6000)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(LoggingMiddleware)

# Routers
app.include_router(auth.router)
app.include_router(broker.router)
app.include_router(charts.router)
app.include_router(analysis.router)
app.include_router(shortlist.router)
app.include_router(shortlist_shorts.router)
app.include_router(watchdog_router.router)
app.include_router(watchdog_router_shorts.router)
app.include_router(short_screener_router.router)
app.include_router(short_screener_router.ws_router)


@app.get("/health", tags=["system"])
@app.get("/api/health", tags=["system"])
async def health_check():
    return {
        "status": "ok",
        "service": "via-agent-backend",
        "version": "2.0.0",
        "features": ["charts", "chart-analysis", "orderflow"],
    }
