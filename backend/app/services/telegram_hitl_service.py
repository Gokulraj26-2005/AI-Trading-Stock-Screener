"""
Telegram Human-In-The-Loop (HITL) Service
=========================================
Provides interactive trade approval workflow via Telegram Bot:
- Rich formatted Markdown approval alerts with inline buttons: [ ✅ APPROVE (₹60k) ] / [ ❌ DECLINE ]
- Long-polling daemon & webhook receiver to handle inline callback clicks with zero configuration
- Trade Closed audit reports with net P&L, duration, and exit reasons
- Queue notifications when slots are occupied
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable, Dict, Optional
import httpx

from config.trading_config import CAPITAL_CONFIG
from logger import logger


class TelegramHITLService:
    def __init__(self):
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", CAPITAL_CONFIG.TELEGRAM_BOT_TOKEN)
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", CAPITAL_CONFIG.TELEGRAM_CHAT_ID)
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"
        self._polling_task: Optional[asyncio.Task] = None
        self._on_approval_callback: Optional[Callable] = None
        self._on_decline_callback: Optional[Callable] = None
        self._last_update_id = 0

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def register_callbacks(
        self,
        on_approval: Callable,
        on_decline: Callable,
    ):
        """Register callbacks invoked when user clicks inline buttons on Telegram."""
        self._on_approval_callback = on_approval
        self._on_decline_callback = on_decline

    async def send_trade_approval_alert(
        self,
        symbol: str,
        security_id: str,
        score: int,
        bias: str,
        trigger_price: float,
        qty: int,
        rsi_5m: Optional[float] = None,
        rvol: Optional[float] = None,
        active_slots: int = 0,
        action: str = "BUY",
        trade_intent_id: Optional[str] = None,
    ) -> bool:
        """
        Sends interactive trade proposal to Telegram with inline APPROVE / DECLINE buttons.
        """
        action_str = action.upper()
        if not self.is_configured():
            logger.info(
                "[SIMULATED TELEGRAM] Approval request for %s %s (Qty: %d @ ₹%.2f). Telegram credentials not configured in .env",
                action_str, symbol, qty, trigger_price
            )
            return True

        text = (
            f"🎯 *NEW QUALIFIED TRADE SETUP ({action_str})*\n\n"
            f"• *Stock:* `{symbol}` (NSE\\_EQ)\n"
            f"• *Setup Bias:* `{bias.replace('_', ' ')}`\n"
            f"• *Potential Score:* `{score}/100` (Tier A+)\n"
            f"• *5m RSI:* `{rsi_5m if rsi_5m is not None else 'N/A'}` | *RVOL:* `{f'{rvol:.2f}x' if rvol is not None else 'N/A'}`\n"
            f"• *Trigger Entry:* `₹{trigger_price:.2f}`\n\n"
            f"💰 *Capital Allocation:* `₹{CAPITAL_CONFIG.PER_TRADE_ALLOCATION:,.2f} (MIS)`\n"
            f"📦 *Position Size:* `{qty} Shares ({action_str})`\n"
            f"📊 *Portfolio Slots:* `{active_slots} / {CAPITAL_CONFIG.MAX_ACTIVE_TRADES} Active`\n\n"
            f"_User approval required before Dhan MCP order execution\\._"
        )

        if not trade_intent_id:
            from services.ai_shortlist_service import get_ai_shortlist_service
            from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts
            service = get_ai_shortlist_service_shorts() if action_str == "SELL" else get_ai_shortlist_service()
            trade_intent_id = service.create_trade_intent(
                symbol, security_id, trigger_price, qty, action_str
            )
        if not trade_intent_id:
            logger.error("Cannot send approval for %s without a persisted trade intent", symbol)
            return False

        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": f"✅ APPROVE {action_str} ({qty} shares • ₹60k)",
                        "callback_data": f"APPROVE_INTENT:{trade_intent_id}",
                    },
                    {
                        "text": "❌ DECLINE",
                        "callback_data": f"DECLINE:{symbol}",
                    },
                ]
            ]
        }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
                        "reply_markup": reply_markup,
                    },
                )
                return resp.status_code == 200
        except Exception as exc:
            logger.error("Failed to send Telegram trade alert: %s", exc)
            return False

    async def send_trade_closed_audit(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        qty: int,
        duration_str: str,
        reason: str,
        available_slots: int,
    ) -> bool:
        """
        Sends post-trade audit report to Telegram upon position closure.
        """
        gross_pnl = round((exit_price - entry_price) * qty, 2)
        pnl_pct = round(((exit_price - entry_price) / entry_price) * 100.0, 2)
        icon = "🟢" if gross_pnl >= 0 else "🔴"
        sign = "+" if gross_pnl >= 0 else ""

        text = (
            f"{icon} *TRADE CLOSED AUDIT REPORT: {symbol}*\n\n"
            f"• *Entry Price:* `₹{entry_price:.2f}`\n"
            f"• *Exit Price:* `₹{exit_price:.2f}`\n"
            f"• *Net P&L:* `{sign}₹{gross_pnl:,.2f} ({sign}{pnl_pct:.2f}%)`\n"
            f"• *Trade Duration:* `{duration_str}`\n"
            f"• *Exit Reason:* `{reason}`\n\n"
            f"🔓 *Freed Capital:* `₹{CAPITAL_CONFIG.PER_TRADE_ALLOCATION:,.2f}`\n"
            f"📊 *Available Slots:* `{available_slots} / {CAPITAL_CONFIG.MAX_ACTIVE_TRADES} Open`"
        )

        if not self.is_configured():
            logger.info("[SIMULATED TELEGRAM REPORT] %s", text.replace("*", "").replace("`", ""))
            return True

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
                    },
                )
                return resp.status_code == 200
        except Exception as exc:
            logger.error("Failed to send Telegram audit report: %s", exc)
            return False

    async def send_chat_message(self, text: str) -> bool:
        """Sends a direct markdown message to the configured Telegram chat."""
        if not self.is_configured():
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
                    },
                )
                return resp.status_code == 200
        except Exception as exc:
            logger.error("Failed to send Telegram message: %s", exc)
            return False

    async def answer_callback_query(self, callback_query_id: str, text: str):
        """Answers Telegram inline callback button to clear loading spinner."""
        if not self.is_configured() or not callback_query_id:
            return
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    f"{self.base_url}/answerCallbackQuery",
                    json={"callback_query_id": callback_query_id, "text": text[:200]},
                )
        except Exception as exc:
            logger.debug("Failed to answer callback query: %s", exc)

    async def process_callback_data(self, callback_data: str, callback_query_id: str = "") -> Dict[str, Any]:
        """
        Parses button click callback:
        - APPROVE_INTENT:{trade_intent_id}
        - DECLINE:{symbol}
        """
        logger.info("Telegram callback received: %s (query_id=%s)", callback_data, callback_query_id)
        parts = callback_data.split(":")
        action = parts[0]

        if action == "APPROVE_INTENT" and len(parts) == 2:
            intent_id = parts[1]
            if self._on_approval_callback:
                try:
                    res = await self._on_approval_callback(intent_id=intent_id)
                    status = res.get("status", "UNKNOWN")
                    await self.answer_callback_query(callback_query_id, f"{'✅' if status in {'EXECUTED', 'REPLAY'} else '❌'} {status}")
                    return res
                except Exception as exc:
                    logger.exception("Error executing intent approval %s: %s", intent_id, exc)
                    await self.answer_callback_query(callback_query_id, f"Error: {exc}")
                    return {"status": "ERROR", "reason": str(exc)}
            return {"status": "SUCCESS", "message": "Approval received"}

        if action == "APPROVE" and len(parts) >= 5:
            # Legacy callback data is accepted only for compatibility with old messages.
            # Routers must resolve it to a persisted intent before execution.
            symbol = parts[1]
            trade_action = parts[5].upper() if len(parts) >= 6 else "BUY"
            from services.ai_shortlist_service import get_ai_shortlist_service
            from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts
            service = get_ai_shortlist_service_shorts() if trade_action == "SELL" else get_ai_shortlist_service()
            trade = service.get_trade(symbol)
            intent_id = trade.get("trade_intent_id") if trade else None
            if not intent_id:
                return {"status": "FAILED", "reason": "INVALID_INTENT"}

            if self._on_approval_callback:
                try:
                    res = await self._on_approval_callback(intent_id=intent_id)
                    status = res.get("status", "UNKNOWN")
                    exec_msg = res.get("message", "")
                    reason = res.get("reason", "")
                    is_paper = res.get("paper_mode", False) or res.get("paper_fallback", False)
                    order_id = res.get("order_id", "N/A")

                    if status == "EXECUTED":
                        mode_badge = " [PAPER FALLBACK]" if res.get("paper_fallback") else ""
                        broker_rej = str(res.get("broker_rejection", ""))
                        notice_str = f"\n• *Notice:* `Broker rejected live order ({broker_rej} -> Executed in Paper Mode)`" if res.get("paper_fallback") else ""
                        await self.answer_callback_query(callback_query_id, f"✅ Trade Executed for {symbol}{mode_badge}")
                        await self.send_chat_message(
                            f"✅ *TRADE EXECUTED{mode_badge}*\n\n"
                            f"• *Symbol:* `{symbol}` (NSE\\_EQ)\n"
                            f"• *Execution:* `{qty} shares @ ₹{trigger_price:.2f}`\n"
                            f"• *Order ID:* `{order_id}`\n"
                            f"• *Watchdog Status:* `ARMED & ACTIVE (Hard SL 1.0% | BE +0.8%)`"
                            f"{notice_str}"
                        )
                    elif status == "QUEUED":
                        await self.answer_callback_query(callback_query_id, f"⏳ {symbol} Added to Priority Queue")
                        await self.send_chat_message(
                            f"⏳ *TRADE ENQUEUED (Slots 3/3 Full)*\n\n"
                            f"• *Symbol:* `{symbol}`\n"
                            f"• *Queue Position:* `Priority Queue #1`\n"
                            f"• *Trigger:* Will auto-execute immediately upon slot release."
                        )
                    elif status == "BLOCKED":
                        await self.answer_callback_query(callback_query_id, "🛡️ Master Execution Switch is OFF")
                        await self.send_chat_message(
                            f"🛡️ *EXECUTION BLOCKED: {symbol}*\n\n"
                            f"Master Execution Switch is currently **OFF** in Watchdog Settings.\n"
                            f"Enable execution switch in the UI desk to route orders to broker."
                        )
                    else:
                        await self.answer_callback_query(callback_query_id, f"❌ Execution Failed: {reason or 'Unknown error'}")
                        await self.send_chat_message(
                            f"❌ *TRADE EXECUTION FAILED: {symbol}*\n\n"
                            f"• *Error:* `{reason or exec_msg or 'Order rejected'}`\n"
                            f"• *Suggestion:* Check broker fund limits or IP whitelist in Dhan portal."
                        )
                    return res
                except Exception as exc:
                    logger.exception("Error executing approval callback for %s: %s", symbol, exc)
                    await self.answer_callback_query(callback_query_id, f"Error: {exc}")
                    await self.send_chat_message(f"⚠️ *Internal Execution Error for {symbol}:* `{exc}`")
                    return {"status": "ERROR", "reason": str(exc)}
            return {"status": "SUCCESS", "message": f"{symbol} approved"}

        elif action == "DECLINE" and len(parts) >= 2:
            symbol = parts[1]
            if self._on_decline_callback:
                try:
                    res = await self._on_decline_callback(symbol)
                    await self.answer_callback_query(callback_query_id, f"❌ {symbol} declined & removed.")
                    await self.send_chat_message(
                        f"❌ *TRADE DECLINED: {symbol}*\n\n"
                        f"Candidate rejected by trader and evicted from Live Tracking desk."
                    )
                    return res
                except Exception as exc:
                    logger.exception("Error executing decline callback for %s: %s", symbol, exc)
                    await self.answer_callback_query(callback_query_id, f"Error: {exc}")
                    return {"status": "ERROR", "reason": str(exc)}
            return {"status": "DECLINED", "symbol": symbol}

        return {"status": "IGNORED"}

    async def _long_polling_loop(self):
        """Background long-polling loop to receive button clicks without public webhook URL."""
        logger.info("Telegram HITL Long-Polling daemon started.")
        while True:
            try:
                if not self.is_configured():
                    await asyncio.sleep(5.0)
                    continue

                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(
                        f"{self.base_url}/getUpdates",
                        params={"offset": self._last_update_id + 1, "timeout": 10},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        updates = data.get("result", [])
                        for update in updates:
                            self._last_update_id = max(self._last_update_id, update.get("update_id", 0))
                            callback_query = update.get("callback_query")
                            if callback_query:
                                cb_data = callback_query.get("data", "")
                                q_id = callback_query.get("id", "")
                                try:
                                    await self.process_callback_data(cb_data, q_id)
                                except Exception as exc:
                                    logger.error("Error processing update %s: %s", update.get("update_id"), exc)
                    elif resp.status_code == 409:
                        logger.warning("Telegram polling 409 Conflict: another polling connection is active. Backing off 6s...")
                        await asyncio.sleep(6.0)
                    else:
                        logger.debug("Telegram getUpdates returned status %d: %s", resp.status_code, resp.text)
                        await asyncio.sleep(3.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Telegram polling transient error: %s", exc)
                await asyncio.sleep(3.0)

    def start_polling_daemon(self):
        """Starts background long polling task if configured and loop is active."""
        if not self.is_configured():
            return
        try:
            loop = asyncio.get_running_loop()
            if self._polling_task is None or self._polling_task.done():
                self._polling_task = loop.create_task(self._long_polling_loop())
        except RuntimeError:
            # No running event loop at import time
            pass

    def stop_polling_daemon(self):
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            self._polling_task = None


_telegram_hitl_singleton: Optional[TelegramHITLService] = None

def get_telegram_hitl_service() -> TelegramHITLService:
    global _telegram_hitl_singleton
    if _telegram_hitl_singleton is None:
        _telegram_hitl_singleton = TelegramHITLService()
    return _telegram_hitl_singleton
