"""
Institutional Live Algorithmic Watchdog for SHORTS
===================================================
Real-time microstructure and risk management engine for short-selling positions:
- Direction-pure short evaluation math (no is_short branching)
- 1.0% Hard Stop Loss Ceiling above entry (Zero-compromise loss ceiling: max ₹600 loss per ₹60k trade)
- Dynamic Breakeven Lock at +0.8% profit (SL moves down to -0.10% below entry to lock in gains + fees)
- Structural Resistance Trailing at +1.5% profit (trails along 5m VWAP & 5m 20-EMA resistance ceilings)
- Oversold Momentum & RSI <= 20 Exhaustion vs Runner logic (Orderbook Depth + CVD Delta)
- Shares CapitalSlotManager (₹1.80L, 3 slots max system-wide) and Dhan200DepthManager with Longs
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config.trading_config import CAPITAL_CONFIG
from logger import logger
from services.capital_slot_manager import CapitalSlotManager, get_capital_slot_manager
from services.dhan_mcp_client import DhanMCPClient, get_dhan_mcp_client
from services.telegram_hitl_service import TelegramHITLService, get_telegram_hitl_service


@dataclass(slots=True)
class WatchdogPositionShort:
    symbol: str
    security_id: str
    entry_price: float
    quantity: int
    order_id: str
    action: str = "SELL"
    entry_time: float = field(default_factory=time.time)
    hard_stop_loss: float = 0.0
    current_stop_loss: float = 0.0
    target_1: float = 0.0
    target_2: float = 0.0
    peak_price: float = 0.0  # For shorts, peak_price tracks the LOWEST price achieved (maximum favorable dip)
    breakeven_locked: bool = False
    is_runner_mode: bool = False
    status: str = "ACTIVE"
    depth_level_active: str = "20-level"
    is_external: bool = False
    is_paper: bool = False
    last_market_update_at: float = field(default_factory=time.time)
    last_telemetry_ts: float = 0.0
    latest_ltp: float = 0.0
    _flow_state: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Pure short initialization: stop loss is strictly ABOVE entry
        if self.hard_stop_loss <= 0.0:
            self.hard_stop_loss = round(self.entry_price * (1.0 + CAPITAL_CONFIG.MAX_HARD_STOP_LOSS_PCT), 2)
        if self.current_stop_loss <= 0.0:
            self.current_stop_loss = self.hard_stop_loss
        if self.peak_price <= 0.0:
            self.peak_price = self.entry_price
        if self.latest_ltp <= 0.0:
            self.latest_ltp = self.entry_price

    def to_dict(self, current_ltp: Optional[float] = None) -> Dict[str, Any]:
        """Serialize position state for short position."""
        display_price = current_ltp if (current_ltp and current_ltp > 0) else (self.latest_ltp if self.latest_ltp > 0 else self.peak_price)
        pnl = round((self.entry_price - display_price) * self.quantity, 2)
        pnl_pct = round(((self.entry_price - display_price) / max(0.01, self.entry_price)) * 100.0, 2)
        return {
            "symbol": self.symbol,
            "security_id": self.security_id,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "order_id": self.order_id,
            "action": self.action,
            "hard_stop_loss": self.hard_stop_loss,
            "current_stop_loss": self.current_stop_loss,
            "target_1": self.target_1,
            "target_2": self.target_2,
            "peak_price": self.peak_price,
            "current_price": display_price,
            "breakeven_locked": self.breakeven_locked,
            "is_runner_mode": self.is_runner_mode,
            "status": self.status,
            "allocated_capital": CAPITAL_CONFIG.PER_TRADE_ALLOCATION,
            "entry_time": self.entry_time,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "depth_level_active": self.depth_level_active,
            "is_external": self.is_external,
            "is_paper": self.is_paper,
            "last_market_update_at": self.last_market_update_at,
        }


class AlgoLiveWatchdogShorts:
    """
    Dedicated Watchdog evaluation engine for short trades.
    Shares the single CapitalSlotManager, DepthManager, MCP client, and Telegram bot.
    """

    def __init__(
        self,
        mcp_client: Optional[DhanMCPClient] = None,
        telegram_service: Optional[TelegramHITLService] = None,
        slot_manager: Optional[CapitalSlotManager] = None,
    ):
        self.mcp = mcp_client or get_dhan_mcp_client()
        self.telegram = telegram_service or get_telegram_hitl_service()
        self.slots = slot_manager or get_capital_slot_manager()
        self.active_positions: Dict[str, WatchdogPositionShort] = {}
        self._watchdog_task: Optional[asyncio.Task] = None

    @property
    def _depth_manager(self):
        """Reuses the single Dhan200DepthManager instance managed by AlgoLiveWatchdog."""
        try:
            from services.live_algo_watchdog import get_algo_live_watchdog
            return getattr(get_algo_live_watchdog(), "_depth_manager", None)
        except Exception:
            return None

    def get_active_positions(self) -> List[Dict[str, Any]]:
        from services.candle_aggregator import get_candle_aggregator
        agg = get_candle_aggregator()
        orch = None
        try:
            from services.orchestrator import get_orchestrator
            orch = get_orchestrator()
        except Exception:
            pass

        result = []
        for pos in self.active_positions.values():
            ltp = None
            try:
                # Auto-repair invalid security_id
                if (not pos.security_id or pos.security_id == "0") and pos.symbol:
                    from services.dhan_instrument_resolver import get_instrument_resolver
                    res = get_instrument_resolver()
                    sid = res.get_security_id(pos.symbol)
                    if sid:
                        pos.security_id = str(sid)

                # 1. Direct sub-second wire feed from Dhan ticker
                if orch and orch.dhan_ticker and hasattr(orch.dhan_ticker, "latest_quotes"):
                    quote = orch.dhan_ticker.latest_quotes.get(pos.symbol)
                    if not quote and pos.security_id and pos.security_id != "0":
                        from services.dhan_instrument_resolver import get_instrument_resolver
                        res = get_instrument_resolver()
                        c_sym = res.get_symbol(pos.security_id)
                        if c_sym:
                            quote = orch.dhan_ticker.latest_quotes.get(c_sym)
                    if quote and float(quote.get("ltp", 0)) > 0:
                        ltp = float(quote["ltp"])

                # 2. CandleAggregator forming 1m bar
                if not ltp and pos.security_id and pos.security_id != "0":
                    token = int(pos.security_id)
                    curr_bar = agg.current_1m.get(token)
                    if curr_bar and float(curr_bar.get("close", 0)) > 0:
                        ltp = float(curr_bar["close"])

                # 3. CandleAggregator last closed 1m bar
                if not ltp and pos.security_id and pos.security_id != "0":
                    token = int(pos.security_id)
                    bars = agg.bars_1m.get(token)
                    if bars and len(bars) > 0 and float(bars[-1].get("close", 0)) > 0:
                        ltp = float(bars[-1]["close"])

                # 4. Check MCP quotes cache
                if not ltp and pos.security_id and pos.security_id != "0":
                    if hasattr(self.mcp, "_quotes_cache") and pos.security_id in self.mcp._quotes_cache:
                        cached_entry = self.mcp._quotes_cache[pos.security_id][1]
                        if cached_entry and float(cached_entry.get("ltp", 0)) > 0:
                            ltp = float(cached_entry["ltp"])

                # 5. Position cached latest_ltp
                if not ltp and getattr(pos, "latest_ltp", 0) > 0:
                    ltp = pos.latest_ltp
            except Exception:
                pass

            if ltp and ltp > 0:
                pos.latest_ltp = ltp
            result.append(pos.to_dict(current_ltp=ltp))
        return result

    def restore_persisted_positions(self) -> int:
        from db_store import get_lifecycle_positions
        restored = 0
        for data in get_lifecycle_positions():
            if str(data.get("action", "")).upper() != "SELL" or data.get("symbol") in self.active_positions:
                continue
            pos = WatchdogPositionShort(
                symbol=data["symbol"],
                security_id=str(data.get("security_id", "")),
                entry_price=float(data["entry_price"]),
                quantity=int(data["quantity"]),
                order_id=str(data["order_id"]),
                entry_time=float(data.get("entry_time", time.time())),
                hard_stop_loss=float(data.get("hard_stop_loss", 0.0)),
                current_stop_loss=float(data.get("current_stop_loss", 0.0)),
                target_1=float(data.get("target_1", 0.0)),
                target_2=float(data.get("target_2", 0.0)),
                peak_price=float(data.get("peak_price", data["entry_price"])),
                breakeven_locked=bool(data.get("breakeven_locked", False)),
                is_runner_mode=bool(data.get("is_runner_mode", False)),
                status=str(data.get("status", "ACTIVE")),
                is_external=bool(data.get("is_external", False)),
                is_paper=bool(data.get("is_paper", False)),
                last_market_update_at=float(data.get("last_market_update_at", time.time())),
            )
            self.active_positions[pos.symbol] = pos
            self._sync_ticker_feed(pos.symbol, pos.security_id)
            self._ensure_candle_history(pos.symbol, pos.security_id)
            restored += 1
        if restored:
            self.start_watchdog_loop()
        return restored

    def start_watchdog_loop(self) -> None:
        """Starts background asyncio loop for shorts tick evaluation."""
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info("AlgoLiveWatchdogShorts: Background tick evaluation loop started.")

    async def _watchdog_loop(self) -> None:
        """Continuous 1-second evaluation loop for all active short positions."""
        from services.candle_aggregator import get_candle_aggregator
        aggregator = get_candle_aggregator()

        while True:
            try:
                await asyncio.sleep(1.0)
                if not self.active_positions:
                    continue

                for symbol, pos in list(self.active_positions.items()):
                    if pos.status not in {"ACTIVE", "PROTECTION_DEGRADED"}:
                        continue
                    try:
                        token = int(pos.security_id)
                        ltp = 0.0
                        last_update = 0.0

                        # 1. Direct sub-second wire feed from Dhan ticker
                        try:
                            from services.orchestrator import get_orchestrator
                            orch = get_orchestrator()
                            if orch and orch.dhan_ticker and hasattr(orch.dhan_ticker, "latest_quotes"):
                                quote = orch.dhan_ticker.latest_quotes.get(symbol)
                                if quote and float(quote.get("ltp", 0)) > 0:
                                    ltp = float(quote["ltp"])
                                    last_update = float(quote.get("timestamp", 0.0))
                        except Exception:
                            pass

                        # 2. Prefer current forming 1m bar
                        curr_bar = aggregator.current_1m.get(token)
                        if curr_bar:
                            bar_update = float(curr_bar.get("last_tick_at", curr_bar.get("start_time", 0.0)))
                            if bar_update > last_update:
                                last_update = bar_update
                                if float(curr_bar.get("close", 0)) > 0:
                                    ltp = float(curr_bar["close"])

                        # 3. Fall back to last closed bar
                        if ltp <= 0:
                            bars = aggregator.bars_1m.get(token)
                            if bars and len(bars) > 0 and float(bars[-1].get("close", 0)) > 0:
                                ltp = float(bars[-1]["close"])

                        # 4. Fall back to Dhan REST quote
                        if ltp <= 0:
                            try:
                                quotes = await self.mcp.get_quotes(pos.security_id)
                                q_item = quotes.get(str(pos.security_id))
                                if q_item and float(q_item.get("ltp", 0)) > 0:
                                    ltp = float(q_item["ltp"])
                                    last_update = float(q_item.get("timestamp", time.time()))
                                    # Ensure ticker has this price for sub-second lookups
                                    try:
                                        from services.orchestrator import get_orchestrator
                                        orch = get_orchestrator()
                                        if orch and orch.dhan_ticker and hasattr(orch.dhan_ticker, "latest_quotes"):
                                            orch.dhan_ticker.latest_quotes[symbol] = {
                                                "ltp": ltp,
                                                "volume": q_item.get("volume", 0),
                                                "timestamp": last_update,
                                            }
                                    except Exception:
                                        pass
                            except Exception as rest_err:
                                logger.debug("WatchdogShorts REST quote fallback failed for %s: %s", symbol, rest_err)

                        # 5. Check feed freshness & auto-heal to ACTIVE
                        now = time.time()
                        if last_update > 0 and (now - last_update <= 30.0):
                            if pos.status == "PROTECTION_DEGRADED":
                                pos.status = "ACTIVE"
                                try:
                                    from db_store import update_lifecycle_position_status
                                    update_lifecycle_position_status(symbol, "ACTIVE")
                                    logger.info("Shorts watchdog feed recovered for %s; position status restored to ACTIVE.", symbol)
                                except Exception:
                                    pass
                        elif last_update > 0 and (now - last_update > 30.0):
                            if pos.status != "PROTECTION_DEGRADED":
                                pos.status = "PROTECTION_DEGRADED"
                                try:
                                    from db_store import update_lifecycle_position_status
                                    update_lifecycle_position_status(symbol, "PROTECTION_DEGRADED")
                                except Exception:
                                    pass
                                logger.warning(
                                    "Shorts watchdog feed quiet (>30s) for %s; retaining position supervision in degraded state.",
                                    symbol,
                                )

                        if ltp <= 0:
                            continue

                        pos.latest_ltp = ltp

                        # Extract indicators for evaluate_tick
                        vwap_5m = None
                        ema20_5m = None
                        rsi_5m = None
                        cvd_delta = 0.0
                        bid_ask_ratio = 1.0

                        try:
                            # 1. 5m closes for EMA and RSI
                            bars_5m = aggregator.bars_5m.get(token, [])
                            if bars_5m:
                                closes_5m = [float(b.get("close", 0)) for b in bars_5m if float(b.get("close", 0)) > 0]
                                if closes_5m:
                                    closes_5m.append(ltp)
                                    if len(closes_5m) >= 20:
                                        from services.multi_tf_analysis import compute_ema
                                        emas = compute_ema(closes_5m, 20)
                                        ema20_5m = emas[-1]
                                    if len(closes_5m) >= 15:
                                        from routers.analysis import _compute_rsi_wilder
                                        rsi_5m = _compute_rsi_wilder(closes_5m)

                            # 2. VWAP 5m
                            from services.vol_vwap import get_vol_vwap_engine
                            vwap_eng = get_vol_vwap_engine()
                            st = vwap_eng._states.get(symbol)
                            if st and st.last_result and st.last_result.bands:
                                vwap_5m = st.last_result.bands.vwap

                            # 3. CVD Delta
                            from services.cvd_engine import get_cvd_engine
                            cvd_eng = get_cvd_engine()
                            cvd_delta = cvd_eng.get_cvd(token) or 0.0
                        except Exception as e:
                            logger.debug("Error computing shorts watchdog indicators for %s: %s", symbol, e)

                        await self.evaluate_tick(
                            symbol=symbol,
                            ltp=ltp,
                            vwap_5m=vwap_5m,
                            ema20_5m=ema20_5m,
                            rsi_5m=rsi_5m,
                            cvd_delta=cvd_delta,
                            bid_ask_ratio=bid_ask_ratio,
                        )

                    except Exception as tick_exc:
                        logger.debug("AlgoLiveWatchdogShorts tick loop error for %s: %s", symbol, tick_exc)

            except asyncio.CancelledError:
                break
            except Exception as loop_exc:
                logger.error("AlgoLiveWatchdogShorts: Unexpected error in watchdog loop: %s", loop_exc)
                await asyncio.sleep(1.0)

    def register_position(
        self,
        symbol: str,
        security_id: str,
        entry_price: float,
        quantity: int,
        order_id: str,
        action: str = "SELL",
        stop_loss: Optional[float] = None,
        target_1: Optional[float] = None,
        target_2: Optional[float] = None,
        is_external: bool = False,
        is_paper: bool = False,
    ) -> WatchdogPositionShort:
        """Registers a newly filled short position under active surveillance."""
        pos = WatchdogPositionShort(
            symbol=symbol,
            security_id=security_id,
            entry_price=entry_price,
            quantity=quantity,
            order_id=order_id,
            action="SELL",
            hard_stop_loss=stop_loss if (stop_loss and stop_loss > entry_price) else 0.0,
            target_1=target_1 or 0.0,
            target_2=target_2 or 0.0,
            is_external=is_external,
            is_paper=is_paper,
        )
        self.active_positions[symbol] = pos
        from db_store import save_lifecycle_position
        save_lifecycle_position(pos.to_dict(), status=pos.status)
        self.slots.occupy_slot(
            symbol=symbol,
            security_id=security_id,
            entry_price=entry_price,
            quantity=quantity,
            order_id=order_id,
        )

        logger.info(
            "🛡️ WatchdogShorts Registered: SELL %s (Qty: %d @ ₹%.2f, Ceiling SL: ₹%.2f)",
            symbol, quantity, entry_price, pos.hard_stop_loss
        )

        # Update 200-depth candidate pool across all active positions
        self._sync_depth_manager()
        self._sync_ticker_feed(symbol, security_id)

        # ── Phase 1 #4: Seed RSI and EMA with prior intraday history ─────────
        self._ensure_candle_history(symbol, security_id)

        self.start_watchdog_loop()
        return pos

    def _ensure_candle_history(self, symbol: str, security_id: str) -> None:
        """Asynchronously seeds 5m and 1m candle history if not already cached (Phase 1 #4)."""
        try:
            from services.candle_aggregator import get_candle_aggregator
            agg = get_candle_aggregator()
            token = int(security_id)
            if token not in agg.bars_5m or len(agg.bars_5m[token]) < 20:
                asyncio.create_task(self._seed_symbol_history(symbol, security_id))
        except Exception:
            pass

    async def _seed_symbol_history(self, symbol: str, security_id: str) -> None:
        try:
            import os
            from services.dhan_historical import DhanHistoricalClient, fetch_symbol_warmup
            from services.candle_aggregator import get_candle_aggregator
            from services.dhan_token_service import get_dhan_token
            client_id = os.getenv("DHAN_CLIENT_ID") or os.getenv("DHAN_USER_ID")
            access_token = get_dhan_token() or os.getenv("DHAN_ACCESS_TOKEN")
            if not client_id or not access_token:
                return
            client = DhanHistoricalClient(
                client_id,
                access_token,
                base_url=os.getenv("DHAN_BASE_URL", "https://api.dhan.co/v2"),
            )
            history = await fetch_symbol_warmup(
                client,
                security_id=security_id,
                exchange_segment="NSE_EQ",
            )
            aggregator = get_candle_aggregator()
            aggregator.hydrate_history(int(security_id), symbol, history.get("1m", []), history.get("5m", []))
            logger.info("WatchdogShorts: seeded historical 5m/1m candles for %s (RSI & EMA ready on tick 1)", symbol)
        except Exception as exc:
            logger.debug("WatchdogShorts: could not seed candle history for %s: %s", symbol, exc)

    async def _confirm_stop_update(self, pos: WatchdogPositionShort, new_stop: float, reason: str = "") -> bool:
        if pos.is_external:
            pos.current_stop_loss = new_stop
            try:
                from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts
                shortlist_shorts = get_ai_shortlist_service_shorts()
                pnl_pct = ((pos.entry_price - pos.peak_price) / max(0.01, pos.entry_price)) * 100.0

                if "BREAKEVEN" in reason:
                    msg = (
                        f"🛡️ Lower your stop loss down to ₹{new_stop:.2f}! "
                        f"We're in profit (+{pnl_pct:.1f}%), so lock it in right now on Dhan. "
                        f"This short is now completely risk-free!"
                    )
                elif "RUNNER" in reason:
                    msg = (
                        f"🚀 Massive breakdown momentum! Lower your stop loss down to ₹{new_stop:.2f} on Dhan "
                        f"to ride the dump while protecting your gains!"
                    )
                else:
                    msg = (
                        f"📉 Trail your stop loss down to ₹{new_stop:.2f}! "
                        f"Price is falling with heavy resistance above. Lower your stop on Dhan to lock in higher profit!"
                    )

                shortlist_shorts.add_timeline_event(
                    symbol=pos.symbol,
                    event_type="MANUAL_ADVISORY_SL",
                    message=msg,
                    price_at_event=new_stop,
                    severity="warning",
                )
            except Exception as exc:
                logger.debug("Failed adding short advisory SL timeline event: %s", exc)
            return True

        try:
            result = await self.mcp.modify_stop_loss(pos.order_id, new_stop, pos.quantity)
        except Exception as exc:
            logger.error("Short stop update failed for %s: %s", pos.symbol, exc)
            return False
        if isinstance(result, bool):
            success = result
        elif not isinstance(result, dict):
            logger.warning(
                "Short stop update for %s returned an unstructured acknowledgement; treating it as accepted.",
                pos.symbol,
            )
            success = True
        else:
            success = str(result.get("status", "")).upper() in {"SUCCESS", "MODIFIED", "UPDATED", "FILLED", "TRADED"}

        if success and not pos.is_external:
            try:
                from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts
                shortlist_shorts = get_ai_shortlist_service_shorts()
                pnl_pct = ((pos.entry_price - pos.peak_price) / max(0.01, pos.entry_price)) * 100.0
                if "BREAKEVEN" in reason:
                    auto_msg = f"🛡️ Breakeven secured! Stop loss automatically lowered to ₹{new_stop:.2f} (+{pnl_pct:.1f}%). Short is now risk-free."
                elif "RUNNER" in reason:
                    auto_msg = f"🚀 Momentum breakdown locked! Stop loss automatically lowered to ₹{new_stop:.2f}."
                else:
                    auto_msg = f"📉 Stop loss automatically lowered to ₹{new_stop:.2f} to protect profits."
                shortlist_shorts.add_timeline_event(
                    symbol=pos.symbol,
                    event_type="AUTO_SL_MODIFIED",
                    message=auto_msg,
                    price_at_event=new_stop,
                    severity="info",
                )
            except Exception:
                pass
        return success

    def _fire_depth_flow_events(
        self,
        pos: WatchdogPositionShort,
        ltp: float,
        cvd_delta: float,
        deep_obi: Optional[float],
        banded_obi: Optional[float],
        obi_smooth: Optional[float],
        bid_wall_near: bool,
        ask_wall_near: bool,
        depth_summary: Optional[Dict[str, Any]],
        rsi_5m: Optional[float],
        vwap_5m: Optional[float],
        cvd_total: int,
        strongest_bid_wall: Optional[Dict[str, Any]] = None,
        strongest_ask_wall: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Phase 3: Routes all depth/flow events for SHORTS through the tiered WatchdogAlertEngine.
        Also writes Phase 0 snapshot data to the recorder.
        """
        from services.watchdog_alert_engine import get_alert_engine, AlertSignalBundle
        from services.watchdog_data_recorder import record_alert, record_snapshot

        engine = get_alert_engine()
        now = time.time()

        # ── OBI smoothing and hysteresis (Phase 1 #3 + Phase 3) ───────────────
        effective_obi = banded_obi if (isinstance(banded_obi, (int, float)) and not isinstance(banded_obi, bool)) else (
            deep_obi if (isinstance(deep_obi, (int, float)) and not isinstance(deep_obi, bool)) else None
        )
        smoothed_obi = engine.smooth_obi(pos.symbol, effective_obi)
        prev_zone = engine.get_state(pos.symbol).obi_zone
        obi_zone = engine.obi_zone_with_hysteresis(pos.symbol, smoothed_obi)

        # ── Phase 1 #1 & #2 & #7: CVD delta crossing and normalization ─────────
        if not getattr(pos, "_flow_state", None):
            pos._flow_state = {
                "prev_cvd": cvd_total,
            }
        prev_cvd = pos._flow_state.get("prev_cvd", cvd_total)
        delta_cvd = cvd_total - prev_cvd
        pos._flow_state["prev_cvd"] = cvd_total

        # Normalized CVD (Phase 1 #7: z-score against 20-bar rolling window)
        norm_cvd = None
        try:
            from services.cvd_engine import get_cvd_engine
            raw_norm = get_cvd_engine().get_normalized_cvd(int(pos.security_id))
            if isinstance(raw_norm, (int, float)) and not isinstance(raw_norm, bool):
                norm_cvd = float(raw_norm)
        except Exception:
            pass

        # A "dump" or "surge" is a recent delta crossing or statistical extreme, NOT session total
        cvd_dump = (delta_cvd <= -3000) or (norm_cvd is not None and norm_cvd <= -2.0)
        cvd_surge = (delta_cvd >= 3000) or (norm_cvd is not None and norm_cvd >= 2.0)

        # ── OBI directional signals (hysteresis-gated) ───────────────────────
        obi_bearish = (obi_zone == "SELLERS")
        obi_bullish = (obi_zone == "BUYERS")

        # ── Book flip: zone transitioned directly from opposite side ────────
        book_flip_sell = (obi_zone == "SELLERS" and prev_zone == "BUYERS")
        book_flip_buy  = (obi_zone == "BUYERS"  and prev_zone == "SELLERS")

        # ── VWAP break against SHORT position (price breaks ABOVE VWAP) ───────
        price_breaks_vwap = (vwap_5m is not None and vwap_5m > 0 and ltp > vwap_5m)

        # ── RSI exhaustion zone for SHORT (RSI <= 20) ────────────────────────
        rsi_exhaustion = (rsi_5m is not None and rsi_5m <= 20.0)

        bundle = AlertSignalBundle(
            cvd_dump=cvd_dump,
            cvd_surge=cvd_surge,
            obi_bearish=obi_bearish,
            obi_bullish=obi_bullish,
            sell_wall_near=ask_wall_near,
            buy_wall_near=bid_wall_near,
            book_flip_sell=book_flip_sell,
            book_flip_buy=book_flip_buy,
            price_breaks_vwap_against=price_breaks_vwap,
            rsi_exhaustion=rsi_exhaustion,
            cvd_delta=int(delta_cvd),
            cvd_total=cvd_total,
            deep_obi=effective_obi,
            wall_price=(
                (strongest_bid_wall or {}).get("price")
                if bid_wall_near else
                (strongest_ask_wall or {}).get("price")
            ),
            wall_side="bid" if bid_wall_near else ("ask" if ask_wall_near else None),
            wall_strength=(
                (strongest_bid_wall or {}).get("strength")
                if bid_wall_near else
                (strongest_ask_wall or {}).get("strength")
            ),
            rsi_value=rsi_5m,
            ltp=ltp,
            vwap_5m=vwap_5m,
        )

        def _add_event(symbol, event_type, message, price, severity):
            try:
                from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts
                get_ai_shortlist_service_shorts().add_timeline_event(
                    symbol=symbol, event_type=event_type,
                    message=message, price_at_event=price, severity=severity,
                )
            except Exception:
                pass

        engine.evaluate(
            symbol=pos.symbol,
            direction="SHORT",
            bundle=bundle,
            entry_price=pos.entry_price,
            add_timeline_event_fn=_add_event,
            record_alert_fn=record_alert,
        )

        # ── Phase 0: record snapshot every tick ─────────────────────────────
        try:
            record_snapshot(
                symbol=pos.symbol,
                direction="SHORT",
                ltp=ltp,
                cvd=cvd_total,
                cvd_delta=int(delta_cvd),
                banded_obi=banded_obi,
                deep_obi=deep_obi,
                obi_smooth=smoothed_obi,
                top_walls=(depth_summary or {}).get("walls", [])[:5],
                rsi_5m=rsi_5m,
                vwap_5m=vwap_5m,
            )
        except Exception:
            pass

    def deregister_position(self, symbol: str) -> None:
        """Removes a short position from active monitoring and releases its capital slot."""
        pos = self.active_positions.pop(symbol, None)
        self.slots.release_slot(symbol)
        from db_store import delete_lifecycle_position
        delete_lifecycle_position(symbol)
        if pos:
            logger.info("WatchdogShorts position %s deregistered.", symbol)
        self._sync_depth_manager()

    def _validate_queued_candidate(self, candidate: Dict[str, Any]) -> bool:
        if str(candidate.get("action", "BUY")).upper() == "SELL":
            from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts
            record = get_ai_shortlist_service_shorts().get_trade(candidate.get("symbol", ""))
        else:
            from services.ai_shortlist_service import get_ai_shortlist_service
            record = get_ai_shortlist_service().get_trade(candidate.get("symbol", ""))
        if not record:
            return False
        return str(record.get("status", "")).upper() in {"QUEUED", "ACTIVE", "WATCHING"}

    def _sync_depth_manager(self) -> None:
        """Synchronizes 200-depth manager candidates with active trades from both watchdogs."""
        depth_mgr = self._depth_manager
        if depth_mgr is None:
            return
        try:
            from services.live_algo_watchdog import get_algo_live_watchdog
            longs_watchdog = get_algo_live_watchdog()
            all_candidates = []
            for p in list(longs_watchdog.active_positions.values()) + list(self.active_positions.values()):
                if p.status in {"ACTIVE", "PROTECTION_DEGRADED"}:
                    all_candidates.append({
                        "ExchangeSegment": "NSE_EQ",
                        "SecurityId": p.security_id,
                        "symbol": p.symbol,
                        "score": 100.0,
                    })
            asyncio.create_task(depth_mgr.update_candidates(all_candidates))
        except Exception as exc:
            logger.debug("WatchdogShorts failed to sync depth candidates: %s", exc)

    def _sync_ticker_feed(self, symbol: str, security_id: str) -> None:
        """Dynamically subscribes the instrument to the standard live Dhan ticker feed."""
        try:
            from services.orchestrator import get_orchestrator
            orch = get_orchestrator()
            if orch and orch.dhan_ticker and hasattr(orch.dhan_ticker, "subscribe_instrument"):
                asyncio.create_task(orch.dhan_ticker.subscribe_instrument(symbol, str(security_id)))
        except Exception as exc:
            logger.debug("WatchdogShorts failed to sync ticker feed for %s: %s", symbol, exc)

    async def evaluate_tick(
        self,
        symbol: str,
        ltp: float,
        vwap_5m: Optional[float] = None,
        ema20_5m: Optional[float] = None,
        rsi_5m: Optional[float] = None,
        cvd_delta: float = 0.0,
        bid_ask_ratio: float = 1.0,
    ) -> Dict[str, Any]:
        """
        Direction-pure short quant tick evaluation:
        1. Hard 1.0% stop floor check (price rising ABOVE ceiling)
        2. Breakeven lock (+0.8% profit -> SL ratchets down below entry)
        3. Structural resistance trailing (+1.5% with VWAP & 20-EMA ceilings above price)
        4. RSI <= 20 oversold exhaustion exit vs runner continuation
        5. Trailing stop hit check (price bouncing back UP into trailed SL)
        """
        pos = self.active_positions.get(symbol)
        if not pos or pos.status not in {"ACTIVE", "PROTECTION_DEGRADED"}:
            return {"action": "NOOP"}

        # ── Phase 1 #5: Stale tick rejection ─────────────────────────────────
        # Reject ticks older than 10 seconds to prevent acting on stale data
        from services.candle_aggregator import get_candle_aggregator
        aggregator = get_candle_aggregator()
        try:
            token_id = int(pos.security_id)
            curr_bar = aggregator.current_1m.get(token_id)
            if curr_bar:
                bar_ts = float(curr_bar.get("last_tick_at", curr_bar.get("start_time", 0.0)))
                if bar_ts > 0 and (time.time() - bar_ts) > 10.0:
                    logger.debug("WatchdogShorts: stale tick rejected for %s (age %.1fs)", symbol, time.time() - bar_ts)
                    return {"action": "STALE_TICK"}
        except Exception:
            pass

        pos.latest_ltp = ltp
        pos.last_market_update_at = time.time()
        # Peak tracks lowest price (maximum favorable drop)
        pos.peak_price = min(pos.peak_price, ltp) if pos.peak_price > 0 else ltp
        # PnL for shorts: gain when LTP falls below entry
        pnl_pct = ((pos.entry_price - ltp) / max(0.01, pos.entry_price)) * 100.0

        # Depth intelligence (200-level preferred, 20-level fallback)
        _deep_obi: Optional[float] = None
        _banded_obi: Optional[float] = None
        _depth_level_used: str = "20-level"
        _ask_wall_near_price: bool = False
        _bid_wall_near_price: bool = False
        _strongest_bid_wall: Optional[Dict[str, Any]] = None
        _strongest_ask_wall: Optional[Dict[str, Any]] = None
        depth_summary = None
        _cvd_total = 0

        try:
            from services.cvd_engine import get_cvd_engine
            cvd_eng = get_cvd_engine()
            token_id = int(pos.security_id)
            raw_deep = cvd_eng.get_deep_obi(token_id)
            _deep_obi = float(raw_deep) if isinstance(raw_deep, (int, float)) and not isinstance(raw_deep, bool) else None

            raw_banded = cvd_eng.get_banded_obi(token_id, ltp)
            _banded_obi = float(raw_banded) if isinstance(raw_banded, (int, float)) and not isinstance(raw_banded, bool) else None

            raw_summary = cvd_eng.get_deep_depth_summary(token_id, ltp=ltp)
            depth_summary = raw_summary if isinstance(raw_summary, dict) else None

            raw_cvd = cvd_eng.get_cvd(token_id)
            _cvd_total = int(raw_cvd) if isinstance(raw_cvd, (int, float)) and not isinstance(raw_cvd, bool) else 0

            depth_mgr = self._depth_manager
            if _deep_obi is not None and depth_mgr is not None:
                if (
                    pos.security_id in getattr(depth_mgr, "active_connections", {})
                    or str(pos.security_id) in getattr(depth_mgr, "active_connections", {})
                ):
                    _depth_level_used = "200-level"

            if depth_summary and depth_summary.get("walls"):
                bid_walls_near = []
                ask_walls_near = []
                for wall in depth_summary["walls"][:10]:
                    distance_pct = abs(wall["price"] - ltp) / ltp
                    if distance_pct <= 0.005:
                        if wall.get("side") == "ask":
                            _ask_wall_near_price = True
                            ask_walls_near.append(wall)
                        elif wall.get("side") == "bid":
                            _bid_wall_near_price = True
                            bid_walls_near.append(wall)
                if bid_walls_near:
                    _strongest_bid_wall = max(bid_walls_near, key=lambda w: w.get("strength", 0.0))
                if ask_walls_near:
                    _strongest_ask_wall = max(ask_walls_near, key=lambda w: w.get("strength", 0.0))
        except Exception as exc:
            logger.debug("Short depth enrichment skipped for %s: %s", symbol, exc)

        pos.depth_level_active = _depth_level_used

        # ── Microstructure & Depth Flow Event Anomaly Monitoring ──────────────
        self._fire_depth_flow_events(
            pos=pos,
            ltp=ltp,
            cvd_delta=cvd_delta,
            deep_obi=_deep_obi,
            banded_obi=_banded_obi,
            obi_smooth=None,
            bid_wall_near=_bid_wall_near_price,
            ask_wall_near=_ask_wall_near_price,
            depth_summary=depth_summary,
            rsi_5m=rsi_5m,
            vwap_5m=vwap_5m,
            cvd_total=_cvd_total,
            strongest_bid_wall=_strongest_bid_wall,
            strongest_ask_wall=_strongest_ask_wall,
        )

        # ── 1. HARD MAXIMUM 1.0% STOP LOSS CEILING (Zero Negotiation) ─────────
        # For shorts, SL is breached when LTP rises AT OR ABOVE hard SL
        if ltp >= pos.hard_stop_loss:
            logger.warning(
                "🚨 HARD 1.0%% SL CEILING HIT for SHORT %s @ ₹%.2f (Ceiling: ₹%.2f). Dispatching MCP Cover Exit.",
                symbol, ltp, pos.hard_stop_loss
            )
            return await self._close_trade(pos, ltp, "HARD_1PCT_SL_HIT")

        # ── 2. DYNAMIC BREAKEVEN LOCK (+0.8% Profit) ─────────────────────────
        # When short gains +0.8%, move SL below entry to lock in gain + STT/brokerage fee buffer
        if not pos.breakeven_locked and pnl_pct >= (CAPITAL_CONFIG.BREAKEVEN_TRIGGER_PCT * 100.0):
            be_sl = round(pos.entry_price * (1.0 - CAPITAL_CONFIG.FEE_BUFFER_PCT), 2)
            if not await self._confirm_stop_update(pos, be_sl, reason="BREAKEVEN_LOCKED"):
                pos.status = "PROTECTION_DEGRADED"
                return {"action": "PROTECTION_DEGRADED", "reason": "BREAKEVEN_UPDATE_FAILED"}
            pos.current_stop_loss = min(pos.current_stop_loss, be_sl)
            pos.breakeven_locked = True
            logger.info("🛡️ %s (SHORT): Breakeven Locked (+%.2f%% Gain) -> New SL: ₹%.2f", symbol, pnl_pct, be_sl)
            return {"action": "MODIFY_SL", "new_sl": be_sl, "reason": "BREAKEVEN_LOCKED"}

        # ── 3. STRUCTURAL RESISTANCE TRAILING (+1.5% Profit) ─────────────────
        structural_modified = False
        if pos.breakeven_locked and pnl_pct >= (CAPITAL_CONFIG.STRUCTURAL_TRAIL_PCT * 100.0):
            structural_candidates = [pos.current_stop_loss]

            # For shorts, structural resistance sits ABOVE price (VWAP & EMA acting as ceilings)
            if vwap_5m and vwap_5m > ltp:
                structural_candidates.append(vwap_5m)
            if ema20_5m and ema20_5m > ltp:
                structural_candidates.append(ema20_5m)

            # Ask wall above price acting as institutional resistance ceiling
            if _ask_wall_near_price and depth_summary:
                ask_walls = [
                    w for w in depth_summary.get("walls", [])
                    if w.get("side") == "ask" and w.get("price", 0.0) > ltp
                ]
                if ask_walls:
                    strongest_ask_wall = max(ask_walls, key=lambda w: w.get("strength", 0.0))
                    structural_candidates.append(strongest_ask_wall["price"])
                    logger.info(
                        "🏗️ %s (SHORT): Ask wall @ ₹%.2f (strength %.1fx) used as structural resistance shelf.",
                        symbol, strongest_ask_wall["price"], strongest_ask_wall.get("strength", 1.0)
                    )

            # The tightest/safest resistance ceiling for a short is the MINIMUM candidate above price
            best_structural_shelf = round(min(structural_candidates), 2)
            if best_structural_shelf < pos.current_stop_loss:  # ratchet DOWN towards price
                if not await self._confirm_stop_update(pos, best_structural_shelf, reason="STRUCTURAL_TRAIL"):
                    pos.status = "PROTECTION_DEGRADED"
                    return {"action": "PROTECTION_DEGRADED", "reason": "STRUCTURAL_UPDATE_FAILED"}
                pos.current_stop_loss = best_structural_shelf
                logger.info(
                    "📉 %s (SHORT): Trailed SL DOWN to structural resistance -> ₹%.2f",
                    symbol, best_structural_shelf
                )
                structural_modified = True
                # If RSI is not oversold, return early; otherwise fall through to check exhaustion
                if rsi_5m is None or rsi_5m > 20.0:
                    return {"action": "MODIFY_SL", "new_sl": best_structural_shelf, "reason": "STRUCTURAL_TRAIL"}

        # ── 3b. Clear runner mode when RSI bounces above oversold ────────────
        if rsi_5m is not None and rsi_5m > 20.0 and pos.is_runner_mode:
            pos.is_runner_mode = False
            logger.info("📈 %s (SHORT): RSI %.1f bounced above 20.0 — Runner mode cleared.", symbol, rsi_5m)

        # ── 4. RSI <= 20 OVERSOLD & BUYER ABSORPTION EXHAUSTION ──────────────
        if rsi_5m is not None and rsi_5m <= 20.0:
            effective_obi = _banded_obi if _banded_obi is not None else _deep_obi
            if effective_obi is not None:
                # Phase 1 #6: Undefined zone guard — OBI between ±0.10 = ambiguous, don't act
                if -0.10 < effective_obi < 0.10 and not _bid_wall_near_price:
                    logger.debug("%s: RSI %.1f oversold but OBI=%.3f is undefined zone — HOLD", symbol, rsi_5m, effective_obi)
                    if structural_modified:
                        return {"action": "MODIFY_SL", "new_sl": pos.current_stop_loss, "reason": "STRUCTURAL_TRAIL"}
                    return {"action": "HOLD", "pnl_pct": round(pnl_pct, 2), "current_sl": pos.current_stop_loss, "peak_price": pos.peak_price}

                # deep_obi > +0.10: buyers stacking bids, absorbing sells -> short squeeze risk
                # bid_wall near price: institutional buyer wall blocking downside -> exhaustion
                is_exhausted = (effective_obi > 0.10) or _bid_wall_near_price
                is_runner = (effective_obi < -0.10) and not _bid_wall_near_price
                exit_reason = "RSI20_DEEP_ORDERFLOW_EXHAUSTION"
            else:
                is_exhausted = (cvd_delta > 0 or bid_ask_ratio > 1.33)
                is_runner = not is_exhausted
                exit_reason = "RSI20_ORDERFLOW_EXHAUSTION"

            if is_exhausted:
                logger.info(
                    "🎯 %s (SHORT): RSI %.1f Oversold + Buyer Absorption Exhaustion "
                    "(obi=%.3f, bid_wall=%s). Taking Profit.",
                    symbol, rsi_5m,
                    effective_obi if effective_obi is not None else float('nan'),
                    _bid_wall_near_price,
                )
                return await self._close_trade(pos, ltp, exit_reason)
            else:
                # Strong breakdown runner: ratchet trailing SL tightly down (0.35% above lowest peak)
                pos.is_runner_mode = True
                runner_sl = round(pos.peak_price * (1.0 + CAPITAL_CONFIG.TIGHT_RUNNER_TRAIL_PCT), 2)
                if runner_sl < pos.current_stop_loss:  # ratchet DOWN
                    if not await self._confirm_stop_update(pos, runner_sl, reason="RUNNER_TIGHT_TRAIL"):
                        pos.status = "PROTECTION_DEGRADED"
                        return {"action": "PROTECTION_DEGRADED", "reason": "RUNNER_UPDATE_FAILED"}
                    pos.current_stop_loss = runner_sl
                    logger.info("🚀 %s (SHORT): Runner Mode Active! Tight Trailing SL -> ₹%.2f", symbol, runner_sl)
                    if ltp >= runner_sl:
                        logger.info("🛑 %s (SHORT): Runner SL hit on same tick @ ₹%.2f (Runner SL: ₹%.2f)", symbol, ltp, runner_sl)
                        return await self._close_trade(pos, ltp, "RUNNER_STOP_HIT")
                    return {"action": "MODIFY_SL", "new_sl": runner_sl, "reason": "RUNNER_TIGHT_TRAIL"}
                elif structural_modified:
                    return {"action": "MODIFY_SL", "new_sl": pos.current_stop_loss, "reason": "STRUCTURAL_TRAIL"}

        # ── 5. TRAILING STOP LOSS HIT ─────────────────────────────────────────
        # For shorts, price bounces back UP to hit the current stop loss
        if pos.breakeven_locked and ltp >= pos.current_stop_loss:
            logger.info("🛑 %s (SHORT): Dynamic Trailing SL Hit @ ₹%.2f (Current SL: ₹%.2f)", symbol, ltp, pos.current_stop_loss)
            return await self._close_trade(pos, ltp, "TRAILED_STOP_HIT")

        if structural_modified:
            return {"action": "MODIFY_SL", "new_sl": pos.current_stop_loss, "reason": "STRUCTURAL_TRAIL"}

        return {
            "action": "HOLD",
            "pnl_pct": round(pnl_pct, 2),
            "current_sl": pos.current_stop_loss,
            "peak_price": pos.peak_price,
        }

    async def _close_trade(self, pos: WatchdogPositionShort, exit_price: float, reason: str) -> Dict[str, Any]:
        """
        Executes immediate market cover exit (BUY to cover short), frees capital slot,
        sends Telegram audit, and updates shorts database.
        For manual positions, sends high-priority Live Stream advisory without executing broker orders.
        """
        pos.status = "EXIT_PENDING"

        if pos.is_external:
            pos.status = "EXIT_ADVISED"
            from db_store import save_lifecycle_position
            save_lifecycle_position(pos.to_dict(), status=pos.status)
            pnl_final = round((pos.entry_price - exit_price) * pos.quantity, 2)
            pnl_pct_final = round(((pos.entry_price - exit_price) / max(0.01, pos.entry_price)) * 100.0, 2)
            try:
                from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts
                shortlist_shorts = get_ai_shortlist_service_shorts()
                shortlist_shorts.update_trade_price_and_status(
                    pos.symbol, exit_price, pnl_pct_final, "EXIT_ADVISED"
                )
                if reason == "HARD_1PCT_SL_HIT":
                    advice_msg = (
                        f"🚨 Hard stop reached — EXIT NOW! "
                        f"Price climbed to ₹{exit_price:.2f} (safety ceiling breached). "
                        f"Buy to cover on Dhan immediately to cut losses and protect your capital!"
                    )
                    ev_type = "HARD_SL_HIT"
                    sev = "danger"
                elif reason == "TRAILED_STOP_HIT":
                    advice_msg = (
                        f"🛑 Trailed stop hit — Time to cash out! "
                        f"Price bounced back to ₹{exit_price:.2f}. "
                        f"Buy to cover on Dhan now and bank your +{pnl_pct_final:.1f}% profit!"
                    )
                    ev_type = "TRAILED_STOP_HIT"
                    sev = "success" if pnl_final >= 0 else "danger"
                elif "EXHAUSTION" in reason:
                    advice_msg = (
                        f"💰 Take your profits now! "
                        f"Sellers are exhausted and buyers are stepping in at ₹{exit_price:.2f}. "
                        f"Buy to cover on Dhan right now to bank your +{pnl_pct_final:.1f}% gain before a bounce!"
                    )
                    ev_type = "TAKE_PROFIT"
                    sev = "success"
                elif reason == "RUNNER_STOP_HIT":
                    advice_msg = (
                        f"🛑 Runner stop reached — Time to cash out! "
                        f"Price bounced to ₹{exit_price:.2f}. "
                        f"Buy to cover on Dhan now and bank your gains!"
                    )
                    ev_type = "TRAILED_STOP_HIT"
                    sev = "success" if pnl_final >= 0 else "danger"
                else:
                    advice_msg = (
                        f"🚨 Exit recommended! Price reached ₹{exit_price:.2f}. "
                        f"Buy to cover on Dhan to protect your capital."
                    )
                    ev_type = "MANUAL_ADVISORY_EXIT"
                    sev = "warning"

                shortlist_shorts.add_timeline_event(
                    symbol=pos.symbol,
                    event_type=ev_type,
                    message=advice_msg,
                    price_at_event=exit_price,
                    severity=sev,
                )
            except Exception as exc:
                logger.debug("Failed adding short advisory exit event: %s", exc)

            try:
                await self.telegram.send_trade_closed_audit(
                    symbol=pos.symbol,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    qty=pos.quantity,
                    duration_str=f"{int(time.time() - pos.entry_time) // 60}m",
                    reason=f"SHORT MANUAL ADVISORY: {reason} (Broker order NOT placed - execute manually!)",
                    available_slots=max(0, CAPITAL_CONFIG.MAX_ACTIVE_TRADES - len(self.active_positions)),
                )
            except Exception:
                pass

            return {
                "action": "EXIT_ADVISED",
                "symbol": pos.symbol,
                "exit_price": exit_price,
                "pnl": pnl_final,
                "reason": reason,
                "is_external": True,
            }

        # For a short position, exit transaction type is BUY
        exit_resp = await self.mcp.emergency_market_exit(
            security_id=pos.security_id,
            quantity=pos.quantity,
            symbol_hint=pos.symbol,
            transaction_type="BUY",
        )

        exit_status = str(exit_resp.get("status", "")).upper()
        if exit_status not in ("SUCCESS", "CLOSED", "FILLED", "TRADED"):
            pos.status = "EXIT_FAILED"
            logger.critical(
                "🚨 CRITICAL: Short market cover exit FAILED for %s! Position retained in watchdog: %s",
                pos.symbol, exit_resp
            )
            return {
                "action": "EXIT_FAILED",
                "symbol": pos.symbol,
                "reason": reason,
                "error": exit_resp.get("reason", "Broker exit rejected"),
                "exit_resp": exit_resp,
            }

        from db_store import delete_lifecycle_position, save_lifecycle_order
        save_lifecycle_order(
            str(exit_resp.get("order_id", f"EXIT_{pos.order_id}")),
            pos.symbol,
            pos.security_id,
            "BUY",
            pos.quantity,
            exit_price,
            "FILLED",
            filled_price=float(exit_resp.get("fill_price", exit_price) or exit_price),
            payload=exit_resp,
            filled_quantity=pos.quantity,
        )
        pos.status = "CLOSED"
        delete_lifecycle_position(pos.symbol)
        duration_secs = int(time.time() - pos.entry_time)
        duration_str = f"{duration_secs // 60}m {duration_secs % 60}s"

        # 2. Release Capital Slot (₹60,000 freed from the shared pool)
        self.active_positions.pop(pos.symbol, None)
        next_candidate = self.slots.release_slot(
            pos.symbol, validator=self._validate_queued_candidate
        )

        # Sync 200-depth slots
        self._sync_depth_manager()

        # Update Shorts SQLite database trade status
        try:
            from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts
            shortlist_shorts = get_ai_shortlist_service_shorts()
            pnl_final = round((pos.entry_price - exit_price) * pos.quantity, 2)
            pnl_pct_final = round(((pos.entry_price - exit_price) / max(0.01, pos.entry_price)) * 100.0, 2)
            shortlist_shorts.update_trade_price_and_status(
                pos.symbol, exit_price, pnl_pct_final, "CLOSED"
            )
            if pnl_final >= 0:
                casual_msg = (
                    f"💰 Short successfully closed with profit! Automatically covered at ₹{exit_price:.2f} "
                    f"(+{pnl_pct_final:.1f}%). Profit locked into your portfolio!"
                )
            else:
                casual_msg = (
                    f"🚨 Safety stop executed! Automatically covered at ₹{exit_price:.2f} "
                    f"({pnl_pct_final:.1f}%) to protect your capital from bigger bounces."
                )
            shortlist_shorts.add_timeline_event(
                pos.symbol,
                "EXIT",
                casual_msg,
                price_at_event=exit_price,
                severity="success" if pnl_final >= 0 else "danger",
            )
        except Exception as db_exc:
            logger.debug("Failed to record short close in sqlite: %s", db_exc)

        # 3. Send Telegram Audit Report
        from services.live_algo_watchdog import get_algo_live_watchdog
        total_active = len(self.active_positions) + len(get_algo_live_watchdog().active_positions)
        available_slots = max(0, CAPITAL_CONFIG.MAX_ACTIVE_TRADES - total_active)

        await self.telegram.send_trade_closed_audit(
            symbol=pos.symbol,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            qty=pos.quantity,
            duration_str=duration_str,
            reason=f"SHORT: {reason}",
            available_slots=available_slots,
        )

        # 4. If a candidate was waiting in Priority Queue, trigger Telegram HITL approval
        if next_candidate:
            logger.info("Auto-dispatching Telegram approval for queued candidate: %s", next_candidate.get("symbol"))
            await self.telegram.send_trade_approval_alert(
                symbol=next_candidate["symbol"],
                security_id=next_candidate["security_id"],
                score=next_candidate["score"],
                bias=next_candidate["bias"],
                trigger_price=next_candidate["trigger_price"],
                qty=next_candidate["quantity"],
                rsi_5m=next_candidate.get("rsi_5m"),
                rvol=next_candidate.get("rvol"),
                active_slots=total_active,
                action=next_candidate.get("action", "BUY"),
            )

        realized_pnl = round((pos.entry_price - exit_price) * pos.quantity, 2)
        return {
            "action": "CLOSED",
            "symbol": pos.symbol,
            "exit_price": exit_price,
            "pnl": realized_pnl,
            "reason": reason,
            "exit_resp": exit_resp,
        }

    def reconcile_exit_fill(
        self,
        order_id: str,
        filled_quantity: int,
        fill_price: float = 0.0,
        symbol: str = "",
    ) -> bool:
        """Apply a broker-confirmed short-cover fill without submitting another order."""
        from db_store import delete_lifecycle_position, save_lifecycle_position
        for pos in list(self.active_positions.values()):
            if pos.order_id == order_id or (symbol and pos.symbol == symbol):
                remaining = max(0, pos.quantity - int(filled_quantity))
                if remaining:
                    pos.quantity = remaining
                    pos.status = "EXIT_PENDING"
                    save_lifecycle_position(pos.to_dict(), status=pos.status)
                else:
                    self.active_positions.pop(pos.symbol, None)
                    self.slots.release_slot(pos.symbol)
                    delete_lifecycle_position(pos.symbol)
                return True
        return False

    async def emergency_kill_all(self, price_overrides: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """Emergency Kill-Switch: Instantly covers all active short positions."""
        logger.warning("🚨 EMERGENCY KILL-SWITCH ACTIVATED across all WatchdogShorts positions!")
        results = {}
        for sym, pos in list(self.active_positions.items()):
            fallback_price = pos.current_stop_loss if pos.current_stop_loss > 0 else pos.entry_price
            exit_price = price_overrides.get(sym) if price_overrides and sym in price_overrides else fallback_price
            res = await self._close_trade(pos, exit_price, "MANUAL_EMERGENCY_KILL_SWITCH")
            results[sym] = res
        return {"status": "ALL_SHORT_POSITIONS_KILLED", "results": results}


_watchdog_shorts_singleton: Optional[AlgoLiveWatchdogShorts] = None

def get_algo_live_watchdog_shorts() -> AlgoLiveWatchdogShorts:
    global _watchdog_shorts_singleton
    if _watchdog_shorts_singleton is None:
        _watchdog_shorts_singleton = AlgoLiveWatchdogShorts()
    return _watchdog_shorts_singleton
