"""
Watchdog Execution Router for SHORTS (VIA-Agent7)
=================================================
Endpoints:
- GET  /api/watchdog-shorts/positions          - Returns live telemetry for active Short Watchdog positions
- GET  /api/watchdog-shorts/slots              - Returns Capital Slot Manager summary (shared ₹1.80L pool)
- POST /api/watchdog-shorts/approve            - Interactive approval for short trades (SELL action)
- POST /api/watchdog-shorts/decline            - Declines a candidate short trade
- POST /api/watchdog-shorts/kill-all           - Emergency manual kill-switch (exits all active short positions)
- POST /api/watchdog-shorts/promote-candidate  - Evaluates candidate breakdown for promotion
- POST /api/watchdog-shorts/evaluate-tick      - Evaluates short position micro-tick rules
- POST /api/watchdog-shorts/check-purge        - Evaluates auto-purge for short setup
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth_helpers import verify_token
from config.trading_config import CAPITAL_CONFIG
from logger import logger
from services.capital_slot_manager import get_capital_slot_manager
from services.dhan_mcp_client import get_dhan_mcp_client, normalize_order_status
from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts

router = APIRouter(prefix="/api/watchdog-shorts", tags=["live-watchdog-shorts"])


class ApproveShortTradeRequest(BaseModel):
    symbol: Optional[str] = None
    intent_id: Optional[str] = None
    security_id: str = ""
    trigger_price: Optional[float] = None
    quantity: Optional[int] = None
    action: Optional[str] = "SELL"


class DeclineShortTradeRequest(BaseModel):
    symbol: str


async def _execute_trade_approval_shorts(
    symbol: Optional[str] = None,
    security_id: str = "",
    trigger_price: float = 0.0,
    quantity: int = 0,
    action: str = "SELL",
    intent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Core approval handler for short trades.
    Dispatches MIS SELL order via Dhan MCP and registers into AlgoLiveWatchdogShorts.
    """
    slot_mgr = get_capital_slot_manager()
    watchdog_shorts = get_algo_live_watchdog_shorts()
    mcp = get_dhan_mcp_client()
    shortlist_svc = get_ai_shortlist_service_shorts()

    trade_record = shortlist_svc.get_trade(symbol) if symbol else None
    if intent_id:
        claim = shortlist_svc.claim_trade_intent(intent_id)
        if claim["status"] == "REPLAY":
            return claim["result"]
        if claim["status"] != "CLAIMED":
            return {"status": "FAILED", "reason": claim.get("reason", "INVALID_INTENT")}
        intent = claim["trade"]
        symbol = intent["symbol"]
        security_id = intent.get("intent_security_id") or ""
        trigger_price = float(intent["intent_trigger_price"])
        quantity = int(intent["intent_quantity"])
        action = "SELL"
        trade_record = intent

    def finish(result: Dict[str, Any]) -> Dict[str, Any]:
        if intent_id:
            shortlist_svc.complete_trade_intent(intent_id, result)
        return result

    # Master Execution Killswitch Guard
    if not CAPITAL_CONFIG.execution_enabled:
        logger.warning("Master Execution Switch is OFF. Blocked short approval for %s.", symbol)
        return finish({
            "status": "BLOCKED",
            "message": "Execution Switch is currently OFF. Enable in Watchdog Settings to route orders.",
            "symbol": symbol,
        })

    stop_price = float((trade_record.get("intent_stop_loss") if intent_id else trade_record.get("stop_loss")) or 0.0) if trade_record else 0.0
    if stop_price <= 0:
        stop_price = round(trigger_price * (1.0 + CAPITAL_CONFIG.MAX_HARD_STOP_LOSS_PCT), 2)
    leverage = await mcp.get_security_leverage(security_id, trigger_price) if security_id else float(getattr(CAPITAL_CONFIG, "MIS_LEVERAGE", 5.0))
    qty = slot_mgr.calculate_execution_quantity(trigger_price, stop_price, leverage=leverage)
    if qty <= 0:
        return finish({"status": "FAILED", "reason": "INVALID_RISK_SIZE", "symbol": symbol})

    reservation_id = intent_id or f"approval:{symbol.upper()}:{security_id}:{trigger_price}:{qty}"
    if isinstance(slot_mgr.reservation_for(symbol), str):
        return finish({"status": "IN_PROGRESS", "reason": "Order submission is already in progress"})
    if symbol.upper() in slot_mgr.active_slots:
        return finish({"status": "REPLAY", "reason": "Position is already active", "symbol": symbol})
    if not slot_mgr.reserve_slot(symbol, reservation_id):
        candidate = {
            "symbol": symbol,
            "security_id": security_id,
            "score": trade_record.get("quality_score", 90) if trade_record else 90,
            "bias": "BEARISH_BREAKDOWN",
            "trigger_price": trigger_price,
            "quantity": qty,
            "action": "SELL",
        }
        slot_mgr.enqueue_approved_trade(candidate)
        shortlist_svc.update_trade_status(symbol, "QUEUED")
        logger.info("Short Trade %s (SELL) placed in Priority Queue (Slots 3/3 Full)", symbol)
        return finish({
            "status": "QUEUED",
            "message": f"All 3 slots full. {symbol} added to Priority Queue #1 (Short).",
            "symbol": symbol,
            "quantity": qty,
        })

    # Slot Available: Dispatch MIS SELL Order via Dhan MCP
    try:
        order_res = await mcp.place_mis_order(
            security_id=security_id,
            transaction_type="SELL",
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
    order_id = str(order_res.get("order_id", f"ORD_SHORT_{symbol}_{int(trigger_price)}"))

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
        action="SELL",
        quantity=qty,
        requested_price=trigger_price,
        status=lifecycle_status,
        filled_price=float(order_res.get("fill_price", trigger_price) or trigger_price)
        if lifecycle_status == "FILLED" else None,
        payload=order_res,
    )
    if not (order_res.get("status") == "SUCCESS" and lifecycle_status == "FILLED"):
        slot_mgr.release_reservation(symbol, reservation_id)
    if order_res.get("status") == "SUCCESS" and lifecycle_status == "FILLED":
        is_paper = bool(order_res.get("paper_mode", False) or order_res.get("paper_fallback", False))
        sl_val = float((trade_record.get("intent_stop_loss") if intent_id else trade_record.get("stop_loss")) or 0.0) if trade_record else 0.0
        t1_val = float((trade_record.get("intent_target_1") if intent_id else trade_record.get("target_1")) or 0.0) if trade_record else 0.0
        t2_val = float((trade_record.get("intent_target_2") if intent_id else trade_record.get("target_2")) or 0.0) if trade_record else 0.0

        # Register in WatchdogShorts
        watchdog_shorts.register_position(
            symbol=symbol,
            security_id=security_id,
            entry_price=trigger_price,
            quantity=qty,
            order_id=order_id,
            action="SELL",
            stop_loss=sl_val if sl_val > 0 else None,
            target_1=t1_val if t1_val > 0 else None,
            target_2=t2_val if t2_val > 0 else None,
            is_paper=is_paper,
        )
        shortlist_svc.update_trade_status(symbol, "ACTIVE_IN_TRADE")
        mode_str = " (Paper Fallback)" if order_res.get("paper_fallback") else " (Paper Mode)" if order_res.get("paper_mode") else ""
        logger.info("✅ Short Trade Approved & Executed via Dhan MCP%s: SELL %s (Qty: %d @ ₹%.2f)", mode_str, symbol, qty, trigger_price)
        return finish({
            "status": "EXECUTED",
            "message": f"Short order executed for {symbol} (SELL {qty} shares @ ₹{trigger_price:.2f}){mode_str}",
            "order_id": order_id,
            "symbol": symbol,
            "quantity": qty,
            "paper_mode": order_res.get("paper_mode", False),
            "paper_fallback": order_res.get("paper_fallback", False),
            "broker_rejection": order_res.get("broker_rejection", ""),
        })
    elif order_res.get("status") == "SUCCESS" and lifecycle_status in {"ACCEPTED", "TRANSIT", "PARTIAL"}:
        logger.info(
            "Short order accepted but not filled for %s: order_id=%s, lifecycle=%s",
            symbol,
            order_res.get("order_id", ""),
            lifecycle_status,
        )
        return finish({
            "status": lifecycle_status,
            "message": f"Short order accepted for {symbol}; watchdog registration awaits fill confirmation.",
            "order_id": str(order_res.get("order_id", "")),
            "symbol": symbol,
            "quantity": qty,
            "paper_mode": bool(order_res.get("paper_mode", False)),
            "paper_fallback": bool(order_res.get("paper_fallback", False)),
        })
    else:
        err = order_res.get("reason", "Order placement failed")
        logger.error("Dhan MCP short order execution failed for %s: %s", symbol, err)
        return finish({"status": "FAILED", "reason": err, "message": f"Short order failed for {symbol}: {err}"})


