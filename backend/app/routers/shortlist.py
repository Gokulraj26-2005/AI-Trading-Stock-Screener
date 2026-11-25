"""
Shortlisted Trades & AI Recommendation Router for VIA-Agent7
============================================================
Routes:
-------
POST   /api/shortlist/generate      - Invokes AI model to select top 2-5 high conviction setups
GET    /api/shortlist               - Returns all currently active shortlisted trades
GET    /api/shortlist/timeline      - Returns chronological IST timeline micro-events
POST   /api/shortlist/poll-status   - Evaluates real-time price surveillance on active trades
DELETE /api/shortlist/{symbol}      - Removes a trade from active surveillance
POST   /api/shortlist/clear         - Clears all active shortlisted trades and history
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth_helpers import verify_token, validate_symbol
from services.ai_shortlist_service import get_ai_shortlist_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/shortlist", tags=["ai-shortlist"])


class GenerateShortlistRequest(BaseModel):
    universe_data: List[Dict[str, Any]] = Field(..., description="Token-compacted candidates from chart analysis")
    model: str = Field("google/gemini-2.5-flash", description="AI Model identifier (e.g. claude-3-5-sonnet, gemini-2.5-flash)")
    api_key: Optional[str] = Field(None, description="Optional user-provided API key")
    provider: str = Field("openrouter", description="Provider: openrouter | openai | gemini | anthropic | ollama")
    custom_endpoint: Optional[str] = Field(None, description="Optional custom base URL for Ollama or local LLM")


class AddManualTradeRequest(BaseModel):
    symbol: str = Field(..., description="Stock symbol, e.g. RELIANCE")
    action: str = Field("BUY", description="BUY or SELL")
    current_price: Optional[float] = Field(None, description="Current market price from chart analysis")
    entry_price: Optional[float] = Field(None, description="Custom entry/trigger price")
    stop_loss: Optional[float] = Field(None, description="Custom stop loss")
    target_1: Optional[float] = Field(None, description="Custom target 1")
    target_2: Optional[float] = Field(None, description="Custom target 2")
    quantity: Optional[int] = Field(None, description="Explicit position quantity")
    setup_type: Optional[str] = Field(None, description="Custom setup type")
    thesis: Optional[str] = Field(None, description="Custom thesis")
    catalyst_summary: Optional[str] = Field(None, description="Catalyst summary")
    tape_evidence: Optional[str] = Field(None, description="Tape evidence")
    risk_reward_ratio: Optional[float] = Field(None, description="Risk-to-reward ratio")


@router.post("/add")
async def add_manual_trade(
    payload: AddManualTradeRequest,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """
    Manually adds a stock directly to the Live Watchdog for immediate risk surveillance.
    Automatically calculates levels using multi-timeframe analysis if not provided.
    """
    raw_sym = validate_symbol(payload.symbol)
    action = payload.action.upper()
    if action != "BUY":
        raise HTTPException(
            status_code=400,
            detail="SELL manual trades must be added through the Shorts Desk.",
        )
    service = get_ai_shortlist_service()

    from services.dhan_instrument_resolver import get_instrument_resolver, resolve_canonical_symbol
    resolver = get_instrument_resolver()
    resolver.load()

    canonical = resolve_canonical_symbol(raw_sym)
    sec_id = resolver.get_security_id(canonical) or resolver.get_security_id(raw_sym)
    if not sec_id:
        candidates = [
            k for k in resolver._nse_eq_map.keys()
            if k.startswith(raw_sym) and " " not in k and not k.endswith("-EQ")
        ]
        if candidates:
            candidates.sort(key=lambda c: len(c))
            canonical = candidates[0]
            sec_id = resolver.get_security_id(canonical)

    if not sec_id:
        raise HTTPException(
            status_code=400,
            detail=f"Stock symbol '{raw_sym}' could not be resolved on NSE. Please verify the symbol and try again.",
        )

    sym = raw_sym

    entry_px = payload.entry_price
    sl = payload.stop_loss
    t1 = payload.target_1
    t2 = payload.target_2
    explicit_geometry = any(
        value is not None and float(value) > 0
        for value in (payload.stop_loss, payload.target_1, payload.target_2)
    )
    setup_type = payload.setup_type or "MANUAL_CONVICTION"
    thesis = payload.thesis or "Manually selected high-priority setup under close real-time surveillance."
    catalyst = payload.catalyst_summary or "Manual priority selection"
    tape_evidence = payload.tape_evidence or "Manual priority entry"
    curr_px = payload.current_price or entry_px or 0.0

    # Auto-enrich from multi-timeframe analysis if levels or price are missing
    if not entry_px or not sl or not t1 or curr_px <= 0:
        try:
            from services.multi_tf_analysis import get_multi_tf_analysis_service
            analysis_svc = get_multi_tf_analysis_service()
            analysis = await analysis_svc.analyze_symbol(sym)
            c = analysis.get("commentary", {})
            computed_px = float(analysis.get("current_price") or analysis.get("price") or 0.0)
            if computed_px > 0:
                curr_px = curr_px if curr_px > 0 else computed_px

            computed_trig = float(c.get("trigger_price") or 0.0)
            entry_px = entry_px or (computed_trig if computed_trig > 0 else curr_px)

            computed_sl = float(c.get("stop_loss") or 0.0)
            sl = sl or (computed_sl if computed_sl > 0 else (entry_px * 0.985))

            computed_t1 = float(c.get("target_price_1") or 0.0)
            t1 = t1 or (computed_t1 if computed_t1 > 0 else (entry_px * 1.03))

            computed_t2 = float(c.get("target_price_2") or 0.0)
            t2 = t2 or (computed_t2 if computed_t2 > 0 else (entry_px * 1.06))

            setup_type = payload.setup_type or c.get("bias", "MANUAL_CONVICTION")
            thesis = payload.thesis or c.get("actionable_thesis", thesis)
            catalyst = payload.catalyst_summary or c.get("latest_news", catalyst)
            flow = c.get("institutional_flow_detail", {})
            if flow and not payload.tape_evidence:
                tape_evidence = f"IFP: {flow.get('score', 0):+d} [{flow.get('classification', 'Flow')}]"
        except Exception as exc:
            logger.warning(f"Could not auto-enrich manual trade {sym}: {exc}", exc_info=True)
            # Try to get candle from cache before resorting to any fallback
            try:
                from services.multi_tf_analysis import get_multi_tf_analysis_service
                analysis_svc = get_multi_tf_analysis_service()
                candles = analysis_svc.cache.get_candles(sym)
                if candles and candles[-1].close > 0:
                    curr_px = curr_px if curr_px > 0 else float(candles[-1].close)
            except Exception:
                pass
            if curr_px <= 0:
                curr_px = entry_px or 100.0
            entry_px = entry_px or curr_px
            sl = sl or (entry_px * 0.985)
            t1 = t1 or (entry_px * 1.03)
            t2 = t2 or (entry_px * 1.06)

    if not (sl < entry_px < t1 < t2) and not explicit_geometry:
        sl = round(entry_px * 0.985, 2)
        t1 = round(entry_px * 1.03, 2)
        t2 = round(entry_px * 1.06, 2)
    if not (sl < entry_px < t1 < t2):
        raise HTTPException(
            status_code=400,
            detail="BUY geometry requires stop_loss < entry_price < target_1 < target_2.",
        )
    risk = abs(entry_px - sl)
    reward = abs(t1 - entry_px)
    rr = payload.risk_reward_ratio or round(reward / max(0.01, risk), 2)

    # Resolve live RSI and RVOL if available
    rsi_5m = None
    rvol = None
    try:
        from routers.analysis import _rsi_cache
        cached = _rsi_cache.get(sym)
        if cached:
            rsi_5m = cached.get("value")
    except Exception:
        pass

    if rsi_5m is not None and rvol is not None:
        tape_evidence = f"RVOL {rvol:.2f}x | 5m RSI {rsi_5m:.1f}"

    # Calculate quantity via Capital Slot Manager & register directly into Live Watchdog
    from services.capital_slot_manager import get_capital_slot_manager
    from services.live_algo_watchdog import get_algo_live_watchdog

    slot_mgr = get_capital_slot_manager()
    if payload.quantity is not None and int(payload.quantity) > 0:
        qty = int(payload.quantity)
    else:
        qty = slot_mgr.calculate_execution_quantity(entry_px, sl)
        if qty <= 0:
            qty = 1

    watchdog = get_algo_live_watchdog()
    from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
    if sym in watchdog.active_positions or sym in get_algo_live_watchdog_shorts().active_positions:
        raise HTTPException(status_code=400, detail=f"{sym} is already active on a trading desk.")
    if not slot_mgr.can_open_new_trade():
        raise HTTPException(status_code=400, detail="Maximum 3 concurrent trading slots occupied.")

    pos = watchdog.register_position(
        symbol=sym,
        security_id=sec_id or "0",
        entry_price=entry_px,
        quantity=qty,
        order_id="MANUAL_TRACKING",
        action="BUY",
        stop_loss=sl,
        target_1=t1,
        target_2=t2,
        is_external=True,
    )

    trade_dict = {
        "symbol": sym,
        "action": "BUY",
        "setup_type": setup_type,
        "entry_price": round(entry_px, 2),
        "stop_loss": round(sl, 2),
        "target_1": round(t1, 2),
        "target_2": round(t2, 2),
        "risk_reward_ratio": rr,
        "conviction": "HIGH",
        "current_price": round(curr_px, 2),
        "pnl_pct": 0.0,
        "status": "ACTIVE",
        "catalyst_summary": catalyst[:140] if catalyst else "",
        "tape_evidence": tape_evidence[:100] if tape_evidence else "",
        "thesis": thesis,
        "invalidation_rule": f"15m candle close below ₹{sl:.2f}",
        "model_name": "manual_user_input",
        "rsi_5m": rsi_5m,
        "rvol": rvol,
    }

    service.upsert_trade(trade_dict)
    service.add_timeline_event(
        symbol=sym,
        event_type="MANUAL_TRADE_ARMED",
        message=(
            f"🎯 Trade armed for {sym}! Watching live depth and tape. "
            f"We'll broadcast right here when to trail your stop loss or cash out. "
            f"(Advisory mode — you execute orders on Dhan!)"
        ),
        price_at_event=curr_px,
        severity="success",
    )

    return {
        "status": "ok",
        "message": f"{sym} registered directly with Live Watchdog.",
        "trade": trade_dict,
        "position": pos.to_dict(current_ltp=curr_px),
    }


@router.post("/generate")
async def generate_shortlist(
    payload: Optional[GenerateShortlistRequest] = None,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """
    Deprecated: AI Model selection has been removed in favor of direct Live Tracking.
    """
    return {
        "status": "deprecated",
        "message": "AI Shortlist generation has been removed. Track setups directly via Live Tracking.",
        "trades": [],
        "count": 0,
    }


@router.get("")
async def get_shortlisted_trades(
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Returns all currently active shortlisted trades."""
    service = get_ai_shortlist_service()
    trades = service.get_all_trades()
    return {
        "status": "ok",
        "trades": trades,
        "count": len(trades),
    }


