"""Dhan Historical Data API client used for pre-market candle warm-up."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime, timedelta
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)
_IST = ZoneInfo("Asia/Kolkata")


class DhanHistoricalClient:
    """Fetch completed intraday candles without persisting them locally."""

    def __init__(self, client_id: str, access_token: str, base_url: str = "https://api.dhan.co/v2"):
        if not client_id or not access_token:
            raise ValueError("Dhan historical API requires client_id and access_token")
        self.client_id = client_id
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")

    async def fetch_candles(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        instrument: str = "EQUITY",
        interval_minutes: int,
        to_date: date | None = None,
        count: int = 100,
        client: httpx.AsyncClient | None = None,
    ) -> List[Dict[str, Any]]:
        if interval_minutes not in (1, 5):
            raise ValueError("Only 1-minute and 5-minute warm-up candles are supported")
        if count < 1:
            raise ValueError("count must be positive")

        end = to_date or datetime.now(_IST).date()
        headers = {
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
        }

        owns_client = client is None
        request_client = client or httpx.AsyncClient(timeout=20.0)
        try:
            # Two NSE sessions provide 150 one-minute or 150 five-minute bars.
            # Start with four calendar days and expand only for holidays/weekends.
            lookback_days = 4
            body: Dict[str, Any] | None = None
            while lookback_days <= 30:
                start = end - timedelta(days=lookback_days)
                payload = {
                    "securityId": str(security_id),
                    "exchangeSegment": exchange_segment,
                    "instrument": instrument,
                    "interval": str(interval_minutes),
                    "oi": False,
                    "fromDate": start.isoformat(),
                    "toDate": end.isoformat(),
                }
                response = None
                for attempt in range(4):
                    response = await request_client.post(
                        f"{self.base_url}/charts/intraday",
                        json=payload,
                        headers=headers,
                    )
                    if response.status_code != 429:
                        break
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = max(1.0, float(retry_after)) if retry_after else 2.0 ** attempt
                    except ValueError:
                        delay = 2.0 ** attempt
                    delay += random.uniform(0.0, 0.25)
                    logger.warning(
                        "Dhan historical rate limit: security_id=%s interval=%sm "
                        "attempt=%d/4 retrying_in=%.2fs",
                        security_id, interval_minutes, attempt + 1, delay,
                    )
                    await asyncio.sleep(delay)

                assert response is not None
                if response.is_error:
                    detail = response.text[:500].replace("\n", " ")
                    if response.status_code == 400:
                        raise ValueError(
                            f"Dhan historical request rejected (400) for security_id={security_id}, "
                            f"interval={interval_minutes}m: {detail}"
                        )
                    response.raise_for_status()
                body = response.json()
                if len(body.get("timestamp", [])) >= count:
                    break
                lookback_days *= 2

            if body is None:
                raise ValueError("Dhan historical API returned no candle response")
        finally:
            if owns_client:
                await request_client.aclose()

        rows = self._normalize_response(body)
        rows = [row for row in rows if row.get("start_time") is not None]
        rows.sort(key=lambda row: row["start_time"])
        return rows[-count:]

    @staticmethod
    def _normalize_response(body: Dict[str, Any]) -> List[Dict[str, Any]]:
        required = ("open", "high", "low", "close", "volume", "timestamp")
        if not isinstance(body, dict) or any(key not in body for key in required):
            raise ValueError("Dhan historical response is missing candle arrays")

        arrays = {key: body[key] for key in required}
        size = len(arrays["timestamp"])
        if any(not isinstance(value, list) or len(value) != size for value in arrays.values()):
            raise ValueError("Dhan historical candle arrays have inconsistent lengths")

        rows: List[Dict[str, Any]] = []
        for index in range(size):
            try:
                timestamp = float(arrays["timestamp"][index])
                open_price = float(arrays["open"][index])
                high = float(arrays["high"][index])
                low = float(arrays["low"][index])
                close = float(arrays["close"][index])
                volume = int(arrays["volume"][index])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"Invalid historical candle at index {index}") from exc
            if min(open_price, high, low, close) <= 0 or high < max(open_price, close) or low > min(open_price, close):
                raise ValueError(f"Invalid OHLC values at index {index}")
            rows.append({
                "start_time": timestamp,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": max(0, volume),
                "day_cumulative_volume": max(0, volume),
                "cvd": 0.0,
                "tick_vol": max(0, volume),
            })
        return rows


async def fetch_symbol_warmup(
    client: DhanHistoricalClient,
    *,
    security_id: str,
    exchange_segment: str,
    http_client: httpx.AsyncClient | None = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch windows sequentially so the pre-market job stays within API limits."""
    one_minute = await client.fetch_candles(
        security_id=security_id,
        exchange_segment=exchange_segment,
        interval_minutes=1,
        client=http_client,
    )
    await asyncio.sleep(0.3)
    five_minute = await client.fetch_candles(
        security_id=security_id,
        exchange_segment=exchange_segment,
        interval_minutes=5,
        client=http_client,
    )
    return {"1m": one_minute, "5m": five_minute}


