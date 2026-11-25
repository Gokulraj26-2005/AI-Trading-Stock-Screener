"""
Shortlisted Trades & Surveillance Router for SHORTS (VIA-Agent7)
================================================================
Routes:
-------
GET    /api/shortlist-shorts               - Returns all currently active shortlisted short trades
GET    /api/shortlist-shorts/timeline      - Returns chronological IST timeline micro-events for shorts
POST   /api/shortlist-shorts/add           - Manually adds a short setup to the Shorts Desk
POST   /api/shortlist-shorts/poll-status   - Evaluates real-time price surveillance on active short trades
DELETE /api/shortlist-shorts/{symbol}      - Removes a short trade from active surveillance
POST   /api/shortlist-shorts/clear         - Clears all active shortlisted short trades and history
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth_helpers import verify_token, validate_symbol
from services.ai_shortlist_service_shorts import get_ai_shortlist_service_shorts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/shortlist-shorts", tags=["ai-shortlist-shorts"])


class AddManualShortTradeRequest(BaseModel):
    symbol: str = Field(..., description="Stock symbol, e.g. RELIANCE")
    action: str = Field("SELL", description="SELL for short positions")
    current_price: Optional[float] = Field(None, description="Current market price from chart analysis")
    entry_price: Optional[float] = Field(None, description="Custom entry/trigger price")
    stop_loss: Optional[float] = Field(None, description="Custom stop loss (above entry)")
    target_1: Optional[float] = Field(None, description="Custom target 1 (below entry)")
    target_2: Optional[float] = Field(None, description="Custom target 2 (below entry)")
    quantity: Optional[int] = Field(None, description="Explicit position quantity")
    setup_type: Optional[str] = Field("BREAKDOWN", description="Custom setup type")
    thesis: Optional[str] = Field(None, description="Custom thesis")
    catalyst_summary: Optional[str] = Field(None, description="Catalyst summary")
    tape_evidence: Optional[str] = Field(None, description="Tape evidence")
    risk_reward_ratio: Optional[float] = Field(None, description="Risk-to-reward ratio")


@router.post("/add")
async def add_manual_short_trade(
    payload: AddManualShortTradeRequest,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """
    Manually adds a stock directly to the Shorts Live Watchdog for immediate risk surveillance.
    """
    raw_sym = validate_symbol(payload.symbol)
    service = get_ai_shortlist_service_shorts()

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
            detail=f"Stock symbol '{raw_sym}' could not be resolved on NSE. Please verify the symbol (e.g. GALLANTT instead of GALLANT) and try again.",
        )

    sym = raw_sym if (raw_sym in resolver._nse_eq_map or raw_sym in resolver._index_map) else (resolver.get_symbol(sec_id) or canonical or raw_sym)

    entry_px = payload.entry_price
    sl = payload.stop_loss
    t1 = payload.target_1
    t2 = payload.target_2
    setup_type = payload.setup_type or "MANUAL_SHORT_CONVICTION"
    thesis = payload.thesis or "Manually selected high-priority short setup under close real-time surveillance."
    catalyst = payload.catalyst_summary or "Manual short priority selection"
    from services.indicator_cache import get_cached_rsi, get_cached_real_rsi
    _init_rsi = get_cached_real_rsi(sym) or get_cached_rsi(sym)
    if _init_rsi is not None:
        tape_evidence = payload.tape_evidence or f"RVOL 1.00x | 5m RSI {_init_rsi:.1f}"
    else:
        tape_evidence = payload.tape_evidence or "RVOL 1.00x | 5m RSI 50.0"
    curr_px = payload.current_price or entry_px or 0.0

    if not entry_px or not sl or not t1 or curr_px <= 0:
        try:
            from services.multi_tf_analysis import get_multi_tf_analysis_service
            analysis_svc = get_multi_tf_analysis_service()
            analysis = await analysis_svc.analyze_symbol(sym)
            computed_px = float(analysis.get("current_price") or analysis.get("price") or 0.0)
            if computed_px > 0:
                curr_px = computed_px
                entry_px = entry_px or computed_px
                # For shorts, default SL is 1.0% above entry
                sl = sl or round(entry_px * 1.01, 2)
                t1 = t1 or round(entry_px * 0.97, 2)
                t2 = t2 or round(entry_px * 0.94, 2)
        except Exception as exc:
            logger.debug("Failed auto-enriching short levels for %s: %s", sym, exc)

    entry_px = float(entry_px or curr_px or 100.0)

    # Robust SELL geometry sanitization (target_2 < target_1 < entry_price < stop_loss)
    # 1. Stop loss MUST be above entry price for a short position
    if not sl or float(sl) <= entry_px:
        sl = round(entry_px * 1.01, 2)
    else:
        sl = float(sl)

    # 2. Target 1 MUST be below entry price for a short position
    if not t1 or float(t1) >= entry_px:
        t1 = round(entry_px * 0.97, 2)
    else:
        t1 = float(t1)

    # 3. Target 2 MUST be below Target 1 for a short position
    if not t2 or float(t2) >= t1:
        t2 = round(entry_px * 0.94, 2)
    else:
        t2 = float(t2)

    if payload.action.upper() != "SELL":
        raise HTTPException(status_code=400, detail="Short manual trades must use SELL action.")

    if not (t2 < t1 < entry_px < sl):
        sl = round(entry_px * 1.01, 2)
        t1 = round(entry_px * 0.97, 2)
        t2 = round(entry_px * 0.94, 2)

    if not (t2 < t1 < entry_px < sl):
        raise HTTPException(
            status_code=400,
            detail=f"SELL geometry requires target_2 ({t2}) < target_1 ({t1}) < entry_price ({entry_px}) < stop_loss ({sl}).",
        )

    from services.live_algo_watchdog import get_algo_live_watchdog
    from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
    long_watchdog = get_algo_live_watchdog()
    watchdog_shorts = get_algo_live_watchdog_shorts()
    if sym in long_watchdog.active_positions or sym in watchdog_shorts.active_positions:
        raise HTTPException(status_code=400, detail=f"{sym} is already active on a trading desk.")

    from services.capital_slot_manager import get_capital_slot_manager
    slot_mgr = get_capital_slot_manager()
    if not slot_mgr.can_open_new_trade():
        raise HTTPException(status_code=400, detail="Maximum 3 concurrent trading slots occupied.")
    
    if payload.quantity is not None and int(payload.quantity) > 0:
        qty = int(payload.quantity)
    else:
        qty = slot_mgr.calculate_execution_quantity(entry_px, sl)
        if qty <= 0:
            qty = 1

    pos = watchdog_shorts.register_position(
        symbol=sym,
        security_id=str(sec_id or "0"),
        entry_price=entry_px,
        quantity=qty,
        order_id="MANUAL_TRACKING",
        action="SELL",
        stop_loss=sl,
        target_1=t1,
        target_2=t2,
        is_external=True,
    )
    risk = max(0.01, sl - entry_px)
    reward = max(0.01, entry_px - t1)
    rr = round(reward / risk, 2) if risk > 0 else 2.0

    trade_dict = {
        "symbol": sym,
        "action": "SELL",
        "setup_type": setup_type,
        "entry_price": entry_px,
        "stop_loss": sl,
        "target_1": t1,
        "target_2": t2,
        "risk_reward_ratio": payload.risk_reward_ratio or rr,
        "conviction": "HIGH",
        "status": "ACTIVE",
        "current_price": curr_px or entry_px,
        "pnl_pct": 0.0,
        "catalyst_summary": catalyst,
        "tape_evidence": tape_evidence,
        "thesis": thesis,
        "invalidation_rule": f"Rises above SL of ₹{sl:.2f}",
        "model_name": "Manual Dispatch",
        "rsi_5m": _init_rsi,
    }

    service.upsert_trade(trade_dict)

    _sec_id_int = int(sec_id) if sec_id and str(sec_id).isdigit() else 0
    if _sec_id_int > 0:
        import asyncio
        async def _hydrate_rsi_task(_sym=sym, _sid=_sec_id_int, _px=curr_px or entry_px):
            try:
                from routers.analysis import _fetch_single_symbol_5m
                rsi = await _fetch_single_symbol_5m(_sym, _sid)
                if rsi is not None:
                    _svc = get_ai_shortlist_service_shorts()
                    _svc.update_trade_price_and_status(
                        _sym,
                        current_price=_px,
                        pnl_pct=0.0,
                        status="ACTIVE",
                        rsi_5m=rsi,
                        tape_evidence=f"RVOL 1.00x | 5m RSI {rsi:.1f}",
                    )
                    from services.indicator_cache import set_cached_rsi
                    set_cached_rsi(_sym, rsi, is_real=True)
            except Exception as _e:
                logger.debug("RSI hydration failed for %s: %s", _sym, _e)

        asyncio.create_task(_hydrate_rsi_task())

    service.add_timeline_event(
        symbol=sym,
        event_type="MANUAL_TRADE_ARMED",
        message=(
            f"🎯 Short trade armed for {sym}! Watching live depth and tape. "
            f"We'll broadcast right here when to lower your stop loss or cover for profit. "
            f"(Advisory mode — you execute orders on Dhan!)"
        ),
        price_at_event=curr_px or entry_px,
        severity="success",
    )

    return {
        "status": "ok",
        "message": f"{sym} registered directly with Shorts Live Watchdog.",
        "trade": trade_dict,
        "position": pos.to_dict(current_ltp=curr_px or entry_px),
    }


@router.get("")
async def get_shortlist_shorts(user_id: str = Depends(verify_token)) -> Dict[str, Any]:
    """Returns all currently active shortlisted short trades."""
    service = get_ai_shortlist_service_shorts()
    trades = service.get_all_trades()
    return {
        "status": "ok",
        "trades": trades,
        "count": len(trades),
    }


@router.get("/timeline")
async def get_shortlist_shorts_timeline(
    symbol: Optional[str] = Query(None, description="Filter by stock symbol"),
    limit: int = Query(60, ge=1, le=200, description="Max events to return"),
    after_id: int = Query(0, ge=0, description="Return events after this server sequence ID"),
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Returns chronological IST timeline micro-events for shorts desk."""
    service = get_ai_shortlist_service_shorts()
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
async def poll_shortlist_shorts_surveillance(
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """
    Surveillance ticker endpoint for Shorts Desk: injects live prices,
    overlays WatchdogShorts telemetry for active positions, and persists updated prices to SQLite.
    """
    import time
    from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
    from services.candle_aggregator import get_candle_aggregator
    from services.dhan_instrument_resolver import get_instrument_resolver

    service = get_ai_shortlist_service_shorts()
    watchdog = get_algo_live_watchdog_shorts()
    aggregator = get_candle_aggregator()
    resolver = get_instrument_resolver()

    trades = service.get_all_trades()
    active_positions = {pos.symbol: pos for pos in watchdog.active_positions.values()}

    for trade in trades:
        sym = trade.get("symbol", "")
        entry_px = float(trade.get("entry_price", 0) or 0)

        # Clean up stale/broken alias positions (e.g. GALLANT with sec_id 0)
        if "GALLANT" in watchdog.active_positions and sym == "GALLANTT":
            watchdog.deregister_position("GALLANT")
            active_positions.pop("GALLANT", None)

        # 1. Try WatchdogShorts telemetry first
        if sym in active_positions:
            pos = active_positions[sym]
            if (not pos.security_id or pos.security_id == "0") and sym:
                sid = resolver.get_security_id(sym)
                if sid:
                    pos.security_id = str(sid)
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
            # Auto-restore active manual trade to watchdog
            if trade.get("status") == "ACTIVE":
                sec_id = resolver.get_security_id(sym) or "0"
                qty = int(trade.get("quantity") or 1)
                if qty <= 1:
                    qty = max(1, int(60000 / max(1.0, entry_px)))
                pos = watchdog.register_position(
                    symbol=sym,
                    security_id=str(sec_id),
                    entry_price=entry_px,
                    quantity=qty,
                    order_id="MANUAL_TRACKING",
                    action="SELL",
                    stop_loss=float(trade.get("stop_loss", entry_px * 1.01)),
                    target_1=float(trade.get("target_1", entry_px * 0.97)),
                    target_2=float(trade.get("target_2", entry_px * 0.94)),
                    is_external=True,
                )
                active_positions[sym] = pos
                ltp = await _get_live_ltp(aggregator, resolver, sym)
                pos_dict = pos.to_dict(current_ltp=ltp if ltp > 0 else None)
                trade["current_price"] = pos_dict.get("current_price", trade.get("current_price", 0))
                trade["pnl_pct"] = pos_dict.get("pnl_pct", 0)
            else:
                # 2. For non-watchdog short stocks: pull LTP and calculate short P&L
                ltp = await _get_live_ltp(aggregator, resolver, sym)
                if ltp > 0:
                    trade["current_price"] = ltp
                    if entry_px > 0:
                        trade["pnl_pct"] = round(((entry_px - ltp) / max(0.01, entry_px)) * 100, 2)

        # Check if real RSI is available from shared indicator cache or aggregator
        import re
        from services.indicator_cache import get_cached_real_rsi, get_cached_rsi, set_cached_rsi
        live_rsi = get_cached_real_rsi(sym)
        if live_rsi is None:
            live_rsi = get_cached_rsi(sym)

        sec_id_val = resolver.get_security_id(sym)
        sec_id_num = int(sec_id_val) if sec_id_val and str(sec_id_val).isdigit() else 0

        # If still None, check if CandleAggregator has >= 15 5m bars
        if live_rsi is None and sec_id_num > 0 and aggregator:
            bars_5m = list(aggregator.bars_5m.get(sec_id_num, ()))
            if len(bars_5m) >= 15:
                from services.indicator_cache import _compute_rsi_wilder
                closes_5m = [float(b["close"]) for b in bars_5m if float(b.get("close", 0)) > 0]
                calc_rsi = _compute_rsi_wilder(closes_5m, period=14)
                if calc_rsi is not None:
                    live_rsi = calc_rsi
                    set_cached_rsi(sym, calc_rsi, is_real=True)

        # Trigger non-blocking 5m fetch if RSI is missing and not already scheduled
        if live_rsi is None and sec_id_num > 0:
            try:
                from routers.analysis import _schedule_5m_fetch
                _schedule_5m_fetch(sym, sec_id_num)
            except Exception:
                pass

        # If live_rsi is available, enrich trade dict and update tape evidence
        new_tape = None
        if live_rsi is not None:
            trade["rsi_5m"] = live_rsi
            curr_tape = str(trade.get("tape_evidence") or "")
            if "5m RSI" not in curr_tape or "None" in curr_tape:
                new_tape = f"RVOL 1.00x | 5m RSI {live_rsi:.1f}"
                trade["tape_evidence"] = new_tape
            elif re.search(r"5m\s*RSI\s*[:\s]?\s*[\d.]+", curr_tape, re.IGNORECASE):
                new_tape = re.sub(r"5m\s*RSI\s*[:\s]?\s*[\d.]+", f"5m RSI {live_rsi:.1f}", curr_tape, flags=re.IGNORECASE)
                trade["tape_evidence"] = new_tape

        # 3. Persist updated price/pnl to SQLite
        curr = float(trade.get("current_price", 0) or 0)
        pnl = float(trade.get("pnl_pct", 0) or 0)
        status = str(trade.get("status", "WATCHING"))
        if curr > 0:
            service.update_trade_price_and_status(
                sym,
                curr,
                pnl,
                status,
                tape_evidence=new_tape if new_tape else None,
                rsi_5m=live_rsi if live_rsi is not None else None,
            )

    events = service.get_timeline_events(limit=20)
    return {
        "status": "ok",
        "trades": trades,
        "new_events": events,
        "evaluated_at": time.time(),
    }


@router.delete("/{symbol}")
async def delete_shortlisted_short_trade(
    symbol: str,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Removes a stock from the active shorts shortlist and releases its Watchdog slot."""
    sym = validate_symbol(symbol)
    service = get_ai_shortlist_service_shorts()
    success = service.delete_trade(sym)
    if not success:
        raise HTTPException(status_code=404, detail=f"Short trade {sym} not found or could not be removed.")
    
    # Release capital slot and deregister position from WatchdogShorts
    from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
    get_algo_live_watchdog_shorts().deregister_position(sym)

    return {"status": "ok", "message": f"Short trade {sym} removed from Shorts Desk and Watchdog slot released."}


@router.post("/clear")
async def clear_all_shortlisted_short_trades(
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Clears all active shortlisted short trades and history, releasing all Watchdog slots."""
    service = get_ai_shortlist_service_shorts()
    service.clear_all()
    from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
    watchdog_shorts = get_algo_live_watchdog_shorts()
    for sym in list(watchdog_shorts.active_positions.keys()):
        watchdog_shorts.deregister_position(sym)
    return {"status": "ok", "message": "All shortlisted short trades cleared and Watchdog slots freed."}
