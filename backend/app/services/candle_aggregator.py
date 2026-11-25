import asyncio
import logging
import time
from collections import deque
from typing import Any, Dict, Optional

import msgpack
import zmq
import zmq.asyncio

from .cvd_engine import get_cvd_engine
from .zmq_publisher import get_zmq_publisher

logger = logging.getLogger(__name__)

class CandleAggregator:
    """Aggregate TICK_* messages into 1m and 5m bars with ring buffers."""

    def __init__(self, port: int = 5555):
        self.port = port
        self.context: Optional[zmq.asyncio.Context] = None
        self.socket: Optional[Any] = None
        self._initialize_subscriber()

        self.bars_1m: Dict[int, deque] = {}
        self.bars_5m: Dict[int, deque] = {}
        self.current_1m: Dict[int, Dict[str, Any]] = {}
        self.current_5m: Dict[int, Dict[str, Any]] = {}
        self.history_readiness: Dict[int, Dict[str, Any]] = {}
        self.ticks_received = 0
        self.bars_closed = 0

        self.running = False
        self.task: Optional[asyncio.Task] = None
        self._last_prune_time = time.time()

    def _initialize_subscriber(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close(0)
            except Exception:
                pass
            finally:
                self.socket = None

        if self.context is not None:
            try:
                self.context.term()
            except Exception:
                pass
            finally:
                self.context = None

        self.context = zmq.asyncio.Context()
        self.socket = self.context.socket(zmq.SUB)
        self.socket.setsockopt(zmq.RCVHWM, 10000)
        self.socket.setsockopt(zmq.TCP_KEEPALIVE, 1)
        self.socket.connect(f"tcp://127.0.0.1:{self.port}")
        self.socket.setsockopt_string(zmq.SUBSCRIBE, "TICK_")

    async def start(self) -> None:
        if self.running and self.task is not None and not self.task.done():
            return

        if self.socket is None or self.context is None or self.socket.closed:
            self._initialize_subscriber()

        self.running = True
        self.task = asyncio.create_task(self.run())
        logger.info("CandleAggregator started")

    async def stop(self) -> None:
        self.running = False

        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None

        if self.socket is not None:
            try:
                self.socket.close(0)
            except Exception:
                pass
            finally:
                self.socket = None

        if self.context is not None:
            try:
                self.context.term()
            except Exception:
                pass
            finally:
                self.context = None

        logger.info("CandleAggregator stopped")

    async def run(self) -> None:
        """Main recv loop.

        IMPORTANT: zmq.asyncio sockets MUST be awaited WITHOUT zmq.NOBLOCK.
        Using NOBLOCK with asyncio defeats the integration â€” the socket raises
        zmq.Again immediately instead of suspending the coroutine, causing a
        busy-wait that never actually reads any messages from the publisher.
        """
        logger.info("CandleAggregator listening on TICK_* topics (zmq.asyncio, no NOBLOCK)")
        _tick_counter = 0
        while self.running:
            try:
                socket = self.socket
                if socket is None:
                    await asyncio.sleep(0.05)
                    continue

                # Correct pattern: await without NOBLOCK â€” asyncio suspends here
                # until a message arrives, yielding the event loop to other tasks.
                parts = await socket.recv_multipart()
                if len(parts) < 2:
                    continue
                payload = parts[1]
                tick = msgpack.unpackb(payload, raw=False)
                self.process_tick(tick)
                _tick_counter += 1

                # Periodic pruning â€” every 10,000 ticks (~every few minutes at
                # market-open pace) instead of coupling it to zmq.Again.
                if _tick_counter >= 10_000:
                    _tick_counter = 0
                    now = time.time()
                    if now - self._last_prune_time > 3600:
                        self._trim_inactive_tokens(now)
                        self._last_prune_time = now

            except asyncio.CancelledError:
                break
            except zmq.ZMQError as exc:
                if exc.errno in (zmq.ENOTSOCK, zmq.ETERM):
                    self.running = False
                    break
                logger.error("CandleAggregator ZMQError: %s", exc)
                await asyncio.sleep(0.1)
            except Exception as exc:
                logger.error("CandleAggregator error: %s", exc)
                await asyncio.sleep(0.1)

    def _init_bar(self, symbol: str, ltp: float, ts_start: int) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "open": ltp,
            "high": ltp,
            "low": ltp,
            "close": ltp,
            "volume": 0,
            "day_cumulative_volume": 0,
            "cvd": 0,
            "tick_vol": 0,
            "start_time": ts_start,
            "last_tick_at": time.time(),
        }

    def _update_bar(self, bar: Dict[str, Any], tick: Dict[str, Any]) -> None:
        ltp = float(tick["ltp"])
        bar["high"] = max(bar["high"], ltp)
        bar["low"] = min(bar["low"], ltp)
        bar["close"] = ltp
        bar["volume"] += max(0, int(tick.get("tick_vol", tick.get("tick_volume", 0))))
        bar["day_cumulative_volume"] = max(
            bar["day_cumulative_volume"],
            int(tick.get("volume", bar["day_cumulative_volume"]))
        )
        bar["cvd"] = float(tick.get("cvd", bar.get("cvd", 0)))
        bar["tick_vol"] = bar["volume"]
        bar["last_tick_at"] = time.time()

    def process_tick(self, tick: Dict[str, Any]) -> None:
        self.ticks_received += 1
        if tick.get("is_index"):
            return
        try:
            from .short_screener_engine import get_short_screener_engine
            screener = get_short_screener_engine()
            if screener.is_active:
                screener.on_tick(tick)
        except Exception:
            pass

        token = int(tick["token"])
        symbol = str(tick.get("symbol", token))
        ts = float(tick.get("timestamp", time.time()))
        ltp = float(tick["ltp"])

        current_1m_boundary = (int(ts) // 60) * 60
        current_5m_boundary = (int(ts) // 300) * 300

        current_1m_bar = self.current_1m.get(token)
        if current_1m_bar is not None and ts < float(current_1m_bar.get("start_time", current_1m_boundary)):
            logger.warning(
                "Dropping late tick due to out-of-order timestamp token=%s ts=%s current_bar_start=%s",
                token,
                ts,
                current_1m_bar.get("start_time"),
            )
            return

        if current_1m_bar is None:
            self.current_1m[token] = self._init_bar(symbol, ltp, current_1m_boundary)
            if token not in self.bars_1m:
                self.bars_1m[token] = deque(maxlen=100)
            current_1m_bar = self.current_1m[token]
        elif current_1m_boundary > current_1m_bar["start_time"]:
            self.bars_1m[token].append(current_1m_bar)
            self.bars_closed += 1
            self.emit_bar_close(token, "1m", current_1m_bar)
            self.current_1m[token] = self._init_bar(symbol, ltp, current_1m_boundary)
            current_1m_bar = self.current_1m[token]

        self._update_bar(current_1m_bar, tick)

        current_5m_bar = self.current_5m.get(token)
        if current_5m_bar is None:
            self.current_5m[token] = self._init_bar(symbol, ltp, current_5m_boundary)
            if token not in self.bars_5m:
                self.bars_5m[token] = deque(maxlen=100)
            current_5m_bar = self.current_5m[token]
        elif current_5m_boundary > current_5m_bar["start_time"]:
            self.bars_5m[token].append(current_5m_bar)
            self.bars_closed += 1
            self.emit_bar_close(token, "5m", current_5m_bar)
            self.current_5m[token] = self._init_bar(symbol, ltp, current_5m_boundary)
            current_5m_bar = self.current_5m[token]

        self._update_bar(current_5m_bar, tick)

    def hydrate_history(
        self,
        token: int,
        symbol: str,
        bars_1m: list[Dict[str, Any]],
        bars_5m: list[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Replace the completed-bar working windows with validated API history."""
        def normalize(rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
            unique = {float(row["start_time"]): dict(row) for row in rows}
            return [unique[key] for key in sorted(unique)[-100:]]

        if bars_1m:
            one_minute = normalize(bars_1m)
            self.bars_1m[token] = deque(one_minute, maxlen=100)
            self.current_1m.pop(token, None)
        else:
            one_minute = list(self.bars_1m.get(token, ()))

        if bars_5m:
            five_minute = normalize(bars_5m)
            self.bars_5m[token] = deque(five_minute, maxlen=100)
            self.current_5m.pop(token, None)
        else:
            five_minute = list(self.bars_5m.get(token, ()))

        if bars_5m and five_minute:
            self._hydrate_downstream_engines(token, symbol, five_minute)

        readiness = {
            "symbol": symbol,
            "bars_1m": len(one_minute),
            "bars_5m": len(five_minute),
            "required": 100,
            "ready": len(one_minute) >= 100 and len(five_minute) >= 100,
            "state": "READY" if len(one_minute) >= 100 and len(five_minute) >= 100 else "INSUFFICIENT_HISTORY",
            "loaded_at": time.time(),
        }
        self.history_readiness[token] = readiness
        return readiness

    def _hydrate_downstream_engines(
        self,
        token: int,
        symbol: str,
        bars_5m: list[Dict[str, Any]],
    ) -> None:
        """Hydrate vol_vwap_engine and compression_engine from historical 5m bars."""
        if not bars_5m:
            return
        try:
            from services.vol_vwap import get_vol_vwap_engine
            from services.compression_engine import get_compression_engine

            vwap_eng = get_vol_vwap_engine()
            comp_eng = get_compression_engine()

            # 1. Reset symbol states in downstream engines before replaying history
            comp_eng.reset_session(symbol)
            vwap_eng.reset_session(symbol)

            # 3. Sequential replay of 5m bars to seed VWAP, ATR, Volatility, and Compression
            prev_bars: list[Dict[str, Any]] = []
            for bar in bars_5m:
                b = {
                    "symbol": symbol,
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                    "volume": float(bar.get("volume", 0.0)),
                    "day_cumulative_volume": float(bar.get("day_cumulative_volume", bar.get("volume", 0.0))),
                    "start_time": float(bar.get("start_time", 0.0)),
                }

                micro = vwap_eng.update_bar(symbol, b, prev_bars, cvd=0.0)
                ewma_vol = float(getattr(micro, "ewma_vol", 0.015))
                micro_state = getattr(micro, "micro_state", "Neutral")

                comp_eng.add_volatility(symbol, ewma_vol)
                comp_eng.update_bar(
                    symbol=symbol,
                    bar=b,
                    cvd_value=0.0,
                    vwap_micro_state=micro_state,
                    regime="Calm Uptrend",
                )
                prev_bars.append(b)
        except Exception as exc:
            logger.debug("Failed downstream hydration for %s: %s", symbol, exc)

    def clear_history(self) -> None:
        """Clear hydrated and completed-bar state before an explicit reload."""
        self.bars_1m.clear()
        self.bars_5m.clear()
        self.current_1m.clear()
        self.current_5m.clear()
        self.history_readiness.clear()

    def emit_bar_close(self, token: int, timeframe: str, bar_data: Dict[str, Any]) -> None:
        logger.debug("[%s] closed bar token=%s close=%s volume=%s", timeframe, token, bar_data["close"], bar_data["volume"])
        try:
            cvd_engine = get_cvd_engine()
            if timeframe == "5m":
                cvd_engine.snapshot_bar(token, bar_data["close"])

            cvd_state = cvd_engine._states.get(token)
            bar_payload = dict(bar_data)
            bar_payload["running_cvd"] = int(getattr(cvd_state, "running_cvd", 0))
            bar_payload["cvd_bullish_div"] = bool(getattr(cvd_state, "last_bullish_div", False))
            bar_payload["cvd_bearish_div"] = bool(getattr(cvd_state, "last_bearish_div", False))

            publisher = get_zmq_publisher()
            if not publisher.running:
                return
            payload = {
                "token": token,
                "symbol": bar_data.get("symbol"),
                "timeframe": timeframe,
                "bar": bar_payload,
                "running_cvd": bar_payload["running_cvd"],
                "cvd_bullish_div": bar_payload["cvd_bullish_div"],
                "cvd_bearish_div": bar_payload["cvd_bearish_div"],
                "timestamp": bar_data.get("start_time", time.time()),
            }
            # B-3 fix: create_task only works when called from a running event loop
            # (i.e., from within an async context). Because process_tick() is a sync
            # method called from run() which is async, a loop is always running here,
            # but we guard explicitly to make unit-testing safe.
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(publisher.publish(f"BAR_{token}_{timeframe}", payload))
            except RuntimeError:
                # No running loop â€” best-effort schedule for test environments.
                import asyncio as _asyncio
                _asyncio.get_event_loop().run_until_complete(
                    publisher.publish(f"BAR_{token}_{timeframe}", payload)
                )
        except Exception as exc:
            logger.debug("Failed to publish bar close event: %s", exc)
    def _trim_inactive_tokens(self, now: float) -> None:
        """
        Phase 2 Fix: Prevent unbounded memory leaks from orphaned options
        contracts or removed tickers. Trims tokens with no updates in >2 hours.
        """
        cutoff = now - 7200
        stale_tokens = []
        for token, bar in self.current_1m.items():
            if bar.get("start_time", 0) < cutoff:
                stale_tokens.append(token)

        for t in stale_tokens:
            self.bars_1m.pop(t, None)
            self.bars_5m.pop(t, None)
            self.current_1m.pop(t, None)
            self.current_5m.pop(t, None)

        if stale_tokens:
            logger.info("CandleAggregator pruned %d inactive tokens.", len(stale_tokens))

    async def health_check(self) -> bool:
        try:
            socket = self.socket
            return self.running and socket is not None and not socket.closed
        except Exception:
            return False

    async def get_status(self) -> Dict[str, Any]:
        socket = self.socket
        return {
            "status": "running" if self.running else "stopped",
            "port": self.port,
            "active_1m_symbols": len(self.current_1m),
            "active_5m_symbols": len(self.current_5m),
            "socket_closed": True if socket is None else socket.closed,
            "ticks_received": self.ticks_received,
            "bars_closed": self.bars_closed,
            "history_required_bars": 100,
            "history_ready_symbols": sum(1 for value in self.history_readiness.values() if value.get("ready")),
            "history_warming_symbols": sum(1 for value in self.history_readiness.values() if not value.get("ready")),
            "history_readiness": list(self.history_readiness.values())[:100],
            "retention": "rolling_100_per_timeframe",
        }

_candle_aggregator: Optional[CandleAggregator] = None

def get_candle_aggregator() -> CandleAggregator:
    global _candle_aggregator
    if _candle_aggregator is None:
        _candle_aggregator = CandleAggregator()
    return _candle_aggregator
