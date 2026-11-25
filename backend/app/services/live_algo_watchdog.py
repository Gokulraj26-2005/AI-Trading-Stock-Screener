"""
Institutional Live Algorithmic Watchdog
=======================================
Real-time microstructure and risk management engine for up to 3 concurrent active positions:
- Replaces static analysis SL/TP with dynamic market microstructure intelligence
- 1.0% Hard Stop Loss Floor (Zero-compromise loss ceiling: max ₹600 loss per ₹60k trade)
- Dynamic Breakeven Lock at +0.8% profit (+0.1% buffer for STT/brokerage fees)
- Structural Support Trailing at +1.5% profit (trails along 5m VWAP & 5m 20-EMA)
- Momentum Alpha & RSI 80+ Exhaustion vs Runner logic (Orderbook Depth + CVD Delta)
- Direct Dhan Official MCP / Gateway tool dispatch for atomic order execution
- Automated Telegram post-trade audit and Priority Queue promotion
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
class WatchdogPosition:
    symbol: str
    security_id: str
    entry_price: float
    quantity: int
    order_id: str
    action: str = "BUY"
    entry_time: float = field(default_factory=time.time)
    hard_stop_loss: float = 0.0
    current_stop_loss: float = 0.0
    target_1: float = 0.0
    target_2: float = 0.0
    peak_price: float = 0.0
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
        if self.hard_stop_loss <= 0.0:
            self.hard_stop_loss = round(self.entry_price * (1.0 - CAPITAL_CONFIG.MAX_HARD_STOP_LOSS_PCT), 2)
        if self.current_stop_loss <= 0.0:
            self.current_stop_loss = self.hard_stop_loss
        if self.peak_price <= 0.0:
            self.peak_price = self.entry_price
        if self.latest_ltp <= 0.0:
            self.latest_ltp = self.entry_price

    def to_dict(self, current_ltp: Optional[float] = None) -> Dict[str, Any]:
        """Serialize position state for Longs position. Uses current_ltp for P&L so
        the dashboard always shows true unrealized gain/loss."""
        display_price = current_ltp if (current_ltp and current_ltp > 0) else (self.latest_ltp if self.latest_ltp > 0 else self.peak_price)
        pnl = round((display_price - self.entry_price) * self.quantity, 2)
        pnl_pct = round(((display_price - self.entry_price) / max(0.01, self.entry_price)) * 100.0, 2)
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


class AlgoLiveWatchdog:
    def __init__(
        self,
        mcp_client: Optional[DhanMCPClient] = None,
        telegram_service: Optional[TelegramHITLService] = None,
        slot_manager: Optional[CapitalSlotManager] = None,
        dhan_client_id: Optional[str] = None,
        dhan_access_token: Optional[str] = None,
    ):
        import os

        self.mcp = mcp_client or get_dhan_mcp_client()
        self.telegram = telegram_service or get_telegram_hitl_service()
        self.slots = slot_manager or get_capital_slot_manager()
        self.active_positions: Dict[str, WatchdogPosition] = {}
        self._watchdog_task: Optional[asyncio.Task] = None

        # ── 200-Level Depth Manager (1 socket per active trade, max 3) ──────
        _client_id = dhan_client_id or os.getenv("DHAN_CLIENT_ID", "") or os.getenv("DHAN_USER_ID", "")
        _token = dhan_access_token or os.getenv("DHAN_ACCESS_TOKEN", "")
        if not _token:
            try:
                from services.dhan_token_service import get_dhan_token
                _token = get_dhan_token() or ""
            except Exception:
                pass

        if _client_id and _token:
            try:
                from services.dhan_adapter import Dhan200DepthManager
                self._depth_manager: Optional[Dhan200DepthManager] = Dhan200DepthManager(
                    client_id=_client_id,
                    access_token=_token,
                    max_connections=3,           # matches MAX_ACTIVE_TRADES
                    rotation_cooldown_sec=5.0,   # fast rotation since these are active trades
                    min_score_advantage=0.0,     # no score gate — all active trades are pre-approved
                )
                self._depth_manager.running = True
                logger.info("AlgoLiveWatchdog: 200-depth manager initialised (max 3 slots).")
            except Exception as exc:
                self._depth_manager = None
                logger.warning("AlgoLiveWatchdog: Failed to initialize Dhan200DepthManager: %s", exc)
        else:
            self._depth_manager = None
            logger.warning(
                "AlgoLiveWatchdog: DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN missing — "
                "200-depth feed DISABLED. Watchdog will fall back to 20-level OBI."
            )

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
        """Restore filled long positions after a process restart.

        Slots are restored by CapitalSlotManager; this method only rebuilds
        watchdog supervision and deliberately does not submit broker orders.
        """
        from db_store import get_lifecycle_positions
        restored = 0
        for data in get_lifecycle_positions():
            if str(data.get("action", "BUY")).upper() == "SELL" or data.get("symbol") in self.active_positions:
                continue
            pos = WatchdogPosition(
                symbol=data["symbol"],
                security_id=str(data.get("security_id", "")),
                entry_price=float(data["entry_price"]),
                quantity=int(data["quantity"]),
                order_id=str(data["order_id"]),
                action=str(data.get("action", "BUY")),
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
        """ARCH-03 Fix: Starts a background asyncio task that continuously feeds
        live prices into evaluate_tick() for all active positions once per second.
        Must be called from within a running event loop (e.g. at app startup)."""
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return  # already running
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info("AlgoLiveWatchdog: Background tick evaluation loop started.")

    async def _watchdog_loop(self) -> None:
        """Continuous 1-second evaluation loop for all active positions.
        Pulls the latest LTP from the CVD engine / candle aggregator and
        passes it to evaluate_tick() so SL/TP/trailing rules execute automatically.
        """
        from services.candle_aggregator import get_candle_aggregator
        aggregator = get_candle_aggregator()

        while True:
            try:
                for sym, pos in list(self.active_positions.items()):
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
                                quote = orch.dhan_ticker.latest_quotes.get(sym)
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
                                            orch.dhan_ticker.latest_quotes[sym] = {
                                                "ltp": ltp,
                                                "volume": q_item.get("volume", 0),
                                                "timestamp": last_update,
                                            }
                                    except Exception:
                                        pass
                            except Exception as rest_err:
                                logger.debug("Watchdog REST quote fallback failed for %s: %s", sym, rest_err)

                        # 5. Check feed freshness & auto-heal to ACTIVE
                        now = time.time()
                        if last_update > 0 and (now - last_update <= 30.0):
                            if pos.status == "PROTECTION_DEGRADED":
                                pos.status = "ACTIVE"
                                try:
                                    from db_store import update_lifecycle_position_status
                                    update_lifecycle_position_status(sym, "ACTIVE")
                                    logger.info("Watchdog feed recovered for %s; position status restored to ACTIVE.", sym)
                                except Exception:
                                    pass
                        elif last_update > 0 and (now - last_update > 30.0):
                            if pos.status != "PROTECTION_DEGRADED":
                                pos.status = "PROTECTION_DEGRADED"
                                try:
                                    from db_store import update_lifecycle_position_status
                                    update_lifecycle_position_status(sym, "PROTECTION_DEGRADED")
                                except Exception:
                                    pass
                                logger.warning(
                                    "Watchdog feed quiet (>30s) for %s; retaining position supervision in degraded state.",
                                    sym,
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
                            st = vwap_eng._states.get(sym)
                            if st and st.last_result and st.last_result.bands:
                                vwap_5m = st.last_result.bands.vwap

                            # 3. CVD Delta
                            from services.cvd_engine import get_cvd_engine
                            cvd_eng = get_cvd_engine()
                            cvd_delta = cvd_eng.get_cvd(token) or 0.0
                        except Exception as e:
                            logger.debug("Error computing watchdog indicators for %s: %s", sym, e)

                        await self.evaluate_tick(
                            symbol=sym,
                            ltp=ltp,
                            vwap_5m=vwap_5m,
                            ema20_5m=ema20_5m,
                            rsi_5m=rsi_5m,
                            cvd_delta=cvd_delta,
                            bid_ask_ratio=bid_ask_ratio,
                        )
                    except Exception as exc:
                        logger.debug("Watchdog loop tick error for %s: %s", sym, exc)
            except Exception as exc:
                logger.error("Watchdog loop iteration error: %s", exc)
            await asyncio.sleep(1.0)

    def register_position(
        self,
        symbol: str,
        security_id: str,
        entry_price: float,
        quantity: int,
        order_id: str,
        action: str = "BUY",
        stop_loss: Optional[float] = None,
        target_1: Optional[float] = None,
        target_2: Optional[float] = None,
        is_external: bool = False,
        is_paper: bool = False,
    ) -> WatchdogPosition:
        """
        Registers an executed trade into the Live Watchdog (Max 3 concurrent positions).
        """
        pos = WatchdogPosition(
            symbol=symbol,
            security_id=security_id,
            entry_price=entry_price,
            quantity=quantity,
            order_id=order_id,
            action=action.upper(),
            hard_stop_loss=float(stop_loss) if stop_loss and float(stop_loss) > 0 else 0.0,
            current_stop_loss=float(stop_loss) if stop_loss and float(stop_loss) > 0 else 0.0,
            target_1=float(target_1) if target_1 and float(target_1) > 0 else 0.0,
            target_2=float(target_2) if target_2 and float(target_2) > 0 else 0.0,
            is_external=is_external,
            is_paper=is_paper,
        )
        self.active_positions[symbol] = pos
        from db_store import save_lifecycle_position
        save_lifecycle_position(pos.to_dict(), status=pos.status)
        # Reserve capital slot
        self.slots.occupy_slot(
            symbol=symbol,
            security_id=security_id,
            entry_price=entry_price,
            quantity=quantity,
            order_id=order_id,
        )
        max_risk = abs(entry_price - pos.hard_stop_loss) * quantity
        logger.info(
            "🛡️ WATCHDOG ARMED: %s (%s) @ ₹%.2f (Qty: %d, SL: ₹%.2f, Max Risk: ₹%.2f, External: %s)",
            symbol, pos.action, entry_price, quantity, pos.hard_stop_loss,
            max_risk, is_external
        )

        # ── Activate 200-level depth feed for this position ──────────────────
        if self._depth_manager is not None:
            try:
                asyncio.create_task(
                    self._depth_manager.update_candidates(
                        self._build_depth_candidates()
                    )
                )
                logger.info(
                    "📡 200-depth feed ACTIVATED for active trade: %s (SecurityId: %s)",
                    symbol, security_id,
                )
            except Exception as exc:
                logger.warning("Failed to dispatch 200-depth activation for %s: %s", symbol, exc)

        self._sync_ticker_feed(symbol, security_id)

        # ── Phase 1 #4: Seed RSI and EMA with prior intraday history ─────────
        self._ensure_candle_history(symbol, security_id)

        # ── Auto-start the background tick evaluation loop on first position ──
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
            logger.info("Watchdog: seeded historical 5m/1m candles for %s (RSI & EMA ready on tick 1)", symbol)
        except Exception as exc:
            logger.debug("Watchdog: could not seed candle history for %s: %s", symbol, exc)

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

    def deregister_position(self, symbol: str) -> None:
        """Removes a position from active monitoring and releases its capital slot."""
        pos = self.active_positions.pop(symbol, None)
        self.slots.release_slot(symbol)
        from db_store import delete_lifecycle_position
        delete_lifecycle_position(symbol)
        if pos:
            logger.info("Watchdog position %s deregistered.", symbol)

    def _sync_ticker_feed(self, symbol: str, security_id: str) -> None:
        """Dynamically subscribes the instrument to the standard live Dhan ticker feed."""
        try:
            from services.orchestrator import get_orchestrator
            orch = get_orchestrator()
            if orch and orch.dhan_ticker and hasattr(orch.dhan_ticker, "subscribe_instrument"):
                asyncio.create_task(orch.dhan_ticker.subscribe_instrument(symbol, str(security_id)))
        except Exception as exc:
            logger.debug("Watchdog failed to sync ticker feed for %s: %s", symbol, exc)

    async def _confirm_stop_update(self, pos: WatchdogPosition, new_stop: float, reason: str = "") -> bool:
        if pos.is_external:
            pos.current_stop_loss = new_stop
            try:
                from services.ai_shortlist_service import get_ai_shortlist_service
                shortlist = get_ai_shortlist_service()
                pnl_pct = ((pos.peak_price - pos.entry_price) / max(0.01, pos.entry_price)) * 100.0

                if "BREAKEVEN" in reason:
                    msg = (
                        f"🛡️ Move your stop loss up to ₹{new_stop:.2f}! "
                        f"We're nicely in profit (+{pnl_pct:.1f}%), so lock it in right now on Dhan. "
                        f"This trade is now completely risk-free!"
                    )
                elif "RUNNER" in reason:
                    msg = (
                        f"🚀 Massive momentum rally! Move your stop loss up to ₹{new_stop:.2f} on Dhan "
                        f"to ride the move while protecting your gains!"
                    )
                else:
                    msg = (
                        f"📈 Trail your stop loss up to ₹{new_stop:.2f}! "
                        f"Price is climbing with strong support below. Move your stop up on Dhan to lock in higher profits!"
                    )

                shortlist.add_timeline_event(
                    symbol=pos.symbol,
                    event_type="MANUAL_ADVISORY_SL",
                    message=msg,
                    price_at_event=new_stop,
                    severity="warning",
                )
            except Exception as exc:
                logger.debug("Failed adding advisory SL timeline event: %s", exc)
            return True

        try:
            result = await self.mcp.modify_stop_loss(pos.order_id, new_stop, pos.quantity)
        except Exception as exc:
            logger.error("Stop update failed for %s: %s", pos.symbol, exc)
            return False
        if isinstance(result, bool):
            success = result
        elif not isinstance(result, dict):
            logger.warning("Stop update for %s returned an unstructured acknowledgement; treating it as accepted.", pos.symbol)
            success = True
        else:
            success = str(result.get("status", "")).upper() in {"SUCCESS", "MODIFIED", "UPDATED", "FILLED", "TRADED"}

        if success and not pos.is_external:
            try:
                from services.ai_shortlist_service import get_ai_shortlist_service
                shortlist = get_ai_shortlist_service()
                pnl_pct = ((pos.peak_price - pos.entry_price) / max(0.01, pos.entry_price)) * 100.0
                if "BREAKEVEN" in reason:
                    auto_msg = f"🛡️ Breakeven secured! Stop loss automatically moved up to ₹{new_stop:.2f} (+{pnl_pct:.1f}%). Trade is now risk-free."
                elif "RUNNER" in reason:
                    auto_msg = f"🚀 Momentum rally locked! Stop loss automatically trailed up to ₹{new_stop:.2f}."
                else:
                    auto_msg = f"📈 Stop loss automatically trailed up to ₹{new_stop:.2f} to protect profits."
                shortlist.add_timeline_event(
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
        pos,
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
        Phase 3: Routes all depth/flow events through the tiered WatchdogAlertEngine.
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

        # ── VWAP break against LONG position ───────────────────────────────
        price_breaks_vwap = (vwap_5m is not None and vwap_5m > 0 and ltp < vwap_5m)

        # ── RSI exhaustion zone ───────────────────────────────────────────
        rsi_exhaustion = (rsi_5m is not None and rsi_5m >= 80.0)

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
                (strongest_ask_wall or {}).get("price")
                if ask_wall_near else
                (strongest_bid_wall or {}).get("price")
            ),
            wall_side="ask" if ask_wall_near else ("bid" if bid_wall_near else None),
            wall_strength=(
                (strongest_ask_wall or {}).get("strength")
                if ask_wall_near else
                (strongest_bid_wall or {}).get("strength")
            ),
            rsi_value=rsi_5m,
            ltp=ltp,
            vwap_5m=vwap_5m,
        )

        def _add_event(symbol, event_type, message, price, severity):
            try:
                from services.ai_shortlist_service import get_ai_shortlist_service
                get_ai_shortlist_service().add_timeline_event(
                    symbol=symbol, event_type=event_type,
                    message=message, price_at_event=price, severity=severity,
                )
            except Exception:
                pass

        engine.evaluate(
            symbol=pos.symbol,
            direction="LONG",
            bundle=bundle,
            entry_price=pos.entry_price,
            add_timeline_event_fn=_add_event,
            record_alert_fn=record_alert,
        )

        # ── Phase 0: record snapshot every tick ─────────────────────────────
        try:
            record_snapshot(
                symbol=pos.symbol,
                direction="LONG",
                ltp=ltp,
                cvd=cvd_total,
                cvd_delta=cvd_delta_int,
                banded_obi=banded_obi,
                deep_obi=deep_obi,
                obi_smooth=smoothed_obi,
                top_walls=(depth_summary or {}).get("walls", [])[:5],
                rsi_5m=rsi_5m,
                vwap_5m=vwap_5m,
            )
        except Exception:
            pass

    def _build_depth_candidates(self) -> List[Dict[str, Any]]:
        """Build candidate list for Dhan200DepthManager from all active positions across Longs and Shorts."""
        candidates = [
            {
                "ExchangeSegment": "NSE_EQ",
                "SecurityId": pos.security_id,
                "symbol": pos.symbol,
                "score": 100.0,
            }
            for pos in self.active_positions.values()
            if pos.status in {"ACTIVE", "PROTECTION_DEGRADED"}
        ]
        try:
            from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
            shorts_watchdog = get_algo_live_watchdog_shorts()
            for pos in shorts_watchdog.active_positions.values():
                if pos.status in {"ACTIVE", "PROTECTION_DEGRADED"}:
                    candidates.append({
                        "ExchangeSegment": "NSE_EQ",
                        "SecurityId": pos.security_id,
                        "symbol": pos.symbol,
                        "score": 100.0,
                    })
        except Exception:
            pass
        return candidates

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
        Sub-second quant tick evaluation:
        1. Hard 1.0% stop floor check
        2. Breakeven lock (+0.8%)
        3. Structural support trailing (+1.5% with VWAP & 20-EMA)
        4. RSI 80+ exhaustion exit vs runner continuation
        5. Trailing stop hit check

        Phase 1 additions:
        - Stale tick rejection (Phase 1 #5)
        - Banded OBI ±0.5% from LTP used for depth decisions (Phase 2)
        - Undefined zone guard: RSI ≥80 with OBI between ±0.10 → HOLD, no action (Phase 1 #6)
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
                    logger.debug("Watchdog: stale tick rejected for %s (age %.1fs)", symbol, time.time() - bar_ts)
                    return {"action": "STALE_TICK"}
        except Exception:
            pass

        pos.latest_ltp = ltp
        pos.last_market_update_at = time.time()
        pos.peak_price = max(pos.peak_price, ltp)
        pnl_pct = ((ltp - pos.entry_price) / max(0.01, pos.entry_price)) * 100.0

        # ── Enrich with deep depth intelligence (200-level preferred, 20-level fallback) ─
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

            # Always read from CVD engine — it holds whatever depth tier has written last.
            raw_deep = cvd_eng.get_deep_obi(token_id)
            _deep_obi = float(raw_deep) if isinstance(raw_deep, (int, float)) and not isinstance(raw_deep, bool) else None

            raw_banded = cvd_eng.get_banded_obi(token_id, ltp)
            _banded_obi = float(raw_banded) if isinstance(raw_banded, (int, float)) and not isinstance(raw_banded, bool) else None

            raw_summary = cvd_eng.get_deep_depth_summary(token_id, ltp=ltp)
            depth_summary = raw_summary if isinstance(raw_summary, dict) else None

            raw_cvd = cvd_eng.get_cvd(token_id)
            _cvd_total = int(raw_cvd) if isinstance(raw_cvd, (int, float)) and not isinstance(raw_cvd, bool) else 0

            if _deep_obi is not None:
                if (
                    self._depth_manager is not None
                    and (
                        pos.security_id in getattr(self._depth_manager, "active_connections", {})
                        or str(pos.security_id) in getattr(self._depth_manager, "active_connections", {})
                    )
                ):
                    _depth_level_used = "200-level"
                else:
                    _depth_level_used = "20-level"

            # Wall detection: check if walls are within 0.5% of current price
            if depth_summary and depth_summary.get("walls"):
                bid_walls_near = []
                ask_walls_near = []
                for wall in depth_summary["walls"][:10]:
                    distance_pct = abs(wall["price"] - ltp) / ltp
                    if distance_pct <= 0.005:  # within 0.5% of LTP
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
            logger.debug("Depth enrichment skipped for %s: %s", symbol, exc)

        pos.depth_level_active = _depth_level_used

        # ── Microstructure & Depth Flow Event Anomaly Monitoring (Phase 3) ────
        self._fire_depth_flow_events(
            pos=pos,
            ltp=ltp,
            cvd_delta=cvd_delta,
            deep_obi=_deep_obi,
            banded_obi=_banded_obi,
            obi_smooth=None,  # computed inside
            bid_wall_near=_bid_wall_near_price,
            ask_wall_near=_ask_wall_near_price,
            depth_summary=depth_summary,
            rsi_5m=rsi_5m,
            vwap_5m=vwap_5m,
            cvd_total=_cvd_total,
            strongest_bid_wall=_strongest_bid_wall,
            strongest_ask_wall=_strongest_ask_wall,
        )

        # ── 1. HARD MAXIMUM 1.0% STOP LOSS FLOOR (Zero Negotiation) ───────────
        if ltp <= pos.hard_stop_loss:
            logger.warning(
                "🚨 HARD 1.0%% SL FLOOR HIT for %s (%s) @ ₹%.2f (Floor: ₹%.2f). Dispatching MCP Exit.",
                symbol, pos.action, ltp, pos.hard_stop_loss
            )
            return await self._close_trade(pos, ltp, "HARD_1PCT_SL_HIT")

        # ── 2. DYNAMIC BREAKEVEN LOCK (+0.8% Profit) ─────────────────────────
        if not pos.breakeven_locked and pnl_pct >= (CAPITAL_CONFIG.BREAKEVEN_TRIGGER_PCT * 100.0):
            be_sl = round(pos.entry_price * (1.0 + CAPITAL_CONFIG.FEE_BUFFER_PCT), 2)
            if not await self._confirm_stop_update(pos, be_sl, reason="BREAKEVEN_LOCKED"):
                pos.status = "PROTECTION_DEGRADED"
                return {"action": "PROTECTION_DEGRADED", "reason": "BREAKEVEN_UPDATE_FAILED"}
            pos.current_stop_loss = max(pos.current_stop_loss, be_sl)
            pos.breakeven_locked = True
            logger.info("🛡️ %s: Breakeven Locked (+%.2f%% Gain) -> New SL: ₹%.2f", symbol, pnl_pct, be_sl)
            return {"action": "MODIFY_SL", "new_sl": be_sl, "reason": "BREAKEVEN_LOCKED"}

        # ── 3. STRUCTURAL SUPPORT TRAILING (+1.5% Profit) ─────────────────────
        structural_modified = False
        if pos.breakeven_locked and pnl_pct >= (CAPITAL_CONFIG.STRUCTURAL_TRAIL_PCT * 100.0):
            structural_candidates = [pos.current_stop_loss]
            if vwap_5m and 0 < vwap_5m < ltp:
                structural_candidates.append(vwap_5m)
            if ema20_5m and 0 < ema20_5m < ltp:
                structural_candidates.append(ema20_5m)

            # If a bid wall exists below price within 0.5%, use it as a structural shelf
            if _bid_wall_near_price and _strongest_bid_wall and _strongest_bid_wall.get("price", 0) < ltp:
                structural_candidates.append(_strongest_bid_wall["price"])
                logger.info(
                    "🏗️ %s: Bid wall @ ₹%.2f (strength %.1fx) used as structural shelf.",
                    symbol, _strongest_bid_wall["price"], _strongest_bid_wall.get("strength", 1.0)
                )

            best_structural_shelf = round(max(structural_candidates), 2)
            if best_structural_shelf > pos.current_stop_loss:
                if not await self._confirm_stop_update(pos, best_structural_shelf, reason="STRUCTURAL_TRAIL"):
                    pos.status = "PROTECTION_DEGRADED"
                    return {"action": "PROTECTION_DEGRADED", "reason": "STRUCTURAL_UPDATE_FAILED"}
                pos.current_stop_loss = best_structural_shelf
                logger.info(
                    "📈 %s: Trailed SL to structural shelf (VWAP/20-EMA/Wall) -> ₹%.2f",
                    symbol, best_structural_shelf
                )
                structural_modified = True
                # If RSI is not in overbought territory, safe to return early.
                # If RSI >= 80, do NOT return early: fall through to Stage 4 to evaluate exhaustion / runner.
                if rsi_5m is None or rsi_5m < CAPITAL_CONFIG.RSI_OVERBOUGHT_THRESHOLD:
                    return {"action": "MODIFY_SL", "new_sl": best_structural_shelf, "reason": "STRUCTURAL_TRAIL"}

        # ── 3b. Clear runner mode when RSI drops below overbought ────────────
        if rsi_5m is not None and rsi_5m < CAPITAL_CONFIG.RSI_OVERBOUGHT_THRESHOLD and pos.is_runner_mode:
            pos.is_runner_mode = False
            logger.info("📉 %s: RSI %.1f dropped below %.1f — Runner mode cleared.", symbol, rsi_5m, CAPITAL_CONFIG.RSI_OVERBOUGHT_THRESHOLD)

        # ── 4. RSI 80+ OVERBOUGHT & ORDERFLOW EXHAUSTION ──────────────────────
        if rsi_5m is not None and rsi_5m >= CAPITAL_CONFIG.RSI_OVERBOUGHT_THRESHOLD:
            # Use banded OBI first (Phase 2), then deep_obi, else 5-level fallback
            effective_obi = _banded_obi if _banded_obi is not None else _deep_obi
            if effective_obi is not None:
                # Phase 1 #6: Undefined zone guard — OBI between ±0.10 = ambiguous, don't act
                if -0.10 < effective_obi < 0.10 and not _ask_wall_near_price:
                    # OBI is ambiguous; hold and wait for clarity
                    logger.debug("%s: RSI %.1f overbought but OBI=%.3f is undefined zone — HOLD", symbol, rsi_5m, effective_obi)
                    if structural_modified:
                        return {"action": "MODIFY_SL", "new_sl": pos.current_stop_loss, "reason": "STRUCTURAL_TRAIL"}
                    return {"action": "HOLD", "pnl_pct": round(pnl_pct, 2), "current_sl": pos.current_stop_loss, "peak_price": pos.peak_price}
                # deep_obi < -0.10: sellers stacking the book -> exhaustion
                # ask_wall near price: institutional seller wall blocking upside -> exhaustion
                is_exhausted = (effective_obi < -0.10) or _ask_wall_near_price
                is_runner = (effective_obi > 0.10) and not _ask_wall_near_price
                exit_reason = "RSI80_DEEP_ORDERFLOW_EXHAUSTION"
            else:
                is_exhausted = (cvd_delta < 0 or bid_ask_ratio < CAPITAL_CONFIG.BID_ASK_EXHAUSTION_RATIO)
                is_runner = not is_exhausted
                exit_reason = "RSI80_ORDERFLOW_EXHAUSTION"

            if is_exhausted:
                logger.info(
                    "🎯 %s: RSI %.1f Overbought + Deep Orderflow Exhaustion "
                    "(obi=%.3f, ask_wall=%s). Taking Profit.",
                    symbol, rsi_5m,
                    effective_obi if effective_obi is not None else float('nan'),
                    _ask_wall_near_price,
                )
                return await self._close_trade(pos, ltp, exit_reason)
            else:
                # Strong institutional runner: tighten trailing gap to 0.35% below price
                pos.is_runner_mode = True
                runner_sl = round(pos.peak_price * (1.0 - CAPITAL_CONFIG.TIGHT_RUNNER_TRAIL_PCT), 2)
                if runner_sl > pos.current_stop_loss:
                    if not await self._confirm_stop_update(pos, runner_sl, reason="RUNNER_TIGHT_TRAIL"):
                        pos.status = "PROTECTION_DEGRADED"
                        return {"action": "PROTECTION_DEGRADED", "reason": "RUNNER_UPDATE_FAILED"}
                    pos.current_stop_loss = runner_sl
                    logger.info("🚀 %s: Runner Mode Active! Tight Trailing SL -> ₹%.2f", symbol, runner_sl)
                    # Same-tick SL check: if price is already at or below the newly ratcheted runner SL
                    if ltp <= runner_sl:
                        logger.info("🛑 %s: Runner SL hit on same tick @ ₹%.2f (Runner SL: ₹%.2f)", symbol, ltp, runner_sl)
                        return await self._close_trade(pos, ltp, "RUNNER_STOP_HIT")
                    return {"action": "MODIFY_SL", "new_sl": runner_sl, "reason": "RUNNER_TIGHT_TRAIL"}
                elif structural_modified:
                    return {"action": "MODIFY_SL", "new_sl": pos.current_stop_loss, "reason": "STRUCTURAL_TRAIL"}

        # ── 5. TRAILING STOP LOSS HIT ─────────────────────────────────────────
        if pos.breakeven_locked and ltp <= pos.current_stop_loss:
            logger.info("🛑 %s: Dynamic Trailing SL Hit @ ₹%.2f (Current SL: ₹%.2f)", symbol, ltp, pos.current_stop_loss)
            return await self._close_trade(pos, ltp, "TRAILED_STOP_HIT")

        if structural_modified:
            return {"action": "MODIFY_SL", "new_sl": pos.current_stop_loss, "reason": "STRUCTURAL_TRAIL"}

        return {
            "action": "HOLD",
            "pnl_pct": round(pnl_pct, 2),
            "current_sl": pos.current_stop_loss,
            "peak_price": pos.peak_price,
        }

    async def _close_trade(self, pos: WatchdogPosition, exit_price: float, reason: str) -> Dict[str, Any]:
        """
        Executes immediate market exit, frees capital slot, sends Telegram audit,
        and auto-promotes the next candidate from the Priority Queue.
        For manual positions, sends high-priority Live Stream advisory without executing broker orders.
        """
        pos.status = "EXIT_PENDING"

        if pos.is_external:
            pos.status = "EXIT_ADVISED"
            from db_store import save_lifecycle_position
            save_lifecycle_position(pos.to_dict(), status=pos.status)
            pnl_final = round((exit_price - pos.entry_price) * pos.quantity, 2)
            pnl_pct_final = round(
                ((exit_price - pos.entry_price) / max(0.01, pos.entry_price)) * 100.0,
                2,
            )
            try:
                from services.ai_shortlist_service import get_ai_shortlist_service
                shortlist = get_ai_shortlist_service()
                shortlist.update_trade_price_and_status(
                    pos.symbol, exit_price, pnl_pct_final, "EXIT_ADVISED"
                )
                if reason == "HARD_1PCT_SL_HIT":
                    advice_msg = (
                        f"🚨 Hard stop reached — EXIT NOW! "
                        f"Price dropped to ₹{exit_price:.2f} (safety floor hit). "
                        f"Close your trade on Dhan immediately to cut losses and protect your capital!"
                    )
                    ev_type = "HARD_SL_HIT"
                    sev = "danger"
                elif reason == "TRAILED_STOP_HIT":
                    advice_msg = (
                        f"🛑 Trailed stop hit — Time to cash out! "
                        f"Price pulled back to ₹{exit_price:.2f}. "
                        f"Close your trade on Dhan now and bank your +{pnl_pct_final:.1f}% profit!"
                    )
                    ev_type = "TRAILED_STOP_HIT"
                    sev = "success" if pnl_final >= 0 else "danger"
                elif "EXHAUSTION" in reason:
                    advice_msg = (
                        f"💰 Take your profits now! "
                        f"Buyers are running out of steam and big sellers are stacking at ₹{exit_price:.2f}. "
                        f"Sell on Dhan right now to lock in your +{pnl_pct_final:.1f}% gain before a pullback!"
                    )
                    ev_type = "TAKE_PROFIT"
                    sev = "success"
                elif reason == "RUNNER_STOP_HIT":
                    advice_msg = (
                        f"🛑 Runner stop reached — Time to cash out! "
                        f"Price retraced to ₹{exit_price:.2f}. "
                        f"Sell on Dhan now to take home your profits!"
                    )
                    ev_type = "TRAILED_STOP_HIT"
                    sev = "success" if pnl_final >= 0 else "danger"
                else:
                    advice_msg = (
                        f"🚨 Exit recommended! Price reached ₹{exit_price:.2f}. "
                        f"Close your trade on Dhan to protect your capital."
                    )
                    ev_type = "MANUAL_ADVISORY_EXIT"
                    sev = "warning"

                shortlist.add_timeline_event(
                    pos.symbol,
                    ev_type,
                    advice_msg,
                    price_at_event=exit_price,
                    severity=sev,
                )
            except Exception as exc:
                logger.debug("Failed adding advisory exit event: %s", exc)

            try:
                await self.telegram.send_trade_closed_audit(
                    symbol=pos.symbol,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    qty=pos.quantity,
                    duration_str=f"{int(time.time() - pos.entry_time) // 60}m",
                    reason=f"LONG MANUAL ADVISORY: {reason} (Broker order NOT placed - execute manually!)",
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

        # 1. Dispatch emergency market exit via Dhan MCP (sell to close long)
        exit_resp = await self.mcp.emergency_market_exit(
            security_id=pos.security_id,
            quantity=pos.quantity,
            symbol_hint=pos.symbol,
            transaction_type="SELL",
        )

        exit_status = str(exit_resp.get("status", "")).upper()
        exit_lifecycle = str(
            exit_resp.get("lifecycle_status", exit_resp.get("order_status", ""))
        ).upper()
        if not exit_lifecycle and exit_status in {"CLOSED", "FILLED", "TRADED", "SUCCESS"}:
            # Legacy adapters returned SUCCESS only after a terminal fill.
            exit_lifecycle = "FILLED"
        if exit_status not in {"SUCCESS", "CLOSED", "FILLED", "TRADED"} or (
            exit_lifecycle not in {"FILLED", "CLOSED", "TRADED", "CANCELLED"}
            and exit_status not in {"CLOSED", "FILLED", "TRADED"}
        ):
            from db_store import save_lifecycle_order, save_lifecycle_position
            exit_order_id = str(exit_resp.get("order_id", f"EXIT_{pos.order_id}"))
            save_lifecycle_order(
                exit_order_id, pos.symbol, pos.security_id, "SELL", pos.quantity,
                exit_price, exit_lifecycle or "UNKNOWN", payload=exit_resp,
                filled_quantity=int(exit_resp.get("filled_quantity", 0) or 0),
            )
            pos.status = "EXIT_PENDING" if exit_lifecycle in {"ACCEPTED", "TRANSIT", "PARTIAL"} else "EXIT_FAILED"
            save_lifecycle_position(pos.to_dict(), status=pos.status)
            if pos.status == "EXIT_PENDING":
                return {
                    "action": "EXIT_PENDING",
                    "symbol": pos.symbol,
                    "reason": reason,
                    "order_id": exit_order_id,
                    "exit_resp": exit_resp,
                }
            logger.critical(
                "🚨 CRITICAL: Emergency market exit FAILED for %s (SELL)! Position retained in watchdog: %s",
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
            pos.symbol, pos.security_id, "SELL", pos.quantity, exit_price,
            "FILLED", filled_price=float(exit_resp.get("fill_price", exit_price) or exit_price),
            payload=exit_resp, filled_quantity=pos.quantity,
        )
        pos.status = "CLOSED"
        delete_lifecycle_position(pos.symbol)
        duration_secs = int(time.time() - pos.entry_time)
        duration_str = f"{duration_secs // 60}m {duration_secs % 60}s"

        # 2. Release Capital Slot (₹60,000 freed)
        self.active_positions.pop(pos.symbol, None)
        next_candidate = self.slots.release_slot(
            pos.symbol, validator=self._validate_queued_candidate
        )

        try:
            from services.ai_shortlist_service import get_ai_shortlist_service
            shortlist = get_ai_shortlist_service()
            pnl_final = round((exit_price - pos.entry_price) * pos.quantity, 2)
            pnl_pct_final = round(
                ((exit_price - pos.entry_price) / max(0.01, pos.entry_price)) * 100.0,
                2,
            )
            shortlist.update_trade_price_and_status(
                pos.symbol, exit_price, pnl_pct_final, "CLOSED"
            )
            if pnl_final >= 0:
                casual_msg = (
                    f"💰 Trade successfully closed with profit! Automatically exited at ₹{exit_price:.2f} "
                    f"(+{pnl_pct_final:.1f}%). Profit locked into your portfolio!"
                )
            else:
                casual_msg = (
                    f"🚨 Safety stop executed! Automatically closed at ₹{exit_price:.2f} "
                    f"({pnl_pct_final:.1f}%) to protect your capital from bigger drops."
                )
            shortlist.add_timeline_event(
                pos.symbol,
                "EXIT",
                casual_msg,
                price_at_event=exit_price,
                severity="success" if pnl_final >= 0 else "danger",
            )
        except Exception as db_exc:
            logger.warning("Failed to record long close for %s: %s", pos.symbol, db_exc)

        # ── Release 200-depth slot for this symbol ────────────────────────────
        if self._depth_manager is not None:
            try:
                sec_id = str(pos.security_id)
                ws = getattr(self._depth_manager, "active_connections", {}).pop(sec_id, None)
                if ws:
                    ws.running = False
                    try:
                        close_coro = ws.close()
                        if asyncio.iscoroutine(close_coro):
                            asyncio.create_task(close_coro)
                    except Exception:
                        pass
                task = getattr(self._depth_manager, "connection_tasks", {}).pop(sec_id, None)
                if task and not task.done():
                    task.cancel()
                getattr(self._depth_manager, "active_metadata", {}).pop(sec_id, None)

                remaining = self._build_depth_candidates()  # already excludes this closed position
                asyncio.create_task(self._depth_manager.update_candidates(remaining))
                logger.info("📡 200-depth slot released for: %s (%d active remaining)", pos.symbol, len(remaining))
            except Exception as exc:
                logger.warning("Failed to release 200-depth slot for %s: %s", pos.symbol, exc)

        # 3. Send Telegram Audit Report
        try:
            from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
            shorts_active = len(get_algo_live_watchdog_shorts().active_positions)
        except Exception:
            shorts_active = 0
        total_active = len(self.active_positions) + shorts_active
        available_slots = max(0, CAPITAL_CONFIG.MAX_ACTIVE_TRADES - total_active)

        await self.telegram.send_trade_closed_audit(
            symbol=pos.symbol,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            qty=pos.quantity,
            duration_str=duration_str,
            reason=f"LONG: {reason}",
            available_slots=available_slots,
        )

        # 4. If a candidate was waiting in the Priority Queue, trigger Telegram HITL approval
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

        realized_pnl = round((exit_price - pos.entry_price) * pos.quantity, 2)
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
        """Apply a broker-confirmed exit fill without submitting another order."""
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
        """Emergency Kill-Switch: Instantly exits all active positions and cancels pending orders."""
        logger.warning("🚨 EMERGENCY KILL-SWITCH ACTIVATED across all Watchdog positions!")
        results = {}
        for sym, pos in list(self.active_positions.items()):
            # Use price override if available, or current stop loss as conservative realistic price.
            # Never use pos.peak_price, which dangerously overstates realized PnL in audits.
            fallback_price = pos.current_stop_loss if pos.current_stop_loss > 0 else pos.entry_price
            exit_price = price_overrides.get(sym) if price_overrides and sym in price_overrides else fallback_price
            res = await self._close_trade(pos, exit_price, "MANUAL_EMERGENCY_KILL_SWITCH")
            results[sym] = res
        return {"status": "ALL_POSITIONS_KILLED", "results": results}


_watchdog_singleton: Optional[AlgoLiveWatchdog] = None

def get_algo_live_watchdog(
    dhan_client_id: Optional[str] = None,
    dhan_access_token: Optional[str] = None,
) -> AlgoLiveWatchdog:
    global _watchdog_singleton
    if _watchdog_singleton is None:
        _watchdog_singleton = AlgoLiveWatchdog(
            dhan_client_id=dhan_client_id,
            dhan_access_token=dhan_access_token,
        )
    return _watchdog_singleton