async def _execute_trade_decline_shorts(symbol: str) -> Dict[str, Any]:
    shortlist_svc = get_ai_shortlist_service_shorts()
    slot_mgr = get_capital_slot_manager()
    slot_mgr.remove_from_queue(symbol)
    shortlist_svc.delete_trade(symbol)
    logger.info("❌ Short trade %s declined and removed from shorts desk.", symbol)
    return {"status": "DECLINED", "symbol": symbol, "message": f"Short trade {symbol} declined and removed."}


@router.get("/positions")
async def get_active_watchdog_shorts_positions(user_id: str = Depends(verify_token)) -> Dict[str, Any]:
    """Returns real-time status of all active Short Watchdog positions."""
    watchdog = get_algo_live_watchdog_shorts()
    positions = watchdog.get_active_positions()
    return {
        "status": "ok",
        "count": len(positions),
        "max_slots": CAPITAL_CONFIG.MAX_ACTIVE_TRADES,
        "positions": positions,
    }


@router.get("/slots")
async def get_capital_slots_summary_shorts(user_id: str = Depends(verify_token)) -> Dict[str, Any]:
    """Returns capital deployment status from the shared capital pool."""
    slot_mgr = get_capital_slot_manager()
    return slot_mgr.get_portfolio_summary()


@router.post("/approve")
async def manual_approve_short_trade(
    payload: ApproveShortTradeRequest,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Interactive approval button from Shorts Web UI."""
    if payload.intent_id:
        return await _execute_trade_approval_shorts(intent_id=payload.intent_id)
    if not payload.symbol or payload.trigger_price is None:
        raise HTTPException(status_code=400, detail="intent_id or complete legacy trade fields are required")
    slot_mgr = get_capital_slot_manager()
    mcp = get_dhan_mcp_client()
    if payload.quantity and payload.quantity > 0:
        qty = payload.quantity
    else:
        leverage = await mcp.get_security_leverage(payload.security_id, payload.trigger_price) if payload.security_id else float(getattr(CAPITAL_CONFIG, "MIS_LEVERAGE", 5.0))
        qty = slot_mgr.calculate_quantity(payload.trigger_price, leverage=leverage)
    return await _execute_trade_approval_shorts(
        symbol=payload.symbol,
        security_id=payload.security_id,
        trigger_price=payload.trigger_price,
        quantity=qty,
        action="SELL",
        intent_id=payload.intent_id,
    )


@router.post("/decline")
async def manual_decline_short_trade(
    payload: DeclineShortTradeRequest,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Interactive decline button from Shorts Web UI."""
    return await _execute_trade_decline_shorts(payload.symbol)


@router.post("/kill-all")
async def emergency_kill_all_shorts_positions(user_id: str = Depends(verify_token)) -> Dict[str, Any]:
    """Emergency Kill-Switch: Exits all open short positions immediately."""
    watchdog = get_algo_live_watchdog_shorts()
    return await watchdog.emergency_kill_all()


class PromoteCandidateShortRequest(BaseModel):
    symbol: str
    score: int
    bias: str = "BEARISH_BREAKDOWN"
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


class WatchdogTickShortRequest(BaseModel):
    symbol: str
    ltp: float
    vwap_5m: Optional[float] = None
    ema20_5m: Optional[float] = None
    rsi_5m: Optional[float] = None
    cvd_delta: float = 0.0
    bid_ask_ratio: float = 1.0


@router.post("/promote-candidate")
async def promote_candidate_short_endpoint(
    payload: PromoteCandidateShortRequest,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Evaluates short candidate from Live Analysis for promotion to Shorts desk."""
    from services.promotion_manager_shorts import get_promotion_manager_shorts
    promo = get_promotion_manager_shorts()
    return await promo.evaluate_and_promote(
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


@router.post("/evaluate-tick")
async def evaluate_watchdog_short_tick(
    payload: WatchdogTickShortRequest,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Tick ingestion endpoint for active short positions."""
    watchdog = get_algo_live_watchdog_shorts()
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
async def check_purge_short_endpoint(
    symbol: str,
    current_price: float,
    trigger_price: float,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Auto-purge check for short setup slipping above trigger."""
    from services.promotion_manager_shorts import get_promotion_manager_shorts
    promo = get_promotion_manager_shorts()
    purged = promo.evaluate_auto_purge(symbol, current_price, trigger_price)
    return {"symbol": symbol, "purged": purged}
