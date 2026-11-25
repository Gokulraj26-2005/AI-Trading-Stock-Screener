from unittest.mock import AsyncMock, MagicMock

import pytest

import db_store
from services.broker_reconciliation import reconcile_order, reconcile_startup_positions


@pytest.mark.asyncio
async def test_partial_fill_is_persisted_and_registers_only_filled_quantity(tmp_path, monkeypatch):
    monkeypatch.setattr(db_store, "DB", tmp_path / "lifecycle.db")
    db_store.init_db()
    db_store.save_lifecycle_order(
        "ORD-1", "TCS", "11536", "BUY", 10, 3500.0, "TRANSIT"
    )
    watchdog = MagicMock()
    watchdog.active_positions = {}
    client = MagicMock()
    client.get_order_status = AsyncMock(return_value={
        "status": "SUCCESS",
        "order_id": "ORD-1",
        "lifecycle_status": "PARTIAL",
        "filled_quantity": 4,
        "fill_price": 3501.5,
    })

    result = await reconcile_order("ORD-1", client=client, long_watchdog=watchdog)

    assert result["status"] == "PARTIAL"
    assert db_store.get_lifecycle_order("ORD-1")["filled_quantity"] == 4
    watchdog.register_position.assert_called_once()
    assert watchdog.register_position.call_args.kwargs["quantity"] == 4


@pytest.mark.asyncio
async def test_pending_exit_fill_reduces_position_and_full_fill_removes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(db_store, "DB", tmp_path / "lifecycle.db")
    db_store.init_db()
    db_store.save_lifecycle_order("EXIT-1", "TCS", "11536", "SELL", 10, 3490.0, "EXIT_PENDING")
    db_store.save_lifecycle_position({
        "symbol": "TCS", "security_id": "11536", "entry_price": 3500.0,
        "quantity": 10, "order_id": "ENTRY-1",
    }, status="EXIT_PENDING")
    watchdog = MagicMock()
    watchdog.reconcile_exit_fill = MagicMock(return_value=True)
    client = MagicMock()
    client.get_order_status = AsyncMock(return_value={
        "status": "SUCCESS", "lifecycle_status": "PARTIAL",
        "filled_quantity": 3, "fill_price": 3490.0,
    })

    result = await reconcile_order("EXIT-1", client=client, long_watchdog=watchdog)

    assert result["status"] == "PARTIAL"
    watchdog.reconcile_exit_fill.assert_called_once()
    assert db_store.get_lifecycle_order("EXIT-1")["filled_quantity"] == 3


@pytest.mark.asyncio
async def test_startup_reconciliation_degrades_drift_and_registers_external_position(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db_store, "DB", tmp_path / "lifecycle.db")
    db_store.init_db()
    db_store.save_lifecycle_position({
        "symbol": "TCS", "security_id": "11536", "entry_price": 3500.0,
        "quantity": 10, "order_id": "ENTRY-1",
    })
    watchdog = MagicMock()
    client = MagicMock()
    client.get_positions = AsyncMock(return_value={
        "status": "SUCCESS",
        "positions": [{
            "tradingSymbol": "INFY", "securityId": "1594",
            "netQty": 5, "costPrice": 1500.0,
        }],
    })

    result = await reconcile_startup_positions(client=client, long_watchdog=watchdog)

    assert result["status"] == "OK"
    assert result["degraded"] == ["TCS"]
    watchdog.register_position.assert_called_once()
    assert watchdog.register_position.call_args.kwargs["is_external"] is True
