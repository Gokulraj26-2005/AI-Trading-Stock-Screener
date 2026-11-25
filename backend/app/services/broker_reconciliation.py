"""Fail-closed reconciliation between Dhan execution state and SQLite lifecycle state."""

from __future__ import annotations

from typing import Any, Dict, Optional

from db_store import (
    get_lifecycle_order,
    get_lifecycle_orders,
    get_lifecycle_positions,
    save_lifecycle_order,
    save_lifecycle_position,
    update_lifecycle_position_status,
)
from logger import logger
from services.dhan_mcp_client import DhanMCPClient, normalize_order_status


def _filled_quantity(payload: Dict[str, Any], requested: int) -> int:
    value = payload.get("filled_quantity", payload.get("filledQty", payload.get("tradedQuantity", 0)))
    try:
        return max(0, min(int(value or 0), int(requested)))
    except (TypeError, ValueError):
        return 0


async def reconcile_order(
    order_id: str,
    *,
    client: DhanMCPClient,
    long_watchdog: Any = None,
    short_watchdog: Any = None,
    broker_event: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Reconcile one order from polling or webhook input.

    Unknown orders and malformed events are rejected without mutating state.
    """
    local = get_lifecycle_order(order_id)
    if not local:
        return {"status": "IGNORED", "reason": "Unknown local order", "order_id": order_id}
    remote = broker_event or await client.get_order_status(order_id)
    if remote.get("status") != "SUCCESS":
        return {"status": "DEFERRED", "reason": remote.get("reason", "Broker lookup failed"), "order_id": order_id}

    lifecycle = normalize_order_status(remote.get("lifecycle_status", remote.get("order_status")))
    requested = int(local["quantity"])
    previous_filled = int(local.get("filled_quantity", 0) or 0)
    filled = _filled_quantity(remote, requested)
    if lifecycle == "FILLED" and filled == 0:
        filled = requested
    fill_price = remote.get("fill_price") or local.get("filled_price") or local.get("requested_price")
    save_lifecycle_order(
        order_id, local["symbol"], local["security_id"], local["action"], requested,
        float(local["requested_price"]), lifecycle, filled_price=float(fill_price) if fill_price else None,
        payload=remote, filled_quantity=filled,
    )

    action = str(local["action"]).upper()
    if action == "SELL":
        if filled:
            delta_filled = max(0, filled - previous_filled)
            if delta_filled == 0:
                return {"status": lifecycle, "order_id": order_id, "filled_quantity": filled}
            for watchdog in (long_watchdog, short_watchdog):
                if watchdog is not None and watchdog.reconcile_exit_fill(
                    order_id, delta_filled, float(fill_price or 0.0), symbol=str(local["symbol"])
                ):
                    break
        return {"status": lifecycle, "order_id": order_id, "filled_quantity": filled}

    if filled <= 0:
        return {"status": lifecycle, "order_id": order_id, "filled_quantity": 0}

    positions = {str(item.get("symbol")): item for item in get_lifecycle_positions()}
    position = positions.get(str(local["symbol"]))
    if position:
        position["quantity"] = filled
        if fill_price:
            position["entry_price"] = float(fill_price)
        save_lifecycle_position(position, status="ACTIVE")
    else:
        for watchdog in (long_watchdog, short_watchdog):
            if watchdog is None:
                continue
            existing = watchdog.active_positions.get(str(local["symbol"]))
            if existing is not None:
                existing.quantity = filled
                if fill_price:
                    existing.entry_price = float(fill_price)
                save_lifecycle_position(existing.to_dict(), status=existing.status)
                break
            if action == "BUY":
                watchdog.register_position(
                    symbol=str(local["symbol"]),
                    security_id=str(local["security_id"]),
                    entry_price=float(fill_price or local["requested_price"]),
                    quantity=filled,
                    order_id=str(local["order_id"]),
                )
                break
    return {"status": lifecycle, "order_id": order_id, "filled_quantity": filled}


async def reconcile_pending_orders(
    *,
    client: DhanMCPClient,
    long_watchdog: Any = None,
    short_watchdog: Any = None,
) -> Dict[str, Any]:
    results = []
    for order in get_lifecycle_orders(["TRANSIT", "ACCEPTED", "PARTIAL", "EXIT_PENDING", "UNKNOWN"]):
        results.append(await reconcile_order(
            str(order["order_id"]), client=client,
            long_watchdog=long_watchdog, short_watchdog=short_watchdog,
        ))
    return {"status": "OK", "results": results}


def _broker_position_fields(item: Dict[str, Any]) -> tuple[str, str, int, float]:
    symbol = str(item.get("tradingSymbol") or item.get("symbol") or "").strip()
    security_id = str(item.get("securityId") or item.get("security_id") or "").strip()
    quantity = int(item.get("netQty", item.get("netQuantity", item.get("quantity", 0))) or 0)
    price = float(item.get("costPrice", item.get("averagePrice", item.get("avgPrice", 0.0))) or 0.0)
    return symbol, security_id, quantity, price


async def reconcile_startup_positions(*, client: DhanMCPClient, long_watchdog: Any = None) -> Dict[str, Any]:
    """Reconcile broker holdings without deleting local state on uncertainty."""
    response = await client.get_positions()
    if response.get("status") != "SUCCESS":
        logger.warning("Startup broker position reconciliation deferred: %s", response.get("reason"))
        return {"status": "DEFERRED", "reason": response.get("reason", "lookup failed")}
    broker = {}
    for item in response.get("positions", []):
        if not isinstance(item, dict):
            continue
        symbol, security_id, quantity, price = _broker_position_fields(item)
        if symbol and quantity:
            broker[symbol] = (security_id, quantity, price)

    local = {str(item.get("symbol")): item for item in get_lifecycle_positions()}
    degraded = []
    for symbol, position in local.items():
        if symbol not in broker or broker[symbol][1] != int(position.get("quantity", 0)):
            update_lifecycle_position_status(symbol, "PROTECTION_DEGRADED")
            degraded.append(symbol)
    for symbol, (security_id, quantity, price) in broker.items():
        if symbol in local:
            continue
        if long_watchdog is not None and long_watchdog.slots.can_open_new_trade():
            long_watchdog.register_position(
                symbol=symbol, security_id=security_id, entry_price=price,
                quantity=quantity, order_id=f"EXTERNAL_{security_id}_{symbol}",
                is_external=True,
            )
        else:
            logger.critical(
                "Broker position %s is not locally tracked and no capital slot is available; "
                "leaving it unmanaged rather than evicting a local position.",
                symbol,
            )
    return {"status": "OK", "degraded": degraded, "external": [s for s in broker if s not in local]}
