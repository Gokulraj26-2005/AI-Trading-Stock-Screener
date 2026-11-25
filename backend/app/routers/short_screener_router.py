"""
Live Short Screener Router
==========================
Exposes REST and WebSocket endpoints for the Live Stock Screener for Short Candidates:
- GET  /api/short-screener/power        - Master ON/OFF power status
- POST /api/short-screener/power       - Toggle power ON/OFF
- GET  /api/short-screener/stocks       - Retrieve live sorted stock list
- GET  /api/short-screener/scan         - Alias for stocks scan
- GET  /api/short-screener/news/{symbol} - 24-hour FinBERT news catalysts & scored headlines
- WS   /ws/short-screener               - Real-time streaming WebSocket updates
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from auth_helpers import verify_token
from services.short_screener_engine import get_short_screener_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/short-screener", tags=["short-screener"])
ws_router = APIRouter(tags=["short-screener-ws"])


class PowerRequest(BaseModel):
    active: bool


@router.get("/power")
async def get_screener_power(
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Get the current power state of the Live Short Screener."""
    engine = get_short_screener_engine()
    return {"is_active": engine.is_active}


@router.post("/power")
async def set_screener_power(
    payload: Optional[PowerRequest] = None,
    active: Optional[bool] = Query(None),
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Toggle Master Power switch for the Live Short Screener."""
    engine = get_short_screener_engine()
    target_state = payload.active if payload is not None else (active if active is not None else False)
    new_state = await engine.set_power(target_state)
    return {"status": "success", "is_active": new_state}


@router.get("/stocks")
async def get_screener_stocks(
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Retrieve the live sorted stocks list for short candidates."""
    engine = get_short_screener_engine()
    return engine.get_scan_payload()


@router.get("/scan")
async def get_screener_scan(
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """Alias for /stocks for backwards compatibility."""
    engine = get_short_screener_engine()
    return engine.get_scan_payload()


@router.get("/news/{symbol}")
async def get_screener_stock_news(
    symbol: str,
    user_id: str = Depends(verify_token),
) -> Dict[str, Any]:
    """
    Retrieve 24-hour FinBERT news catalysts, scored headlines, and key details
    for a given stock symbol in the Live Short Screener.
    """
    sym = symbol.strip().upper()
    engine = get_short_screener_engine()
    st = engine.states.get(sym)

    now_iso = datetime.now(timezone.utc).isoformat()
    cached = None
    try:
        from services.finbert_sentiment import FinBERTSentimentEngine
        finbert = FinBERTSentimentEngine.get_instance()
        cached = finbert.get_cached_analysis(sym)

        if cached is None or not cached.get("headlines"):
            from services.multi_tf_analysis import _fetch_stock_news_24h_sync
            _, articles = await asyncio.to_thread(_fetch_stock_news_24h_sync, sym)
            if articles:
                cached = await asyncio.to_thread(
                    finbert.analyze_articles,
                    sym,
                    articles,
                    st.ifp_score if st else None,
                )
    except Exception as exc:
        logger.warning("Error fetching 24h news analysis for %s: %s", sym, exc)

    if not cached or not cached.get("headlines"):
        # Search completed, but 0 relevant news in 24h envelope
        if st:
            st.news_catalyst_score = 0
            st.is_news_evaluated = False
            st.news_article_count = 0
            st.news_status = "NO_NEWS"
            st.news_catalyst_label = "NO_NEWS"

        return {
            "symbol": sym,
            "sentiment_score": 0,
            "sentiment_label": "NO_NEWS",
            "is_evaluated": True,
            "status": "NO_NEWS",
            "probabilities": {"positive": 0.33, "neutral": 0.34, "negative": 0.33},
            "article_count": 0,
            "headlines": [],
            "summary": f"Search completed: No news articles published for {sym} in the past 24 hours.",
            "narrative_alignment": {
                "state": "UNALIGNED",
                "badge": "No Catalyst Pressure",
                "type": "neutral",
                "detail": "Absence of company news leaves price discovery primarily driven by technical structure.",
            },
            "scanned_at": now_iso,
            "lookback_window": "24h",
        }

    # Format headlines with human-readable time_ago ("1h ago", "2h ago", etc.)
    headlines_formatted = []
    for h in cached.get("headlines", []):
        hrs = int(h.get("hours_ago", 1))
        time_str = f"{hrs}h ago" if hrs >= 1 else "Just now"
        sentiment_type = h.get("sentiment", "NEUTRAL")
        item_score = int(h.get("score", 0))

        # Important details snippet
        detail_snippet = h.get("detail")
        if not detail_snippet:
            prob_neg = int(round(float(h.get("prob_negative", 0.0)) * 100))
            prob_pos = int(round(float(h.get("prob_positive", 0.0)) * 100))
            prob_neu = int(round(float(h.get("prob_neutral", 0.0)) * 100))
            if sentiment_type == "BEARISH":
                detail_snippet = f"Negative FinBERT conviction ({prob_neg}%). Identified bearish sentiment drag."
            elif sentiment_type == "BULLISH":
                detail_snippet = f"Positive FinBERT conviction ({prob_pos}%). Identified bullish catalyst tailwind."
            else:
                detail_snippet = f"Neutral FinBERT reading ({prob_neu}%). Routine informational update."

        headlines_formatted.append({
            "title": h.get("title", ""),
            "source": h.get("source", "News"),
            "hours_ago": hrs,
            "time_ago": time_str,
            "sentiment": sentiment_type,
            "score": item_score,
            "is_regulatory": bool(h.get("is_regulatory", False)),
            "prob_positive": h.get("prob_positive", 0.0),
            "prob_negative": h.get("prob_negative", 0.0),
            "prob_neutral": h.get("prob_neutral", 0.0),
            "detail": detail_snippet,
        })

    score = int(cached.get("sentiment_score", 0))
    label = str(cached.get("sentiment_label", "NEUTRAL"))
    if st:
        st.news_catalyst_score = score
        st.is_news_evaluated = len(headlines_formatted) > 0
        st.news_article_count = len(headlines_formatted)
        if st.is_news_evaluated:
            st.news_status = "EVALUATED"
            st.news_catalyst_label = "BEARISH_DRAG" if score <= -20 else ("BULLISH_SURGE" if score >= 20 else "NEUTRAL")
        else:
            st.news_status = "NO_NEWS"
            st.news_catalyst_label = "NO_NEWS"

    return {
        "symbol": sym,
        "sentiment_score": score,
        "sentiment_label": label,
        "is_evaluated": len(headlines_formatted) > 0,
        "status": "EVALUATED" if len(headlines_formatted) > 0 else "NO_NEWS",
        "probabilities": cached.get("probabilities", {"positive": 0.33, "neutral": 0.34, "negative": 0.33}),
        "article_count": len(headlines_formatted),
        "headlines": headlines_formatted,
        "summary": cached.get("summary", ""),
        "narrative_alignment": cached.get("narrative_alignment", {
            "state": "UNALIGNED",
            "badge": "Flow Baseline Pending",
            "type": "neutral",
            "detail": "Institutional tape score pending for narrative cross-check.",
        }),
        "scanned_at": cached.get("scanned_at", now_iso),
        "lookback_window": "24h",
    }


@ws_router.websocket("/ws/short-screener")
async def websocket_short_screener(websocket: WebSocket) -> None:
    """Streams real-time updates of the live short screener table."""
    await websocket.accept()
    engine = get_short_screener_engine()
    queue: asyncio.Queue = asyncio.Queue(maxsize=30)
    engine.register_subscriber(queue)

    try:
        # Immediately push current state upon connection
        initial_payload = engine.get_scan_payload()
        await websocket.send_text(json.dumps(initial_payload, default=str))

        while True:
            receive_task = asyncio.create_task(websocket.receive_text())
            queue_task = asyncio.create_task(queue.get())

            done, pending = await asyncio.wait(
                [receive_task, queue_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            if queue_task in done:
                payload = queue_task.result()
                await websocket.send_text(json.dumps(payload, default=str))

            if receive_task in done:
                msg = receive_task.result()
                try:
                    data = json.loads(msg)
                    if data.get("action") == "ping":
                        await websocket.send_text(json.dumps({"type": "pong", "time": data.get("time")}))
                except Exception:
                    pass

    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as exc:
        logger.warning("LiveShortScreener WebSocket error: %s", exc)
    finally:
        engine.unregister_subscriber(queue)
        try:
            await websocket.close()
        except Exception:
            pass
