"""
Institutional Trade Promotion & Invalidation Gatekeeper
========================================================
Enforces strict promotion pipeline rules:
1. ONLY TWO ways a stock enters Live Tracking:
   - Automated Promotion: Score >= 85, Confirmed Trigger (sustained 40+ ticks), 5m RSI < 73, RVOL >= 1.0.
   - Manual Addition: User explicitly clicks "+ Track Setup" in Chart Analysis.
2. Auto-Dispatches Telegram HITL approval alert with fixed ₹60k sizing.
3. Auto-Purge Invalidation: If an unapproved stock in Live Tracking falls > 0.20% below trigger,
   it is immediately purged to prevent entering degraded setups.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from config.trading_config import CAPITAL_CONFIG
from logger import logger
from services.capital_slot_manager import get_capital_slot_manager
from services.dhan_instrument_resolver import get_instrument_resolver, resolve_canonical_symbol
from services.telegram_hitl_service import get_telegram_hitl_service
from services.ai_shortlist_service import get_ai_shortlist_service


class PromotionManager:
    def __init__(self):
        self.slot_manager = get_capital_slot_manager()
        self.telegram = get_telegram_hitl_service()
        self.resolver = get_instrument_resolver()
        self.shortlist_service = get_ai_shortlist_service()
        # Track promoted symbols to avoid duplicate spam alerts
        self._promoted_cache: Dict[str, float] = {}

    def is_eligible_for_promotion(
        self,
        symbol: str,
        score: int,
        live_status: str,
        trigger_price: float,
        current_price: float,
        rsi_5m: Optional[float] = None,
        rvol: Optional[float] = None,
        bias: str = "BULLISH_BREAKOUT",
        quote_source: Optional[str] = None,
        quote_age_ms: Optional[int] = None,
        rsi_is_real: bool = False,
    ) -> bool:
        """
        Validates all institutional criteria:
        1. Score >= 85
        2. Status is CONFIRMED_TRIGGER or SUSTAINED_BREAKOUT (proved 40+ ticks duration)
        3. RSI 5m < 73 (clean momentum runway for longs)
        4. RVOL >= 1.0 (volume expansion)
        5. Current price is at or beyond trigger in setup direction
        """
        if bias.upper().startswith("BEAR"):
            logger.debug("Longs promotion rejected %s: bias %s is bearish", symbol, bias)
            return False

        if score < CAPITAL_CONFIG.MIN_POTENTIAL_SCORE:
            return False

        if live_status not in ("CONFIRMED_TRIGGER", "SUSTAINED_BREAKOUT"):
            return False

        # RSI Gate: A stock CANNOT be promoted if RSI is None (unknown/unverified data).
        # Must be strictly < MAX_ENTRY_RSI_5M (73.0) to prevent buying in overbought exhaustion.
        if rsi_5m is None:
            logger.warning(
                "RSI gate blocked %s: 5m RSI is unavailable (None).",
                symbol,
            )
            return False

        if rsi_5m >= CAPITAL_CONFIG.MAX_ENTRY_RSI_5M:
            logger.warning(
                "RSI gate blocked %s: RSI=%.1f exceeds max threshold %.1f (overbought)",
                symbol, rsi_5m, CAPITAL_CONFIG.MAX_ENTRY_RSI_5M
            )
            return False

        # RVOL Gate: RVOL must be verified and >= MIN_ENTRY_RVOL (1.0).
        if rvol is None or rvol < CAPITAL_CONFIG.MIN_ENTRY_RVOL:
            logger.warning(
                "RVOL gate blocked %s: RVOL=%s below minimum threshold %.1f",
                symbol, rvol, CAPITAL_CONFIG.MIN_ENTRY_RVOL
            )
            return False

        # Price Gate: Must be at or beyond trigger at the exact moment of promotion
        if current_price < trigger_price:
            logger.warning("Price gate blocked %s: LTP %.2f is below trigger %.2f at promotion time", symbol, current_price, trigger_price)
            return False

        # Time-of-Day Cutoff Gate: Prior to 15:10 MIS square-off
        from datetime import datetime, timezone, timedelta
        try:
            from zoneinfo import ZoneInfo
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        except Exception:
            now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        cutoff_parts = getattr(CAPITAL_CONFIG, "PROMOTION_CUTOFF_TIME_IST", "15:00").split(":")
        cutoff_hour = int(cutoff_parts[0])
        cutoff_min = int(cutoff_parts[1]) if len(cutoff_parts) > 1 else 0
        if (now_ist.hour > cutoff_hour) or (now_ist.hour == cutoff_hour and now_ist.minute >= cutoff_min):
            logger.warning("Time cutoff blocked %s: current time %02d:%02d >= cutoff %02d:%02d IST", symbol, now_ist.hour, now_ist.minute, cutoff_hour, cutoff_min)
            return False

        return True

    async def evaluate_and_promote(
        self,
        symbol: str,
        score: int,
        bias: str,
        live_status: str,
        trigger_price: float,
        current_price: float,
        rsi_5m: Optional[float] = None,
        rvol: Optional[float] = None,
        thesis: str = "",
        quote_source: Optional[str] = None,
        quote_age_ms: Optional[int] = None,
        rsi_is_real: bool = False,
        trigger_correlation_id: Optional[str] = None,
        stop_loss: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Evaluates a stock from Live Analysis. If all criteria pass, promotes to Live Tracking desk
        and fires interactive Telegram HITL alert.
        """
        canonical = resolve_canonical_symbol(symbol.upper().strip())

        if trigger_correlation_id:
            from services.live_trigger_state import get_live_trigger_state_store
            trigger_state = get_live_trigger_state_store().get_state(canonical)
            if not trigger_state or trigger_state.get("correlation_id") != trigger_correlation_id:
                return {"promoted": False, "reason": "UNVERIFIED_TRIGGER_STATE"}
            live_status = str(trigger_state.get("status") or live_status)

        # Preserve the execution-switch response even when the caller is using
        # a preview/screener payload without live-data metadata.
        if not CAPITAL_CONFIG.execution_enabled:
            logger.warning(
                "Master execution switch is OFF: promotion blocked for %s before live-data checks.",
                canonical,
            )
            return {
                "promoted": False,
                "execution_blocked": True,
                "symbol": canonical,
                "reason": "Master execution switch is OFF. Telegram alert and MCP order placement blocked.",
            }

        if quote_source not in {"aggregator", "rest", "rest_cache"}:
            logger.warning(
                "Quote-quality gate blocked %s: source=%s is not executable market data.",
                canonical,
                quote_source,
            )
            return {"promoted": False, "reason": "UNVERIFIED_QUOTE_SOURCE"}

        max_quote_age_ms = int(getattr(CAPITAL_CONFIG, "MAX_AGGREGATOR_QUOTE_AGE_MS", 15_000)) if quote_source == "aggregator" else int(getattr(CAPITAL_CONFIG, "MAX_REST_QUOTE_AGE_MS", 15_000))
        if quote_age_ms is None or quote_age_ms < 0 or quote_age_ms > max_quote_age_ms:
            logger.warning(
                "Quote-quality gate blocked %s: source=%s age_ms=%s (max=%d).",
                canonical,
                quote_source,
                quote_age_ms,
                max_quote_age_ms,
            )
            return {"promoted": False, "reason": "STALE_QUOTE"}

        if not rsi_is_real:
            logger.warning("RSI quality gate blocked %s: RSI is proxy-derived.", canonical)
            return {"promoted": False, "reason": "UNVERIFIED_RSI"}

        if not self.is_eligible_for_promotion(
            symbol=canonical,
            score=score,
            live_status=live_status,
            trigger_price=trigger_price,
            current_price=current_price,
            rsi_5m=rsi_5m,
            rvol=rvol,
            bias=bias,
            quote_source=quote_source,
            quote_age_ms=quote_age_ms,
            rsi_is_real=rsi_is_real,
        ):
            return {"promoted": False, "reason": "CRITERIA_NOT_MET"}

        # Cooldown check: don't alert the same symbol within 15 minutes
        now = time.time()
        if canonical in self._promoted_cache and (now - self._promoted_cache[canonical]) < 900:
            return {"promoted": False, "reason": "COOLDOWN_ACTIVE"}

        # Check if already tracked on the desk
        existing_trades = self.shortlist_service.get_all_trades()
        existing_syms = {t.get("symbol") for t in existing_trades}
        if canonical in existing_syms:
            return {"promoted": False, "reason": "ALREADY_TRACKED"}

        self.resolver.load()
        sec_id = str(self.resolver.get_security_id(canonical) or "")

        action = "BUY"
        default_sl = round(trigger_price * (1.0 - CAPITAL_CONFIG.MAX_HARD_STOP_LOSS_PCT), 2)
        if stop_loss is not None and stop_loss > 0 and stop_loss < trigger_price:
            stop_dist_pct = (trigger_price - stop_loss) / trigger_price
            if stop_dist_pct <= CAPITAL_CONFIG.MAX_HARD_STOP_LOSS_PCT:
                sl = round(stop_loss, 2)
            else:
                logger.warning("Structural stop loss %.2f for %s exceeds max hard stop %.2f%%; clamping to floor", stop_loss, canonical, CAPITAL_CONFIG.MAX_HARD_STOP_LOSS_PCT * 100)
                sl = default_sl
        else:
            sl = default_sl

        t1 = round(trigger_price * 1.03, 2)
        t2 = round(trigger_price * 1.06, 2)

        # Size from maximum stop-distance loss, then cap by MIS margin.
        from services.dhan_mcp_client import get_dhan_mcp_client
        mcp = get_dhan_mcp_client()
        leverage = await mcp.get_security_leverage(sec_id, trigger_price) if sec_id else float(getattr(CAPITAL_CONFIG, "MIS_LEVERAGE", 5.0))
        qty = self.slot_manager.calculate_execution_quantity(trigger_price, sl, leverage=leverage)
        if qty <= 0:
            return {"promoted": False, "reason": "INVALID_QUANTITY"}

        # 1. Insert into Live Tracking Desk Database with status PENDING_APPROVAL
        trade_payload = {
            "symbol": canonical,
            "action": action,
            "setup_type": bias,
            "entry_price": trigger_price,
            "current_price": current_price,
            "stop_loss": sl,
            "target_1": t1,
            "target_2": t2,
            "risk_reward_ratio": round(abs(t1 - trigger_price) / max(0.01, abs(trigger_price - sl)), 2),
            "thesis": thesis or f"Institutional acceptance breakout. Score: {score}, RSI: {rsi_5m:.1f}, RVOL: {rvol:.2f}x",
            "catalyst_summary": f"Sustained 40+ ticks beyond trigger. Sizing: {qty} shares (₹60k MIS @ {leverage:.1f}x).",
            "tape_evidence": f"RVOL {rvol:.2f}x | 5m RSI {rsi_5m:.1f}",
            "rsi_5m": float(rsi_5m),
            "rvol": float(rvol),
            "status": "PENDING_APPROVAL",
            "quality_score": score,
            "security_id": sec_id,
            "quantity": qty,
            "leverage": leverage,
        }

        # Master Execution Killswitch Guard (Blocks BEFORE reaching Telegram & MCP placement)
        if not CAPITAL_CONFIG.execution_enabled:
            logger.warning(
                "🛡️ MASTER EXECUTION SWITCH IS OFF: %s passed breakout criteria, but Telegram alert and MCP placement are BLOCKED.",
                canonical
            )
            trade_payload["status"] = "EXECUTION_PAUSED"
            trade_payload["catalyst_summary"] = f"Breakout confirmed but Execution Switch is OFF. Sizing: {qty} shares."
            self.shortlist_service.upsert_trade(trade_payload)
            self._promoted_cache[canonical] = now
            return {
                "promoted": False,
                "execution_blocked": True,
                "symbol": canonical,
                "reason": "Master execution switch is OFF. Telegram alert and MCP order placement blocked.",
            }

        self.shortlist_service.upsert_trade(trade_payload)
        self._promoted_cache[canonical] = now

        logger.info(
            "🚀 PROMOTED TO LIVE TRACKING: %s %s (Score: %d, Qty: %d @ ₹%.2f, Capital: ₹%.2f)",
            action, canonical, score, qty, trigger_price, CAPITAL_CONFIG.PER_TRADE_ALLOCATION
        )

        # 2. Dispatch Interactive Telegram Approval Alert
        active_count = len(self.slot_manager.active_slots)
        await self.telegram.send_trade_approval_alert(
            symbol=canonical,
            security_id=sec_id,
            score=score,
            bias=bias,
            trigger_price=trigger_price,
            qty=qty,
            rsi_5m=rsi_5m,
            rvol=rvol,
            active_slots=active_count,
            action=action,
        )

        return {"promoted": True, "symbol": canonical, "quantity": qty, "trigger_price": trigger_price, "action": action}

    def evaluate_auto_purge(self, symbol: str, current_price: float, trigger_price: float, action: str = "BUY") -> bool:
        """
        Auto-Purge Invalidation Rule for Longs:
        If a stock sitting on the Live Tracking desk falls > 0.20% adverse before fill,
        evict it immediately to avoid trading failed setups.
        """
        purge_floor = trigger_price * (1.0 - CAPITAL_CONFIG.PURGE_SLIP_TOLERANCE_PCT)
        if current_price < purge_floor:
            logger.warning(
                "❌ AUTO-PURGE (LONG): %s slipped below trigger (LTP: ₹%.2f < Floor: ₹%.2f). Evicting from desk.",
                symbol, current_price, purge_floor
            )
            self.shortlist_service.delete_trade(symbol)
            return True
        return False


_promotion_manager_singleton: Optional[PromotionManager] = None

def get_promotion_manager() -> PromotionManager:
    global _promotion_manager_singleton
    if _promotion_manager_singleton is None:
        _promotion_manager_singleton = PromotionManager()
    return _promotion_manager_singleton
