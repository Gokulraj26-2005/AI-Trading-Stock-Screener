"""
Watchdog & Telegram HITL Execution Router
=========================================
Endpoints:
- GET  /api/watchdog/positions   - Returns live telemetry for up to 3 active Watchdog positions
- GET  /api/watchdog/slots       - Returns Capital Slot Manager summary (₹1.80L, ₹60k/trade, Queue)
- POST /api/watchdog/approve     - Interactive approval (Web UI or internal dispatcher)
- POST /api/watchdog/decline     - Declines a candidate trade
- POST /api/watchdog/kill-all    - Emergency manual kill-switch (exits all active positions)
- POST /api/telegram/webhook     - Telegram Webhook receiver for inline button clicks
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from auth_helpers import verify_token
from config.trading_config import CAPITAL_CONFIG
from logger import logger
from services.capital_slot_manager import get_capital_slot_manager
from services.dhan_mcp_client import get_dhan_mcp_client, normalize_order_status
from services.live_algo_watchdog import get_algo_live_watchdog
from services.telegram_hitl_service import get_telegram_hitl_service

router = APIRouter(prefix="/api/watchdog", tags=["live-watchdog"])


import os
import hmac

class ApproveTradeRequest(BaseModel):
    symbol: Optional[str] = None
    intent_id: Optional[str] = None
    security_id: str = ""
    trigger_price: Optional[float] = None
    quantity: Optional[int] = None
    action: Optional[str] = "BUY"


class DeclineTradeRequest(BaseModel):
    symbol: str


async def _execute_trade_approval(
    symbol: Optional[str] = None,
    security_id: str = "",
    trigger_price: float = 0.0,
    quantity: int = 0,
    action: str = "BUY",
    intent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Core approval handler invoked by Telegram inline button or Web UI.
    """
    from services.ai_shortlist_service import get_ai_shortlist_service
    from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts

    slot_mgr = get_capital_slot_manager()
    watchdog = get_algo_live_watchdog()
    mcp = get_dhan_mcp_client()
    shortlist_svc = get_ai_shortlist_service()
    shortlist_shorts = get_ai_shortlist_service_shorts()

    intent_service = shortlist_svc
    if intent_id:
        claim = shortlist_svc.claim_trade_intent(intent_id)
        if claim["status"] == "INVALID_INTENT":
            claim = shortlist_shorts.claim_trade_intent(intent_id)
            intent_service = shortlist_shorts
        if claim["status"] == "IN_PROGRESS":
            return {"status": "IN_PROGRESS", "reason": "Approval is already being processed"}
        if claim["status"] == "REPLAY":
            return claim["result"]
        if claim["status"] != "CLAIMED":
            return {"status": "FAILED", "reason": claim.get("reason", "INVALID_INTENT")}
        intent = claim["trade"]
        symbol = intent["symbol"]
        security_id = intent.get("intent_security_id") or ""
        trigger_price = float(intent["intent_trigger_price"])
        quantity = int(intent["intent_quantity"])
        action = str(intent["intent_action"] or "BUY").upper()
    if not symbol:
        return {"status": "FAILED", "reason": "MISSING_SYMBOL"}

    def finish(result: Dict[str, Any]) -> Dict[str, Any]:
        if intent_id:
            intent_service.complete_trade_intent(intent_id, result)
        return result

    # Look up original setup from database if available
    trade_record = intent if intent_id else shortlist_svc.get_trade(symbol)
    if not intent_id:
        if not trade_record:
            short_record = shortlist_shorts.get_trade(symbol)
            if short_record:
                trade_record = short_record
                action = "SELL"
        elif trade_record.get("action"):
            action = trade_record["action"].upper()

    # If action is SELL, route to dedicated shorts approval workflow
    if action.upper() == "SELL":
        from routers.watchdog_router_shorts import _execute_trade_approval_shorts
        result = await _execute_trade_approval_shorts(
            symbol=symbol,
            security_id=security_id,
            trigger_price=trigger_price,
            quantity=quantity,
            action="SELL",
            intent_id=None,
        )
        return finish(result)

    # Master Execution Killswitch Guard
    if not CAPITAL_CONFIG.execution_enabled:
        logger.warning("Master Execution Switch is OFF. Blocked approval for %s.", symbol)
        return finish({
            "status": "BLOCKED",
            "message": "Execution Switch is currently OFF. Enable in Watchdog Settings to route orders.",
            "symbol": symbol,
        })

    stop_price = float((trade_record.get("intent_stop_loss") if intent_id else trade_record.get("stop_loss")) or 0.0) if trade_record else 0.0
    if stop_price <= 0:
        stop_price = round(trigger_price * (1.0 - CAPITAL_CONFIG.MAX_HARD_STOP_LOSS_PCT), 2)
    if security_id:
        leverage_result = mcp.get_security_leverage(security_id, trigger_price)
        leverage = (
            await leverage_result
            if inspect.isawaitable(leverage_result)
            else float(getattr(CAPITAL_CONFIG, "MIS_LEVERAGE", 5.0))
        )
    else:
        leverage = float(getattr(CAPITAL_CONFIG, "MIS_LEVERAGE", 5.0))
    calculated_qty = slot_mgr.calculate_execution_quantity(
        trigger_price, stop_price, leverage=leverage
    )
    qty = int(calculated_qty) if isinstance(calculated_qty, (int, float)) else int(quantity)
    if qty <= 0:
        return finish({"status": "FAILED", "reason": "INVALID_RISK_SIZE", "symbol": symbol})

    reservation_id = intent_id or f"approval:{symbol.upper()}:{security_id}:{trigger_price}:{qty}"
    if isinstance(slot_mgr.reservation_for(symbol), str):
        return finish({"status": "IN_PROGRESS", "reason": "Order submission is already in progress"})
    if symbol.upper() in slot_mgr.active_slots:
        return finish({"status": "REPLAY", "reason": "Position is already active", "symbol": symbol})
    if not slot_mgr.reserve_slot(symbol, reservation_id):
        # Enqueue in Priority Queue
        candidate = {
            "symbol": symbol,
            "security_id": security_id,
            "score": trade_record.get("quality_score", 90) if trade_record else 90,
            "bias": "BULLISH_BREAKOUT",
            "trigger_price": trigger_price,
            "quantity": qty,
        }
        slot_mgr.enqueue_approved_trade(candidate)
        shortlist_svc.update_trade_status(symbol, "QUEUED")
        logger.info("Trade %s (%s) placed in Priority Queue (Slots 3/3 Full)", symbol, action)
        return finish({
            "status": "QUEUED",
            "message": f"All 3 slots full. {symbol} added to Priority Queue #1.",
            "symbol": symbol,
            "quantity": qty,
        })

    # Slot Available: Dispatch MIS Order via Dhan MCP
    try:
        order_res = await mcp.place_mis_order(
            security_id=security_id,
            transaction_type=action.upper(),
            quantity=qty,
            price=trigger_price,
            order_type="MARKET",
            symbol_hint=symbol,
        )
    except Exception:
        slot_mgr.release_reservation(symbol, reservation_id)
        raise

    lifecycle_status = normalize_order_status(
        order_res.get("lifecycle_status", order_res.get("order_status"))
    )
    order_id = str(order_res.get("order_id", f"ORD_{symbol}_{int(trigger_price)}"))

    if order_res.get("status") == "SUCCESS" and lifecycle_status in {"ACCEPTED", "TRANSIT", "PARTIAL"}:
        import asyncio
        for _ in range(10):  # Poll for up to 5 seconds
            await asyncio.sleep(0.5)
            try:
                poll_res = await mcp.get_order_status(order_id)
                if poll_res and poll_res.get("status") == "SUCCESS":
                    poll_lifecycle = poll_res.get("lifecycle_status")
                    if poll_lifecycle == "FILLED":
                        order_res["order_status"] = poll_res.get("order_status")
                        order_res["lifecycle_status"] = "FILLED"
                        order_res["fill_price"] = poll_res.get("fill_price", trigger_price)
                        order_res["filled_quantity"] = poll_res.get("filled_quantity", qty)
                        lifecycle_status = "FILLED"
                        break
                    elif poll_lifecycle in {"CANCELLED", "REJECTED"}:
                        order_res["lifecycle_status"] = poll_lifecycle
                        lifecycle_status = poll_lifecycle
                        break
            except Exception:
                pass

    from db_store import save_lifecycle_order
    save_lifecycle_order(
        order_id=order_id,
        symbol=symbol,
        security_id=security_id,
        action=action.upper(),
        quantity=qty,
        requested_price=trigger_price,
        status=lifecycle_status,
        filled_price=float(order_res.get("fill_price", trigger_price) or trigger_price)
        if lifecycle_status == "FILLED" else None,
        payload=order_res,
        filled_quantity=int(order_res.get("filled_quantity", qty) or 0)
        if lifecycle_status == "FILLED" else int(order_res.get("filled_quantity", 0) or 0),
    )
    if not (order_res.get("status") == "SUCCESS" and lifecycle_status == "FILLED"):
        slot_mgr.release_reservation(symbol, reservation_id)
    if order_res.get("status") == "SUCCESS" and lifecycle_status == "FILLED":
        is_paper = bool(order_res.get("paper_mode", False) or order_res.get("paper_fallback", False))
        sl_val = float((trade_record.get("intent_stop_loss") if intent_id else trade_record.get("stop_loss")) or 0.0) if trade_record else 0.0
        t1_val = float((trade_record.get("intent_target_1") if intent_id else trade_record.get("target_1")) or 0.0) if trade_record else 0.0
        t2_val = float((trade_record.get("intent_target_2") if intent_id else trade_record.get("target_2")) or 0.0) if trade_record else 0.0

        # Register in Watchdog
        watchdog.register_position(
            symbol=symbol,
            security_id=security_id,
            entry_price=trigger_price,
            quantity=qty,
            order_id=order_id,
            action=action.upper(),
            stop_loss=sl_val if sl_val > 0 else None,
            target_1=t1_val if t1_val > 0 else None,
            target_2=t2_val if t2_val > 0 else None,
            is_paper=is_paper,
        )
        shortlist_svc.update_trade_status(symbol, "ACTIVE_IN_TRADE")
        mode_str = " (Paper Fallback)" if order_res.get("paper_fallback") else " (Paper Mode)" if order_res.get("paper_mode") else ""
        logger.info("✅ Trade Approved & Executed via Dhan MCP%s: %s %s (Qty: %d @ ₹%.2f)", mode_str, action, symbol, qty, trigger_price)
        return finish({
            "status": "EXECUTED",
            "message": f"Order executed for {symbol} ({action} {qty} shares @ ₹{trigger_price:.2f}){mode_str}",
            "order_id": order_id,
            "symbol": symbol,
            "quantity": qty,
            "paper_mode": order_res.get("paper_mode", False),
            "paper_fallback": order_res.get("paper_fallback", False),
            "broker_rejection": order_res.get("broker_rejection", ""),
        })
    elif order_res.get("status") == "SUCCESS" and lifecycle_status in {"ACCEPTED", "TRANSIT", "PARTIAL"}:
        logger.info(
            "Order accepted but not filled for %s: order_id=%s, lifecycle=%s",
            symbol,
            order_res.get("order_id", ""),
            lifecycle_status,
        )
        return finish({
            "status": lifecycle_status,
            "message": f"Order accepted for {symbol}; watchdog registration awaits fill confirmation.",
            "order_id": str(order_res.get("order_id", "")),
            "symbol": symbol,
            "quantity": qty,
            "paper_mode": bool(order_res.get("paper_mode", False)),
            "paper_fallback": bool(order_res.get("paper_fallback", False)),
        })
    else:
        err = order_res.get("reason", "Order placement failed")
        logger.error("Dhan MCP order execution failed for %s: %s", symbol, err)
        return finish({"status": "FAILED", "reason": err, "message": f"Order failed for {symbol}: {err}"})