@router.get("/timeline")
async def get_shortlist_timeline(
    symbol: Optional[str] = Query(None, description="Filter by stock symbol"),
    limit: int = Query(60, ge=1, le=200, description="Max events to return"),
    after_id: int = Query(0, ge=0, description="Return events after this server sequence ID"),
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Returns chronological IST timeline micro-events."""
    service = get_ai_shortlist_service()
    events = service.get_timeline_events(symbol=symbol, limit=limit, after_id=after_id)
    return {
        "status": "ok",
        "events": events,
        "count": len(events),
        "last_id": events[-1]["id"] if events else after_id,
    }


async def _get_live_ltp(aggregator, resolver, symbol: str) -> float:
    """Resolve a symbol and pull latest LTP from Dhan direct ticker wire feed, CandleAggregator, or REST quote."""
    import time
    import asyncio
    try:
        from services.dhan_instrument_resolver import resolve_canonical_symbol
        canonical = resolve_canonical_symbol(symbol)
        sec_id = resolver.get_security_id(symbol) or resolver.get_security_id(canonical)
        sec_id_str = str(sec_id) if sec_id else None

        # 1. Direct sub-second wire feed from Dhan ticker
        try:
            from services.orchestrator import get_orchestrator
            orch = get_orchestrator()
            if orch and orch.dhan_ticker and hasattr(orch.dhan_ticker, "latest_quotes"):
                quote = (
                    orch.dhan_ticker.latest_quotes.get(symbol)
                    or orch.dhan_ticker.latest_quotes.get(canonical)
                )
                if not quote and sec_id_str:
                    canonical_sym = resolver.get_symbol(sec_id_str)
                    if canonical_sym:
                        quote = orch.dhan_ticker.latest_quotes.get(canonical_sym)
                if quote and float(quote.get("ltp", 0)) > 0:
                    return round(float(quote["ltp"]), 2)
                # Ensure instrument is dynamically subscribed on the live feed
                if sec_id_str and hasattr(orch.dhan_ticker, "subscribe_instrument"):
                    if not hasattr(orch, "_dynamically_subscribed"):
                        orch._dynamically_subscribed = set()
                    target_sym = canonical or symbol
                    if target_sym not in orch._dynamically_subscribed:
                        orch._dynamically_subscribed.add(target_sym)
                        orch._dynamically_subscribed.add(symbol)
                        asyncio.create_task(orch.dhan_ticker.subscribe_instrument(target_sym, sec_id_str))
        except Exception:
            pass

        if sec_id_str:
            token = int(sec_id_str)
            # 2. Prefer the currently-forming 1m bar (most recent tick)
            curr_bar = aggregator.current_1m.get(token)
            if curr_bar and float(curr_bar.get("close", 0)) > 0:
                return round(float(curr_bar["close"]), 2)
            # 3. Fall back to last closed 1m bar
            bars = aggregator.bars_1m.get(token)
            if bars and len(bars) > 0:
                last_close = float(bars[-1].get("close", 0))
                if last_close > 0:
                    return round(last_close, 2)

            # 4. Fall back to Dhan REST marketfeed quote
            try:
                from services.dhan_mcp_client import get_dhan_mcp_client
                mcp = get_dhan_mcp_client()
                quotes = await mcp.get_quotes(sec_id_str)
                q_data = quotes.get(sec_id_str)
                if q_data and float(q_data.get("ltp", 0)) > 0:
                    ltp = round(float(q_data["ltp"]), 2)
                    try:
                        from services.orchestrator import get_orchestrator
                        orch = get_orchestrator()
                        if orch and orch.dhan_ticker and hasattr(orch.dhan_ticker, "latest_quotes"):
                            quote_entry = {
                                "ltp": ltp,
                                "volume": q_data.get("volume", 0),
                                "timestamp": time.time(),
                            }
                            orch.dhan_ticker.latest_quotes[symbol] = quote_entry
                            if canonical:
                                orch.dhan_ticker.latest_quotes[canonical] = quote_entry
                    except Exception:
                        pass
                    return ltp
            except Exception as rest_err:
                logger.debug("REST quote lookup failed for %s: %s", symbol, rest_err)

    except Exception:
        pass
    return 0.0


@router.post("/poll-status")
async def poll_shortlist_surveillance(
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """
    Surveillance ticker endpoint: injects LIVE prices for every stock on the desk
    (from CandleAggregator ticks), overlays Watchdog telemetry for active positions,
    and persists updated prices to SQLite.
    """
    import time
    from services.live_algo_watchdog import get_algo_live_watchdog
    from services.candle_aggregator import get_candle_aggregator
    from services.dhan_instrument_resolver import get_instrument_resolver

    service = get_ai_shortlist_service()
    watchdog = get_algo_live_watchdog()
    aggregator = get_candle_aggregator()

    # Resolve symbol -> security_id for LTP lookup
    resolver = get_instrument_resolver()

    trades = service.get_all_trades()
    active_positions = {pos.symbol: pos for pos in watchdog.active_positions.values()}

    for trade in trades:
        sym = trade.get("symbol", "")
        entry_px = float(trade.get("entry_price", 0) or 0)

        # ── 1. Try Watchdog telemetry first (most accurate for active trades) ──
        if sym in active_positions:
            pos = active_positions[sym]
            # Get real LTP from aggregator
            ltp = await _get_live_ltp(aggregator, resolver, sym)
            pos_dict = pos.to_dict(current_ltp=ltp if ltp > 0 else None)
            trade["status"] = pos.status
            trade["current_price"] = pos_dict.get("current_price", trade.get("current_price", 0))
            trade["pnl_pct"] = pos_dict.get("pnl_pct", 0)
            if pos.hard_stop_loss > 0:
                trade["stop_loss"] = pos.hard_stop_loss
            if pos.target_1 > 0:
                trade["target_1"] = pos.target_1
            if pos.target_2 > 0:
                trade["target_2"] = pos.target_2
        else:
            # ── 2. For non-watchdog stocks: pull LTP from CandleAggregator ──
            ltp = await _get_live_ltp(aggregator, resolver, sym)
            if ltp > 0:
                trade["current_price"] = ltp
                if entry_px > 0:
                    action = str(trade.get("action", "BUY")).upper()
                    if action == "SELL":
                        trade["pnl_pct"] = round(((entry_px - ltp) / max(0.01, entry_px)) * 100, 2)
                    else:
                        trade["pnl_pct"] = round(((ltp - entry_px) / max(0.01, entry_px)) * 100, 2)

        # ── 3. Persist updated price/pnl to SQLite ──
        curr = float(trade.get("current_price", 0) or 0)
        pnl = float(trade.get("pnl_pct", 0) or 0)
        status = str(trade.get("status", "WATCHING"))
        if curr > 0:
            service.update_trade_price_and_status(sym, curr, pnl, status)

    events = service.get_timeline_events(limit=20)
    return {
        "status": "ok",
        "trades": trades,
        "new_events": events,
        "evaluated_at": time.time(),
    }


@router.delete("/{symbol}")
async def delete_shortlisted_trade(
    symbol: str,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Removes a stock from the active shortlist and releases its Watchdog slot."""
    sym = validate_symbol(symbol)
    service = get_ai_shortlist_service()
    success = service.delete_trade(sym)
    if not success:
        raise HTTPException(status_code=404, detail=f"Trade {sym} not found or could not be removed.")
    
    # Free capital slot and deregister position from Watchdog
    from services.live_algo_watchdog import get_algo_live_watchdog
    get_algo_live_watchdog().deregister_position(sym)

    return {"status": "ok", "message": f"Trade {sym} removed from shortlist and Watchdog slot released."}


@router.post("/clear")
async def clear_all_shortlisted_trades(
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Clears all active shortlisted trades and history, releasing all Watchdog slots."""
    service = get_ai_shortlist_service()
    service.clear_all()
    from services.live_algo_watchdog import get_algo_live_watchdog
    watchdog = get_algo_live_watchdog()
    for sym in list(watchdog.active_positions.keys()):
        watchdog.deregister_position(sym)
    return {"status": "ok", "message": "All shortlisted trades cleared and Watchdog slots freed."}
