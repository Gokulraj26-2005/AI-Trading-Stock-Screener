"""
Production-Level Orchestrator for Charts & Order Flow
======================================================
Coordinates the market data ingestion pipeline (Dhan WebSocket feeds / Mock feed)
and routes real-time data to CVD Engine, Candle Aggregator, Vol VWAP, and Compression.
"""

import asyncio
import inspect
import logging
import os
import random
import signal
import threading
from datetime import datetime, time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from config.symbols import TRADING_SYMBOLS
from .candle_aggregator import CandleAggregator, get_candle_aggregator
from .compression_engine import CompressionEngine, get_compression_engine
from .cvd_engine import CVDEngine, get_cvd_engine
from .dhan_adapter import DhanWebSocket, get_dhan_depth_ticker, get_dhan_ticker
from .dhan_instrument_resolver import get_instrument_resolver
from .regime_engine import HMMRegimeDetector, get_regime_engine
from .vol_vwap import VolVWAPEngine, get_vol_vwap_engine
from .zmq_publisher import ZMQPublisher, get_zmq_publisher

logger = logging.getLogger(__name__)


class ProductionOrchestrator:
    """
    Orchestrator managing real-time data flow for Charts and Order Flow.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        base_config = self._get_default_config()
        self.config = {**base_config, **(config or {})}
        self.services: Dict[str, Any] = {}
        self.tasks: List[asyncio.Task] = []
        self.running = False
        self.health_check_interval = 30  # seconds
        self.metrics = {
            "start_time": None,
            "ticks_processed": 0,
            "bars_generated": 0,
            "signals_generated": 0,
            "errors": 0,
        }
        self.dhan_ticker = None
        self.dhan_depth_ticker = None
        self.dhan_ticker_task: Optional[asyncio.Task] = None
        self.dhan_depth_task: Optional[asyncio.Task] = None
        self._signal_handlers_registered = False

    def register_signal_handlers(self) -> None:
        """Register process signal handlers from the main application entrypoint."""
        if self._signal_handlers_registered:
            return

        if threading.current_thread() is not threading.main_thread():
            logger.debug("Skipping signal handler registration outside main thread")
            return

        try:
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
            self._signal_handlers_registered = True
        except Exception as exc:
            logger.warning("Signal handler registration failed: %s", exc)

    def _get_default_config(self) -> Dict[str, Any]:
        """Get default production configuration."""
        return {
            "zmq_port": 5555,
            "symbols": TRADING_SYMBOLS,
            "bar_intervals": [60, 300],
            "max_concurrent_symbols": 50,
            "dhan_enabled": True,
            "dhan_depth_tier": 20,
            "enable_depth_feed": True,
            "use_dhan_mock": False,
            "production_mode": False,
            "fail_closed_on_live_error": False,
            "dhan_client_id": None,
            "dhan_access_token": None,
            "security_ids": [],
            "health_check_enabled": True,
            "auto_recovery_enabled": True,
            "metrics_enabled": True,
        }

    async def initialize_services(self) -> None:
        """Initialize core services in dependency order."""
        logger.info("Initializing core orchestrator services...")

        try:
            self.services["publisher"] = get_zmq_publisher()
            self.services["cvd_engine"] = get_cvd_engine()
            self.services["candle_aggregator"] = get_candle_aggregator()
            self.services["regime_engine"] = get_regime_engine()
            self.services["vol_vwap_engine"] = get_vol_vwap_engine()
            self.services["compression_engine"] = get_compression_engine()

            try:
                from .short_screener_engine import get_short_screener_engine
                from config.symbols import CHART_ANALYSIS_SYMBOLS
                screener = get_short_screener_engine()
                screener.initialize(
                    symbols=CHART_ANALYSIS_SYMBOLS,
                    cvd_engine=self.services.get("cvd_engine"),
                    vol_vwap_engine=self.services.get("vol_vwap_engine"),
                    candle_aggregator=self.services.get("candle_aggregator"),
                    dhan_ticker=self.dhan_ticker,
                )
                self.services["short_screener"] = screener
            except Exception as screener_init_err:
                logger.warning("ShortScreenerEngine initialization in orchestrator deferred: %s", screener_init_err)

            logger.info(f"Initialized {len(self.services)} services successfully")
        except Exception as e:
            logger.error(f"Failed to initialize services: {e}")
            raise

    async def start_services(self) -> None:
        """Start all services with proper error handling."""
        logger.info("Starting core services...")

        for name, service in self.services.items():
            try:
                if hasattr(service, "start"):
                    start_result = service.start()
                    if inspect.isawaitable(start_result):
                        await start_result
                    logger.info(f"Started service: {name}")
                else:
                    logger.debug(f"Service {name} has no start method")
            except Exception as e:
                logger.error(f"Failed to start service {name}: {e}")
                if self.config["auto_recovery_enabled"]:
                    logger.info(f"Attempting to restart service {name}...")
                    await self._restart_service(name)
                else:
                    raise

    async def connect_data_flow(self) -> None:
        """Establish data flow connections between services."""
        logger.info("Data flow is managed via ZMQ pub/sub and in-process feeds.")

    async def start_pipeline(self) -> None:
        """Start the complete processing pipeline."""
        logger.info("Starting production trading pipeline...")

        self.running = True
        self.metrics["start_time"] = datetime.now()

        # Start background monitor tasks
        self.tasks = [
            asyncio.create_task(self._health_monitor()),
            asyncio.create_task(self._metrics_collector()),
            asyncio.create_task(self._error_handler()),
            asyncio.create_task(self._market_hours_manager()),
        ]

        # Start Dhan feed path or mock
        dhan_enabled_cfg = self.config.get("dhan_enabled", True)
        dhan_enabled = (
            dhan_enabled_cfg.lower() == "true"
            if isinstance(dhan_enabled_cfg, str)
            else bool(dhan_enabled_cfg)
        )
        production_mode_cfg = self.config.get("production_mode", os.getenv("PRODUCTION_MODE", "false"))
        production_mode = (
            production_mode_cfg.lower() == "true"
            if isinstance(production_mode_cfg, str)
            else bool(production_mode_cfg)
        )
        fail_closed_cfg = self.config.get(
            "fail_closed_on_live_error", os.getenv("FAIL_CLOSED_ON_LIVE_ERROR", "false")
        )
        fail_closed_on_live_error = (
            fail_closed_cfg.lower() == "true"
            if isinstance(fail_closed_cfg, str)
            else bool(fail_closed_cfg)
        )

        if dhan_enabled:
            await self._start_dhan_feed()
        else:
            if production_mode or fail_closed_on_live_error:
                raise RuntimeError("dhan_enabled=false is blocked in fail-closed/production mode")
            logger.warning("dhan_enabled=false, falling back to internal mock tick generator.")
            self.tasks.append(asyncio.create_task(self._mock_data_generator()))

        logger.info("Production pipeline started successfully")
        logger.info(f"Monitoring {len(self.config['symbols'])} symbols")

    async def _start_dhan_feed(self) -> None:
        """Initialize and start Dhan WebSocket feeds."""
        logger.info("Initializing Dhan WebSocket feeds...")

        client_id = self.config.get("dhan_client_id") or os.getenv("DHAN_CLIENT_ID")
        access_token = self.config.get("dhan_access_token") or os.getenv("DHAN_ACCESS_TOKEN")
        use_mock_cfg = self.config.get("use_dhan_mock", os.getenv("USE_DHAN_MOCK", "false"))
        use_mock = (
            use_mock_cfg.lower() == "true"
            if isinstance(use_mock_cfg, str)
            else bool(use_mock_cfg)
        )
        production_mode_cfg = self.config.get("production_mode", os.getenv("PRODUCTION_MODE", "false"))
        production_mode = (
            production_mode_cfg.lower() == "true"
            if isinstance(production_mode_cfg, str)
            else bool(production_mode_cfg)
        )
        fail_closed_cfg = self.config.get(
            "fail_closed_on_live_error", os.getenv("FAIL_CLOSED_ON_LIVE_ERROR", "false")
        )
        fail_closed_on_live_error = (
            fail_closed_cfg.lower() == "true"
            if isinstance(fail_closed_cfg, str)
            else bool(fail_closed_cfg)
        )
        allow_mock_fallback = not (production_mode or fail_closed_on_live_error)

        def _start_mock_feed(reason: str) -> None:
            if not allow_mock_fallback:
                raise RuntimeError(f"{reason} (mock fallback blocked in production/fail-closed mode)")
            logger.warning("%s Falling back to MockDhanWebSocket.", reason)
            self.dhan_ticker = get_dhan_ticker(use_mock=True)
            self.dhan_ticker_task = asyncio.create_task(self.dhan_ticker.connect())
            self.tasks.append(self.dhan_ticker_task)

        if not client_id or not access_token:
            if use_mock or allow_mock_fallback:
                _start_mock_feed("Dhan credentials not configured.")
                return
            raise RuntimeError("Dhan credentials missing in production/fail-closed mode")

        symbols = self.config.get("symbols", TRADING_SYMBOLS)
        self.dhan_ticker = get_dhan_ticker(
            client_id=client_id,
            access_token=access_token,
            use_mock=use_mock,
        )
        screener = self.services.get("short_screener")
        if screener and hasattr(screener, "set_dhan_ticker"):
            screener.set_dhan_ticker(self.dhan_ticker)

        if use_mock:
            self.dhan_ticker_task = asyncio.create_task(self.dhan_ticker.connect())
            self.tasks.append(self.dhan_ticker_task)
            logger.info("Dhan feed started in mock mode.")
            return

        # Live Mode: Resolve symbols and subscribe
        resolver = get_instrument_resolver()
        try:
            resolver.load()
            index_symbols = self.config.get("index_symbols", ["INDIA VIX", "NIFTY 50"])
            token_to_symbol = resolver.resolve_symbols(symbols)
            index_token_to_symbol = resolver.resolve_index_symbols(index_symbols)
            instrument_list = resolver.get_subscription_list(symbols + index_symbols)
        except Exception as exc:
            if allow_mock_fallback:
                _start_mock_feed(f"Live instrument token resolution failed ({exc}).")
                return
            raise

        if not instrument_list:
            if allow_mock_fallback:
                _start_mock_feed("No instruments resolved for live feed.")
                return
            raise RuntimeError("No instruments resolved for live Dhan WebSocket subscription")

        if hasattr(self.dhan_ticker, "set_instrument_list"):
            self.dhan_ticker.set_instrument_list(instrument_list)
        if hasattr(self.dhan_ticker, "set_symbol_lookup"):
            self.dhan_ticker.set_symbol_lookup(token_to_symbol, index_token_to_symbol)

        try:
            self.dhan_ticker_task = asyncio.create_task(self.dhan_ticker.connect())
            self.tasks.append(self.dhan_ticker_task)
            logger.info("Dhan standard feed started in live mode.")
        except Exception as exc:
            if allow_mock_fallback:
                _start_mock_feed(f"Live Dhan WebSocket connect failed ({exc}).")
                return
            raise

        # 20-Depth feed for top active symbols (idle by default to prevent background overhead)
        enable_depth_feed = (
            str(self.config.get("enable_depth_feed", os.getenv("ENABLE_DEPTH_FEED", "true"))).lower() == "true"
        )
        if enable_depth_feed:
            # Reserve 3 of the 50 depth slots for Watchdog active trades (which get dedicated 200-level).
            # This ensures the 20-depth connection always has headroom for dynamic subscription refresh.
            MAX_20DEPTH_ANALYTICAL = 47   # 50 total - 3 reserved for watchdog
            shortlist_20 = instrument_list[:MAX_20DEPTH_ANALYTICAL]
            if shortlist_20:
                try:
                    self.dhan_depth_ticker = get_dhan_depth_ticker(
                        client_id=client_id,
                        access_token=access_token,
                        depth_tier=20,
                    )
                    self.dhan_depth_ticker.set_instrument_list(shortlist_20)
                    self.dhan_depth_ticker.set_symbol_lookup(token_to_symbol)
                    self.dhan_depth_task = asyncio.create_task(self.dhan_depth_ticker.connect())
                    self.tasks.append(self.dhan_depth_task)
                    logger.info("Dhan 20-depth feed started with %d instruments.", len(shortlist_20))
                except Exception as exc:
                    logger.warning("Failed to start 20-depth feed: %s", exc)
        else:
            logger.info("Dhan 20-depth feed idle by default (zero background processing overhead for Pro Charts).")

    async def _mock_data_generator(self) -> None:
        """Generate realistic synthetic tick data for development/testing."""
        logger.info("Starting internal mock data generator...")

        base_prices = {symbol: 1000 + random.uniform(-200, 200) for symbol in self.config["symbols"]}
        cumulative_volume = {symbol: 0 for symbol in self.config["symbols"]}
        cvd_engine = self.services.get("cvd_engine")

        while self.running:
            try:
                for symbol in self.config["symbols"]:
                    price_change = random.gauss(0, 2)
                    tick_volume = random.randint(10, 120)
                    base_prices[symbol] += price_change
                    cumulative_volume[symbol] += tick_volume

                    ltp = round(base_prices[symbol], 2)
                    bid_price = round(ltp - 0.1, 2)
                    ask_price = round(ltp + 0.1, 2)
                    bid_qty = random.randint(200, 2500)
                    ask_qty = random.randint(200, 2500)
                    token = abs(hash(symbol)) % 1000000

                    cvd_result = (
                        cvd_engine.process_tick(
                            instrument_token=token,
                            last_price=ltp,
                            current_volume=cumulative_volume[symbol],
                            best_bid=bid_price,
                            best_ask=ask_price,
                            total_bid_qty=bid_qty,
                            total_ask_qty=ask_qty,
                        )
                        if cvd_engine
                        else None
                    )

                    tick_data = {
                        "token": token,
                        "symbol": symbol,
                        "ltp": ltp,
                        "volume": cumulative_volume[symbol],
                        "tick_vol": cvd_result.tick_volume if cvd_result else tick_volume,
                        "cvd": cvd_result.cvd if cvd_result else 0.0,
                        "obi": cvd_result.obi if cvd_result else 0.0,
                        "timestamp": datetime.now().timestamp(),
                        "bid_price": bid_price,
                        "ask_price": ask_price,
                        "bid_qty": bid_qty,
                        "ask_qty": ask_qty,
                        "best_bid": bid_price,
                        "best_ask": ask_price,
                        "total_bid_qty": bid_qty,
                        "total_ask_qty": ask_qty,
                    }

                    if "publisher" in self.services:
                        await self.services["publisher"].publish_tick(tick_data)
                    if "short_screener" in self.services:
                        self.services["short_screener"].on_tick(tick_data)
                    self.metrics["ticks_processed"] += 1

                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Mock data generator error: {e}")
                await asyncio.sleep(5)

    async def _health_monitor(self) -> None:
        """Monitor service health and perform auto-recovery."""
        while self.running:
            try:
                await asyncio.sleep(self.health_check_interval)

                for name, service in list(self.services.items()):
                    if hasattr(service, "health_check"):
                        try:
                            healthy = await service.health_check()
                            if not healthy:
                                logger.warning(f"Service {name} health check failed")
                                if self.config["auto_recovery_enabled"]:
                                    await self._restart_service(name)
                        except Exception as svc_exc:
                            logger.warning(f"Service {name} health_check raised: {svc_exc}")

            except Exception as e:
                logger.error(f"Health monitor error: {e}")

    async def _restart_service(self, service_name: str) -> None:
        """Restart a failed service."""
        try:
            logger.info(f"Restarting service: {service_name}")

            if service_name in self.services:
                old_service = self.services[service_name]
                if hasattr(old_service, "stop"):
                    stop_res = old_service.stop()
                    if inspect.isawaitable(stop_res):
                        await stop_res

                if service_name == "publisher":
                    self.services[service_name] = get_zmq_publisher()
                elif service_name == "cvd_engine":
                    self.services[service_name] = get_cvd_engine()
                elif service_name == "candle_aggregator":
                    self.services[service_name] = get_candle_aggregator()
                elif service_name == "regime_engine":
                    self.services[service_name] = get_regime_engine()
                elif service_name == "vol_vwap_engine":
                    self.services[service_name] = get_vol_vwap_engine()
                elif service_name == "compression_engine":
                    self.services[service_name] = get_compression_engine()

                if hasattr(self.services[service_name], "start"):
                    restart_result = self.services[service_name].start()
                    if inspect.isawaitable(restart_result):
                        await restart_result

                logger.info(f"Successfully restarted service: {service_name}")

        except Exception as e:
            logger.error(f"Failed to restart service {service_name}: {e}")

    async def _metrics_collector(self) -> None:
        """Collect and log performance metrics."""
        while self.running:
            try:
                await asyncio.sleep(60)

                candle_aggregator = self.services.get("candle_aggregator")
                ticks_count = self.metrics["ticks_processed"]
                if candle_aggregator and hasattr(candle_aggregator, "ticks_received"):
                    ticks_count = max(ticks_count, candle_aggregator.ticks_received)
                if self.dhan_ticker and hasattr(self.dhan_ticker, "latest_quotes"):
                    ticks_count = max(ticks_count, len(self.dhan_ticker.latest_quotes))

                bars_count = self.metrics["bars_generated"]
                if candle_aggregator and hasattr(candle_aggregator, "bars_closed"):
                    bars_count = max(bars_count, candle_aggregator.bars_closed)

                uptime = (
                    (datetime.now() - self.metrics["start_time"]).total_seconds()
                    if self.metrics.get("start_time")
                    else 1.0
                )
                ticks_per_sec = ticks_count / uptime if uptime > 0 else 0

                logger.info(
                    "Metrics: Uptime=%.0fs | Ticks=%d (%.2f/s) | Bars=%d | Errors=%d",
                    uptime,
                    ticks_count,
                    ticks_per_sec,
                    bars_count,
                    self.metrics["errors"],
                )
            except Exception as e:
                logger.error(f"Metrics collector error: {e}")

    async def _error_handler(self) -> None:
        """Global error monitor for the pipeline."""
        while self.running:
            try:
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Error handler caught exception: {e}")
                self.metrics["errors"] += 1

    async def _market_hours_manager(self) -> None:
        """Manage market hours and session resets.

        Detects the 09:15 IST session boundary crossing and performs a full
        intraday reset of CVD accumulators and candle-aggregator forming bars
        so prior-day state never bleeds into the new session.
        """
        _ist = ZoneInfo("Asia/Kolkata")
        _last_session_was_open: bool = False  # track prior state to detect crossing

        while self.running:
            try:
                now = datetime.now(_ist)
                now_time = now.time()
                is_open = time(9, 15) <= now_time <= time(15, 30)

                # ── Session-open crossing: closed → open ─────────────────────
                if is_open and not _last_session_was_open:
                    logger.info("⏰ Market session OPENED (IST %s). Triggering full intraday reset.", now_time.strftime("%H:%M"))

                    # 1. Reset CVD accumulators & divergence windows for ALL tokens
                    cvd_engine = self.services.get("cvd_engine")
                    if cvd_engine and hasattr(cvd_engine, "reset_session"):
                        try:
                            cvd_engine.reset_session()  # None → reset all
                            logger.info("CVDEngine: Full session reset dispatched at market open.")
                        except Exception as exc:
                            logger.error("CVDEngine session reset failed: %s", exc)

                    # 2. Reset Vol/VWAP engine session state
                    vol_vwap = self.services.get("vol_vwap_engine")
                    if vol_vwap and hasattr(vol_vwap, "reset_all_sessions"):
                        try:
                            vol_vwap.reset_all_sessions()
                            logger.info("VolVWAP engine: All session states reset at market open.")
                        except Exception as exc:
                            logger.error("VolVWAP session reset failed: %s", exc)

                    # 3. Reset Compression engine session state
                    comp_engine = self.services.get("compression_engine")
                    if comp_engine and hasattr(comp_engine, "reset_all_sessions"):
                        try:
                            comp_engine.reset_all_sessions()
                            logger.info("CompressionEngine: All session states reset at market open.")
                        except Exception as exc:
                            logger.error("CompressionEngine session reset failed: %s", exc)

                logger.debug(
                    "Market status: %s (IST %s)",
                    "OPEN" if is_open else "CLOSED",
                    now_time.strftime("%H:%M"),
                )
                _last_session_was_open = is_open
                await asyncio.sleep(60)  # check every minute for reliable crossing detection
            except Exception as e:
                logger.error("Market hours manager error: %s", e)

    async def stop_pipeline(self) -> None:
        """Gracefully stop the pipeline and feeds."""
        logger.info("Stopping production pipeline...")
        self.running = False

        if self.dhan_ticker is not None:
            try:
                if hasattr(self.dhan_ticker, "stop"):
                    maybe_coro = self.dhan_ticker.stop()
                    if asyncio.iscoroutine(maybe_coro):
                        await maybe_coro
                if hasattr(self.dhan_ticker, "close"):
                    maybe_coro = self.dhan_ticker.close()
                    if asyncio.iscoroutine(maybe_coro):
                        await maybe_coro
            except Exception as exc:
                logger.error("Error stopping Dhan standard feed: %s", exc)

        if self.dhan_depth_ticker is not None:
            try:
                if hasattr(self.dhan_depth_ticker, "stop"):
                    maybe_coro = self.dhan_depth_ticker.stop()
                    if asyncio.iscoroutine(maybe_coro):
                        await maybe_coro
                if hasattr(self.dhan_depth_ticker, "close"):
                    maybe_coro = self.dhan_depth_ticker.close()
                    if asyncio.iscoroutine(maybe_coro):
                        await maybe_coro
            except Exception as exc:
                logger.error("Error stopping Dhan depth feed: %s", exc)

        for task in self.tasks:
            task.cancel()

        for name, service in self.services.items():
            try:
                if hasattr(service, "stop"):
                    stop_result = service.stop()
                    if inspect.isawaitable(stop_result):
                        await stop_result
                    logger.info(f"Stopped service: {name}")
            except Exception as e:
                logger.error(f"Error stopping service {name}: {e}")

        logger.info("Production pipeline stopped")

    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.stop_pipeline())

    async def get_status(self) -> Dict[str, Any]:
        """Get pipeline status."""
        status = {
            "running": self.running,
            "uptime": (
                (datetime.now() - self.metrics["start_time"]).total_seconds()
                if self.metrics["start_time"]
                else 0
            ),
            "services": {},
            "metrics": self.metrics.copy(),
            "config": self.config.copy(),
        }

        for name, service in self.services.items():
            service_status = {"status": "unknown"}
            if hasattr(service, "get_status"):
                try:
                    service_status = await service.get_status()
                except Exception as e:
                    service_status = {"status": "error", "error": str(e)}
            status["services"][name] = service_status

        return status


_orchestrator: Optional[ProductionOrchestrator] = None


def get_orchestrator(config: Optional[Dict[str, Any]] = None) -> ProductionOrchestrator:
    """Get singleton orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ProductionOrchestrator(config)
    elif config:
        _orchestrator.config.update(config)
    return _orchestrator


async def start_production_pipeline(config: Optional[Dict[str, Any]] = None) -> None:
    """Start the complete production pipeline."""
    orchestrator = get_orchestrator(config)
    try:
        await orchestrator.initialize_services()
        await orchestrator.start_services()
        await orchestrator.connect_data_flow()
        await orchestrator.start_pipeline()

        while orchestrator.running:
            await asyncio.sleep(1)
    except Exception as e:
        logger.critical(f"Failed to start production pipeline: {e}")
        await orchestrator.stop_pipeline()
        raise


async def stop_production_pipeline() -> None:
    """Stop the production pipeline."""
    global _orchestrator
    orchestrator = get_orchestrator()
    await orchestrator.stop_pipeline()
    _orchestrator = None