async def _execute_trade_decline(symbol: str) -> Dict[str, Any]:
    from services.ai_shortlist_service import get_ai_shortlist_service
    from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts
    shortlist_svc = get_ai_shortlist_service()
    shortlist_shorts = get_ai_shortlist_service_shorts()
    slot_mgr = get_capital_slot_manager()
    slot_mgr.remove_from_queue(symbol)
    shortlist_svc.delete_trade(symbol)
    shortlist_shorts.delete_trade(symbol)
    logger.info("❌ Trade %s declined and removed from desk.", symbol)
    return {"status": "DECLINED", "symbol": symbol, "message": f"{symbol} declined and removed."}


# Register callbacks with Telegram Service
telegram_svc = get_telegram_hitl_service()
telegram_svc.register_callbacks(
    on_approval=_execute_trade_approval,
    on_decline=_execute_trade_decline,
)
telegram_svc.start_polling_daemon()


@router.get("/positions")
async def get_active_watchdog_positions(user_id: str = Depends(verify_token)) -> Dict[str, Any]:
    """Returns real-time status of all active Watchdog positions (Longs + Shorts combined)."""
    from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
    watchdog = get_algo_live_watchdog()
    watchdog_shorts = get_algo_live_watchdog_shorts()
    long_positions = watchdog.get_active_positions()
    short_positions = watchdog_shorts.get_active_positions()
    combined = long_positions + short_positions
    return {
        "status": "ok",
        "count": len(combined),
        "long_count": len(long_positions),
        "short_count": len(short_positions),
        "max_slots": CAPITAL_CONFIG.MAX_ACTIVE_TRADES,
        "positions": combined,
    }


