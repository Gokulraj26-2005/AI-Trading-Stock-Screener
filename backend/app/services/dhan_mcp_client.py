"""
Dhan Official MCP & Gateway Client
==================================
Connects to DhanHQ's official hosted Model Context Protocol (MCP) server
(https://mcp.dhan.co/mcp) and DhanHQ v2 endpoints.

Provides atomic, typed, zero-GUI tools for:
- Placing MIS (Intraday) orders with exact capital sizing
- Modifying trailing stop losses
- Immediate emergency market exits
- Retrieving order status & 20-level orderbook depth
- Deterministic simulation / paper-trade fallback when broker credentials are off-market
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional
import httpx

from config.trading_config import CAPITAL_CONFIG
from logger import logger
from services.dhan_token_service import get_dhan_token
from services.dhan_instrument_resolver import get_instrument_resolver, resolve_canonical_symbol


def normalize_order_status(value: Any) -> str:
    """Return a safe, broker-independent lifecycle status."""
    status = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if status in {"TRADED", "FILLED", "COMPLETE", "COMPLETED", "EXECUTED"}:
        return "FILLED"
    if status in {"TRANSIT", "IN_TRANSIT", "PUT_ORDER_REQ_RECEIVED"}:
        return "TRANSIT"
    if status in {"ACCEPTED", "ACKNOWLEDGED", "OPEN", "PENDING", "VALIDATION_PENDING"}:
        return "ACCEPTED"
    if status in {"PART_TRADED", "PARTIALLY_FILLED", "PARTIAL_FILLED", "PARTIAL"}:
        return "PARTIAL"
    if status in {"REJECTED", "FAILED", "ERROR"}:
        return "REJECTED"
    if status in {"CLOSED", "SUCCESS", "CANCELLED", "CANCELED", "EXPIRED"}:
        return "CANCELLED"
    return "UNKNOWN"


class DhanMCPClient:
    def __init__(self):
        self.client_id = os.getenv("DHAN_CLIENT_ID", "")
        self.mcp_url = CAPITAL_CONFIG.DHAN_MCP_ENDPOINT
        self.base_api_url = CAPITAL_CONFIG.DHAN_BASE_API_URL
        self._mock_orders: Dict[str, Dict[str, Any]] = {}
        self._leverage_cache: Dict[str, Dict[str, Any]] = {}

    def _get_token(self) -> str:
        return get_dhan_token() or os.getenv("DHAN_ACCESS_TOKEN", "")

    def _get_headers(self) -> Dict[str, str]:
        token = self._get_token()
        return {
            "client-id": self.client_id,
            "access-token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def is_configured(self) -> bool:
        return bool(self.client_id and self._get_token())

    async def get_security_leverage(self, security_id: str, price: float) -> float:
        """
        Queries Dhan's official Margin Calculator API (/v2/margincalculator) to determine
        the exact broker MIS leverage for this scrip (e.g. 5.0x, 4.46x).
        Cached for 30 minutes to reduce network overhead.
        """
        default_lev = float(getattr(CAPITAL_CONFIG, "MIS_LEVERAGE", 5.0))
        if not security_id or price <= 0 or not self.is_configured():
            return default_lev

        cached = self._leverage_cache.get(str(security_id))
        now = time.time()
        if cached and (now - cached.get("time", 0)) < 1800:
            return cached.get("leverage", default_lev)

        try:
            test_qty = 10
            payload = {
                "dhanClientId": self.client_id,
                "exchangeSegment": "NSE_EQ",
                "transactionType": "BUY",
                "quantity": test_qty,
                "productType": "INTRADAY",
                "securityId": str(security_id),
                "price": float(price),
            }
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(
                    f"{self.base_api_url}/margincalculator",
                    json=payload,
                    headers=self._get_headers(),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tot_margin = float(data.get("totalMargin", 0.0))
                    if tot_margin > 0:
                        total_trade_value = test_qty * price
                        computed_leverage = round(total_trade_value / tot_margin, 2)
                        lev = max(1.0, min(5.0, computed_leverage))
                        self._leverage_cache[str(security_id)] = {"leverage": lev, "time": now}
                        logger.info("Dhan Margin Calculator for sec_id=%s: Margin=₹%.2f, Dynamic Leverage=%.2fx", security_id, tot_margin, lev)
                        return lev
                    lev_str = data.get("leverage", "")
                    if lev_str and "X" in lev_str.upper():
                        parsed = float(lev_str.upper().replace("X", "").strip())
                        lev = max(1.0, min(5.0, parsed))
                        self._leverage_cache[str(security_id)] = {"leverage": lev, "time": now}
                        return lev
        except Exception as exc:
            logger.debug("Dhan margin calculation exception for %s: %s", security_id, exc)

        return default_lev

    async def place_mis_order(
        self,
        security_id: str,
        transaction_type: str,  # "BUY" or "SELL"
        quantity: int,
        price: float = 0.0,
        order_type: str = "MARKET",
        symbol_hint: str = "",
    ) -> Dict[str, Any]:
        """
        Places an Intraday (MIS) order via Dhan MCP / API.
        Returns: { "status": "SUCCESS" | "FAILED", "order_id": str, "fill_price": float, "details": dict }
        """
        if quantity <= 0:
            return {"status": "FAILED", "reason": "Quantity must be > 0"}

        correlation_id = f"VIA_{security_id}_{int(time.time() * 1000)}"

        # If credentials configured, dispatch real Dhan order
        if self.is_configured():
            payload = {
                "dhanClientId": self.client_id,
                "correlationId": correlation_id,
                "transactionType": transaction_type.upper(),
                "exchangeSegment": "NSE_EQ",
                "productType": "INTRADAY",  # MIS product code
                "orderType": order_type.upper(),
                "validity": "DAY",
                "securityId": str(security_id),
                "quantity": int(quantity),
                "price": float(price) if order_type.upper() == "LIMIT" else 0.0,
                "triggerPrice": 0.0,
                "afterMarketOrder": False,
            }

            try:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    resp = await client.post(
                        f"{self.base_api_url}/orders",
                        json=payload,
                        headers=self._get_headers(),
                    )
                    data = resp.json()
                    status_code = resp.status_code

                    if status_code in (200, 201):
                        order_id = str(data.get("orderId", ""))
                        raw_order_status = str(data.get("orderStatus", "TRANSIT"))
                        lifecycle_status = normalize_order_status(raw_order_status)
                        logger.info(
                            "Dhan MCP Order Placed: ID=%s, Sym=%s, Status=%s, Qty=%d",
                            order_id, symbol_hint or security_id, lifecycle_status, quantity
                        )
                        return {
                            "status": "SUCCESS",
                            "order_id": order_id,
                            "order_status": raw_order_status.strip().upper(),
                            "lifecycle_status": lifecycle_status,
                            "data": data,
                        }
                    else:
                        err_code = data.get("errorCode", "")
                        err_msg = data.get("errorMessage") or data.get("message", f"HTTP {status_code} Error")
                        full_err = f"{err_code}: {err_msg}" if err_code else err_msg
                        logger.error("Dhan MCP order rejected by broker: %s (Status: %d, Response: %s)", full_err, status_code, data)
                        return {"status": "FAILED", "reason": full_err, "raw": data}
            except Exception as exc:
                logger.error("Dhan MCP network/call error on place_mis_order: %s", exc)
                return {"status": "FAILED", "reason": str(exc)}

        # Deterministic Institutional Paper-Trade Fallback (Zero Risk Sandbox)
        sim_order_id = f"SIM_{correlation_id}"
        self._mock_orders[sim_order_id] = {
            "order_id": sim_order_id,
            "security_id": security_id,
            "symbol": symbol_hint,
            "transaction_type": transaction_type.upper(),
            "quantity": quantity,
            "price": price,
            "status": "TRADED",
            "timestamp": time.time(),
        }
        logger.info(
            "[PAPER MODE] Dhan MCP MIS Order simulated: ID=%s, Sym=%s x %d shares",
            sim_order_id, symbol_hint or security_id, quantity
        )
        return {
            "status": "SUCCESS",
            "order_id": sim_order_id,
            "order_status": "TRADED",
            "lifecycle_status": "FILLED",
            "paper_mode": True,
        }


    async def modify_stop_loss(
        self,
        order_id: str,
        new_trigger_price: float,
        quantity: int,
    ) -> bool:
        """
        Modifies a pending stop-loss order via Dhan MCP / API.
        """
        new_sl = round(new_trigger_price, 2)
        if order_id.startswith("SIM_"):
            if order_id in self._mock_orders:
                self._mock_orders[order_id]["trigger_price"] = new_sl
            logger.info("[PAPER MODE] Modified SL for %s -> ₹%.2f", order_id, new_sl)
            return True

        if not self.is_configured():
            return False

        payload = {
            "orderId": order_id,
            "orderType": "STOP_LOSS_MARKET",
            "triggerPrice": new_sl,
            "quantity": int(quantity),
            "validity": "DAY",
        }

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.put(
                    f"{self.base_api_url}/orders/{order_id}",
                    json=payload,
                    headers=self._get_headers(),
                )
                if resp.status_code in (200, 201):
                    logger.info("Dhan MCP Modified SL successfully for %s -> ₹%.2f", order_id, new_sl)
                    return True
                logger.warning("Dhan MCP modify SL failed: %s", resp.text)
                return False
        except Exception as exc:
            logger.error("Dhan MCP modify_stop_loss exception: %s", exc)
            return False

    async def cancel_order(self, order_id: str) -> bool:
        """Cancels a pending order."""
        if order_id.startswith("SIM_"):
            self._mock_orders.pop(order_id, None)
            return True

        if not self.is_configured():
            return False

    async def get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Fetch one broker order without inventing a fill on transport failure."""
        if not order_id:
            return {"status": "FAILED", "reason": "Missing order id"}
        if order_id.startswith("SIM_"):
            order = self._mock_orders.get(order_id)
            if not order:
                return {"status": "FAILED", "reason": "Unknown simulated order"}
            raw = order.get("status", "UNKNOWN")
            return {
                "status": "SUCCESS",
                "order_id": order_id,
                "order_status": raw,
                "lifecycle_status": normalize_order_status(raw),
                "filled_quantity": int(order.get("filled_quantity", order.get("quantity", 0))),
                "fill_price": order.get("fill_price", order.get("price")),
                "data": order,
            }
        if not self.is_configured():
            return {"status": "FAILED", "reason": "Broker is not configured"}
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    f"{self.base_api_url}/orders/{order_id}",
                    headers=self._get_headers(),
                )
                data = resp.json()
                if resp.status_code != 200 or not isinstance(data, dict):
                    return {"status": "FAILED", "reason": f"Order status HTTP {resp.status_code}", "raw": data}
                raw = data.get("orderStatus") or data.get("status")
                filled = data.get("filledQty", data.get("tradedQuantity", data.get("filledQuantity", 0)))
                return {
                    "status": "SUCCESS",
                    "order_id": str(data.get("orderId", order_id)),
                    "order_status": str(raw or "UNKNOWN").upper(),
                    "lifecycle_status": normalize_order_status(raw),
                    "filled_quantity": int(filled or 0),
                    "fill_price": data.get("averageTradedPrice", data.get("avgTradedPrice")),
                    "data": data,
                }
        except Exception as exc:
            logger.error("Dhan order status lookup failed for %s: %s", order_id, exc)
            return {"status": "FAILED", "reason": str(exc)}

    async def get_positions(self) -> Dict[str, Any]:
        """Return broker net positions; failures are explicit and never treated as empty."""
        if not self.is_configured():
            return {"status": "FAILED", "reason": "Broker is not configured"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self.base_api_url}/positions",
                    headers=self._get_headers(),
                )
                data = resp.json()
                if resp.status_code != 200 or not isinstance(data, list):
                    return {"status": "FAILED", "reason": f"Positions HTTP {resp.status_code}", "raw": data}
                return {"status": "SUCCESS", "positions": data}
        except Exception as exc:
            logger.error("Dhan positions lookup failed: %s", exc)
            return {"status": "FAILED", "reason": str(exc)}

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.delete(
                    f"{self.base_api_url}/orders/{order_id}",
                    headers=self._get_headers(),
                )
                return resp.status_code in (200, 202)
        except Exception as exc:
            logger.error("Dhan MCP cancel_order exception: %s", exc)
            return False

    async def emergency_market_exit(
        self,
        security_id: str,
        quantity: int,
        symbol_hint: str = "",
        transaction_type: str = "SELL",
    ) -> Dict[str, Any]:
        """
        Dispatches an immediate zero-negotiation market exit order.
        """
        txn_type = transaction_type.upper()
        logger.warning(
            "🚨 DISPATCHING EMERGENCY MARKET EXIT (%s) via Dhan MCP for %s x %d shares",
            txn_type, symbol_hint or security_id, quantity
        )
        return await self.place_mis_order(
            security_id=security_id,
            transaction_type=txn_type,
            quantity=quantity,
            order_type="MARKET",
            symbol_hint=symbol_hint,
        )

    async def get_quotes(
        self,
        security_ids: Any,
        exchange_segment: str = "NSE_EQ",
    ) -> Dict[str, Dict[str, Any]]:
        """
        Retrieves live market quotes (LTP, OHLCV, volume) via Dhan REST marketfeed endpoint.
        Uses a short 2.0s cache to eliminate rate limits when polled in sub-second loops.
        """
        if isinstance(security_ids, (str, int)):
            id_list = [str(security_ids)]
        elif isinstance(security_ids, (list, tuple, set)):
            id_list = [str(sid) for sid in security_ids]
        else:
            return {}

        now = time.time()
        results: Dict[str, Dict[str, Any]] = {}
        missing_ids: List[str] = []

        if not hasattr(self, "_quotes_cache"):
            self._quotes_cache = {}

        # 429 rate limit backoff guard
        rate_limited_until = getattr(self, "_quote_rate_limited_until", 0.0)
        is_in_cooldown = now < rate_limited_until

        for sid in id_list:
            sid_str = sid.strip()
            if not sid_str:
                continue
            if sid_str in self._quotes_cache:
                cached_ts, cached_val = self._quotes_cache[sid_str]
                if (now - cached_ts < 3.0) or is_in_cooldown:
                    results[sid_str] = cached_val
                    continue
            missing_ids.append(sid_str)

        if not missing_ids or is_in_cooldown:
            for sid_str in missing_ids:
                if sid_str in self._quotes_cache:
                    results[sid_str] = self._quotes_cache[sid_str][1]
                else:
                    results[sid_str] = {"ltp": 0.0, "volume": 0}
            return results

        if not self.is_configured():
            for sid_str in missing_ids:
                results[sid_str] = {"ltp": 0.0, "volume": 0}
            return results

        try:
            numeric_ids = [int(sid) for sid in missing_ids if sid.isdigit()]
            if numeric_ids:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    resp = await client.post(
                        "https://api.dhan.co/v2/marketfeed/quote",
                        json={exchange_segment: numeric_ids},
                        headers=self._get_headers(),
                    )
                    if resp.status_code == 200:
                        feed_data = resp.json().get("data", {}).get(exchange_segment, {})
                        for sid_str, item in feed_data.items():
                            ltp = float(item.get("last_price", 0.0) or 0.0)
                            vol = int(item.get("volume", 0) or 0)
                            ohlc = item.get("ohlc", {})
                            quote_dict = {
                                "ltp": ltp,
                                "volume": vol,
                                "open": float(ohlc.get("open", 0.0) or 0.0),
                                "high": float(ohlc.get("high", 0.0) or 0.0),
                                "low": float(ohlc.get("low", 0.0) or 0.0),
                                "close": float(ohlc.get("close", 0.0) or 0.0),
                                "average_price": float(item.get("average_price", 0.0) or 0.0),
                                "timestamp": now,
                            }
                            self._quotes_cache[sid_str] = (now, quote_dict)
                            results[sid_str] = quote_dict
                    elif resp.status_code == 429:
                        logger.warning("Dhan marketfeed quote API 429 Too Many Requests. Backing off for 5.0s.")
                        self._quote_rate_limited_until = now + 5.0
                        for sid_str in missing_ids:
                            if sid_str in self._quotes_cache:
                                results[sid_str] = self._quotes_cache[sid_str][1]
                            else:
                                results[sid_str] = {"ltp": 0.0, "volume": 0}
        except Exception as exc:
            logger.debug("Failed to fetch live quotes via Dhan REST for %s: %s", missing_ids, exc)

        return results

    async def get_market_depth(self, security_id: str) -> Dict[str, Any]:
        """
        Retrieves 20-level orderbook depth to calculate Bid/Ask volume pressure ratio.
        """
        if not self.is_configured():
            return {"bid_volume": 12000, "ask_volume": 8000, "ratio": 1.5}

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(
                    "https://api.dhan.co/v2/marketfeed/quote",
                    json={"NSE_EQ": [int(security_id)]},
                    headers=self._get_headers(),
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", {}).get("NSE_EQ", {}).get(str(security_id), {})
                    depth = data.get("depth", {})
                    bids = depth.get("buy", [])
                    asks = depth.get("sell", [])
                    total_bid_vol = sum(int(b.get("quantity", 0)) for b in bids)
                    total_ask_vol = sum(int(a.get("quantity", 0)) for a in asks)
                    ratio = round(total_bid_vol / total_ask_vol, 2) if total_ask_vol > 0 else 1.0
                    return {
                        "bid_volume": total_bid_vol,
                        "ask_volume": total_ask_vol,
                        "ratio": ratio,
                    }
        except Exception as exc:
            logger.debug("Failed to fetch orderbook depth for %s: %s", security_id, exc)

        return {"bid_volume": 10000, "ask_volume": 10000, "ratio": 1.0}

    async def get_fund_limits(self) -> Dict[str, Any]:
        """
        Retrieves live account balance and margin limits via Dhan MCP / API gateway.
        """
        if not self.is_configured():
            return {
                "status": "SUCCESS",
                "paper_mode": True,
                "availabelBalance": 180000.0,
                "withdrawableBalance": 180000.0,
                "sodLimit": 180000.0,
                "utilizedAmount": 0.0,
            }

        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(
                    f"{self.base_api_url}/fundlimit",
                    headers=self._get_headers(),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "status": "SUCCESS",
                        "paper_mode": False,
                        "dhanClientId": data.get("dhanClientId", self.client_id),
                        "availabelBalance": float(data.get("availabelBalance", 0.0)),
                        "withdrawableBalance": float(data.get("withdrawableBalance", 0.0)),
                        "sodLimit": float(data.get("sodLimit", 0.0)),
                        "collateralAmount": float(data.get("collateralAmount", 0.0)),
                        "utilizedAmount": float(data.get("utilizedAmount", 0.0)),
                        "blockedPayoutAmount": float(data.get("blockedPayoutAmount", 0.0)),
                        "raw": data,
                    }
                else:
                    return {"status": "FAILED", "reason": f"HTTP {resp.status_code}: {resp.text}"}
        except Exception as exc:
            logger.error("Dhan MCP get_fund_limits exception: %s", exc)
            return {"status": "FAILED", "reason": str(exc)}


_dhan_mcp_singleton: Optional[DhanMCPClient] = None

def get_dhan_mcp_client() -> DhanMCPClient:
    global _dhan_mcp_singleton
    if _dhan_mcp_singleton is None:
        _dhan_mcp_singleton = DhanMCPClient()
    return _dhan_mcp_singleton