_warmup_status: Dict[str, Any] = {
    "status": "idle",
    "symbols_requested": 0,
    "symbols_ready": 0,
    "current_symbol": "",
    "failures": [],
    "started_at": None,
    "finished_at": None,
}
_warmup_task: Optional[asyncio.Task] = None


def get_warmup_status() -> Dict[str, Any]:
    """Return the current status and progress of the candle warm-up."""
    global _warmup_status
    from .candle_aggregator import get_candle_aggregator
    agg = get_candle_aggregator()
    ready_count = len([k for k, v in getattr(agg, "history_readiness", {}).items() if v.get("ready")])
    status_copy = dict(_warmup_status)
    if ready_count > status_copy.get("symbols_ready", 0):
        status_copy["symbols_ready"] = ready_count
    return status_copy


async def trigger_warmup_in_background(*, clear_existing: bool = True) -> Dict[str, Any]:
    """Launch candle warm-up in the background without blocking the HTTP caller."""
    global _warmup_task, _warmup_status
    if _warmup_status.get("status") == "running" and _warmup_task and not _warmup_task.done():
        return {"status": "running", "message": "Candle warm-up already in progress", **get_warmup_status()}

    _warmup_task = asyncio.create_task(warmup_candle_aggregator(clear_existing=clear_existing))
    return {"status": "started", "message": "Candle warm-up started in background", **get_warmup_status()}


async def warmup_candle_aggregator(*, clear_existing: bool = True) -> Dict[str, Any]:
    """Fetch and hydrate configured symbols for scheduled or manual warm-up."""
    global _warmup_status
    from config.symbols import TRADING_SYMBOLS
    from .candle_aggregator import get_candle_aggregator
    from .dhan_instrument_resolver import get_instrument_resolver
    from .dhan_token_service import get_dhan_token
    import os

    client_id = os.getenv("DHAN_CLIENT_ID") or os.getenv("DHAN_USER_ID")
    access_token = get_dhan_token() or os.getenv("DHAN_ACCESS_TOKEN")
    client = DhanHistoricalClient(
        client_id,
        access_token,
        base_url=os.getenv("DHAN_BASE_URL", "https://api.dhan.co/v2"),
    )
    resolver = get_instrument_resolver()
    resolver.load()
    resolved = resolver.resolve_symbols(list(TRADING_SYMBOLS))
    aggregator = get_candle_aggregator()
    if clear_existing:
        aggregator.clear_history()

    _warmup_status = {
        "status": "running",
        "symbols_requested": len(resolved),
        "symbols_ready": 0,
        "current_symbol": "",
        "failures": [],
        "started_at": datetime.now(_IST).isoformat(),
        "finished_at": None,
    }

    ready = 0
    failures: List[Dict[str, str]] = []
    try:
        async with httpx.AsyncClient(timeout=20.0) as http_client:
            for security_id, symbol in resolved.items():
                _warmup_status["current_symbol"] = symbol
                try:
                    history = await fetch_symbol_warmup(
                        client,
                        security_id=security_id,
                        exchange_segment="NSE_EQ",
                        http_client=http_client,
                    )
                    status = aggregator.hydrate_history(
                        int(security_id), symbol, history["1m"], history["5m"]
                    )
                    if status["ready"]:
                        ready += 1
                        _warmup_status["symbols_ready"] = ready
                except Exception as exc:
                    logger.error("Candle warm-up failed for %s: %s", symbol, exc)
                    failures.append({"symbol": symbol, "error": str(exc)})
                    _warmup_status["failures"] = failures[-10:]
                await asyncio.sleep(0.1)
        _warmup_status["status"] = "completed"
    except Exception as exc:
        logger.error("Candle warm-up loop encountered error: %s", exc)
        _warmup_status["status"] = "failed"
    finally:
        _warmup_status["finished_at"] = datetime.now(_IST).isoformat()
        _warmup_status["symbols_ready"] = ready
        _warmup_status["current_symbol"] = ""

    return {
        "status": _warmup_status["status"],
        "state": "READY" if ready == len(resolved) and not failures else "PARTIAL",
        "symbols_requested": len(resolved),
        "symbols_ready": ready,
        "failures": failures,
    }