@router.get("/slots")
async def get_capital_slots_summary(user_id: str = Depends(verify_token)) -> Dict[str, Any]:
    """Returns capital deployment status and priority queue."""
    slot_mgr = get_capital_slot_manager()
    return slot_mgr.get_portfolio_summary()


@router.post("/approve")
async def manual_approve_trade(
    payload: ApproveTradeRequest,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Interactive approval button from Web UI."""
    if payload.intent_id:
        return await _execute_trade_approval(intent_id=payload.intent_id)
    if not payload.symbol or payload.trigger_price is None:
        raise HTTPException(status_code=400, detail="intent_id or complete legacy trade fields are required")
    slot_mgr = get_capital_slot_manager()
    mcp = get_dhan_mcp_client()
    if payload.quantity and payload.quantity > 0:
        qty = payload.quantity
    else:
        leverage = await mcp.get_security_leverage(payload.security_id, payload.trigger_price) if payload.security_id else float(getattr(CAPITAL_CONFIG, "MIS_LEVERAGE", 5.0))
        qty = slot_mgr.calculate_quantity(payload.trigger_price, leverage=leverage)
    res = await _execute_trade_approval(intent_id=payload.intent_id, symbol=payload.symbol,
                                        security_id=payload.security_id, trigger_price=payload.trigger_price,
                                        quantity=qty, action=payload.action or "BUY")
    return res


@router.post("/decline")
async def manual_decline_trade(
    payload: DeclineTradeRequest,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Interactive decline button from Web UI."""
    return await _execute_trade_decline(payload.symbol)


@router.post("/kill-all")
async def emergency_kill_all_positions(user_id: str = Depends(verify_token)) -> Dict[str, Any]:
    """Emergency Kill-Switch: Exits all open positions immediately across Longs and Shorts."""
    from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
    watchdog = get_algo_live_watchdog()
    watchdog_shorts = get_algo_live_watchdog_shorts()
    long_res = await watchdog.emergency_kill_all()
    short_res = await watchdog_shorts.emergency_kill_all()
    return {
        "status": "ALL_POSITIONS_KILLED",
        "longs": long_res,
        "shorts": short_res,
    }


class PromoteCandidateRequest(BaseModel):
    symbol: str
    score: int
    bias: str = "BULLISH_BREAKOUT"
    live_status: str
    trigger_price: float
    current_price: float
    rsi_5m: Optional[float] = None
    rvol: Optional[float] = None
    quote_source: Optional[str] = None
    quote_age_ms: Optional[int] = None
    rsi_is_real: bool = False
    trigger_correlation_id: Optional[str] = None
    thesis: str = ""
    stop_loss: Optional[float] = None


class WatchdogTickRequest(BaseModel):
    symbol: str
    ltp: float
    vwap_5m: Optional[float] = None
    ema20_5m: Optional[float] = None
    rsi_5m: Optional[float] = None
    cvd_delta: float = 0.0
    bid_ask_ratio: float = 1.0


@router.post("/promote-candidate")
async def promote_candidate_endpoint(
    payload: PromoteCandidateRequest,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """
    Evaluates candidate from Live Analysis against strict institutional criteria.
    If confirmed, promotes to Live Tracking desk and triggers Telegram HITL alert.
    """
    from services.promotion_manager import get_promotion_manager
    promo = get_promotion_manager()
    res = await promo.evaluate_and_promote(
        symbol=payload.symbol,
        score=payload.score,
        bias=payload.bias,
        live_status=payload.live_status,
        trigger_price=payload.trigger_price,
        current_price=payload.current_price,
        rsi_5m=payload.rsi_5m,
        rvol=payload.rvol,
        quote_source=payload.quote_source,
        quote_age_ms=payload.quote_age_ms,
        rsi_is_real=payload.rsi_is_real,
        trigger_correlation_id=payload.trigger_correlation_id,
        thesis=payload.thesis,
        stop_loss=payload.stop_loss,
    )
    return res


@router.post("/evaluate-tick")
async def evaluate_watchdog_tick(
    payload: WatchdogTickRequest,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """
    Tick ingestion endpoint for active Watchdog positions (evaluates Hard SL, Breakeven, Trailing, RSI 80+).
    """
    watchdog = get_algo_live_watchdog()
    return await watchdog.evaluate_tick(
        symbol=payload.symbol,
        ltp=payload.ltp,
        vwap_5m=payload.vwap_5m,
        ema20_5m=payload.ema20_5m,
        rsi_5m=payload.rsi_5m,
        cvd_delta=payload.cvd_delta,
        bid_ask_ratio=payload.bid_ask_ratio,
    )


@router.post("/check-purge")
async def check_purge_endpoint(
    symbol: str,
    current_price: float,
    trigger_price: float,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """
    Auto-purge check: If an unapproved stock in Live Tracking falls >0.20% below trigger, evicts it.
    """
    from services.promotion_manager import get_promotion_manager
    promo = get_promotion_manager()
    purged = promo.evaluate_auto_purge(symbol, current_price, trigger_price)
    return {"symbol": symbol, "purged": purged}


@router.post("/telegram-webhook")
async def telegram_webhook_endpoint(request: Request) -> Dict[str, Any]:
    """Secured webhook receiver for Telegram update callbacks with HMAC and chat validation."""
    # 1. Verify Telegram secret token header if configured in environment
    secret_token = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if secret_token:
        header_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(header_token, secret_token):
            logger.warning("Telegram webhook rejected: invalid X-Telegram-Bot-Api-Secret-Token")
            raise HTTPException(status_code=403, detail="Invalid webhook secret token")

    try:
        body = await request.json()
        callback_query = body.get("callback_query")
        if not callback_query:
            return {"status": "ignored"}

        # 2. Verify chat ID matches configured chat
        chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
        expected_chat = str(os.getenv("TELEGRAM_CHAT_ID", getattr(CAPITAL_CONFIG, "TELEGRAM_CHAT_ID", "")))
        if expected_chat and chat_id and chat_id != expected_chat:
            logger.warning("Telegram webhook from unauthorized chat: %s (expected %s)", chat_id, expected_chat)
            raise HTTPException(status_code=403, detail="Unauthorized chat")

        cb_data = callback_query.get("data", "")
        q_id = callback_query.get("id", "")
        res = await telegram_svc.process_callback_data(cb_data, q_id)
        return {"status": "ok", "result": res}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Telegram webhook error: %s", exc)
        return {"status": "error", "detail": str(exc)}


class UpdateSettingsRequest(BaseModel):
    execution_enabled: Optional[bool] = None
    total_capital: Optional[float] = None
    per_trade_allocation: Optional[float] = None
    max_active_trades: Optional[int] = None
    max_hard_stop_loss_percent: Optional[float] = None
    breakeven_trigger_percent: Optional[float] = None
    structural_trail_percent: Optional[float] = None
    purge_slip_tolerance_percent: Optional[float] = None


class ToggleExecutionRequest(BaseModel):
    enabled: Optional[bool] = None


@router.get("/settings")
async def get_watchdog_settings_endpoint(user_id: str = Depends(verify_token)) -> Dict[str, Any]:
    """Returns current Watchdog execution settings, master switch state, and depth intelligence telemetry."""
    watchdog = get_algo_live_watchdog()

    # Build per-position depth level telemetry
    depth_telemetry = []
    for sym, pos in watchdog.active_positions.items():
        depth_telemetry.append({
            "symbol": sym,
            "depth_level_active": getattr(pos, "depth_level_active", "20-level"),
            "security_id": pos.security_id,
        })

    # Summarise 200-depth manager status
    depth_mgr = getattr(watchdog, "_depth_manager", None)
    active_conns = getattr(depth_mgr, "active_connections", {}) if depth_mgr else {}
    depth_manager_status = {
        "enabled": depth_mgr is not None,
        "active_200depth_connections": len(active_conns) if depth_mgr else 0,
        "max_200depth_slots": getattr(depth_mgr, "max_connections", 0) if depth_mgr else 0,
        "positions_using_200level": [
            sym for sym, pos in watchdog.active_positions.items()
            if getattr(pos, "depth_level_active", "") == "200-level"
        ],
        "positions_using_20level": [
            sym for sym, pos in watchdog.active_positions.items()
            if getattr(pos, "depth_level_active", "") == "20-level"
        ],
    }

    return {
        "status": "ok",
        "settings": CAPITAL_CONFIG.to_dict(),
        "depth_intelligence": {
            "per_position": depth_telemetry,
            "summary": depth_manager_status,
            "fallback_chain": "200-level → 20-level (never 5-level)",
            "analytical_pool_size": 47,   # top 47 for continuous 20-depth
            "watchdog_reserved_slots": 3,  # reserved for 200-depth active trades
        },
    }


@router.post("/settings")
async def update_watchdog_settings_endpoint(
    payload: UpdateSettingsRequest,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Updates customizable Watchdog sizing, risk boundaries, and master execution switch."""
    updates = {k: v for k, v in payload.dict().items() if v is not None}
    updated_settings = CAPITAL_CONFIG.update_settings(updates)
    logger.info("Watchdog settings updated by %s: %s", user_id, updates)
    return {
        "status": "ok",
        "message": "Watchdog settings updated successfully",
        "settings": updated_settings,
    }


@router.post("/toggle-execution")
async def toggle_execution_endpoint(
    payload: Optional[ToggleExecutionRequest] = None,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """1-Click Toggle for the Master Execution Switch (ON/OFF before Telegram & MCP)."""
    target = payload.enabled if payload else None
    new_state = CAPITAL_CONFIG.toggle_execution(target)
    state_str = "ARMED (ON)" if new_state else "STANDBY (OFF)"
    logger.info("Master Execution Switch toggled to %s by %s", state_str, user_id)
    return {
        "status": "ok",
        "execution_enabled": new_state,
        "message": f"Execution switch is now {state_str}. {'Telegram alerts and MCP routing active.' if new_state else 'Telegram alerts and MCP order placement blocked.'}",
    }
