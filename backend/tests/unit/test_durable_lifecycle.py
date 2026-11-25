import db_store
import time


def test_lifecycle_state_round_trips_in_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr(db_store, "DB", tmp_path / "lifecycle.db")
    db_store.init_db()

    position = {
        "symbol": "TCS",
        "security_id": "11536",
        "entry_price": 3500.0,
        "quantity": 2,
        "order_id": "ORD-1",
        "action": "BUY",
        "hard_stop_loss": 3465.0,
    }
    db_store.save_lifecycle_order(
        "ORD-1", "TCS", "11536", "BUY", 2, 3500.0, "FILLED",
        filled_price=3501.0, payload={"orderStatus": "TRADED"},
    )
    db_store.save_lifecycle_position(position)
    db_store.save_lifecycle_queue(
        [{"symbol": "INFY", "security_id": "1594", "quantity": 3, "enqueued_at": 10.0}]
    )

    assert db_store.get_lifecycle_positions() == [position]
    assert db_store.get_lifecycle_queue()[0]["symbol"] == "INFY"

    db_store.delete_lifecycle_position("TCS")
    assert db_store.get_lifecycle_positions() == []


def test_capital_slots_restore_persisted_positions_and_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(db_store, "DB", tmp_path / "lifecycle.db")
    db_store.init_db()
    db_store.save_lifecycle_position({
        "symbol": "TCS",
        "security_id": "11536",
        "entry_price": 3500.0,
        "quantity": 2,
        "order_id": "ORD-1",
        "allocated_capital": 60000.0,
        "entry_time": 100.0,
    })
    db_store.save_lifecycle_queue([
        {"symbol": "INFY", "security_id": "1594", "quantity": 3, "enqueued_at": 10.0}
    ])

    from services.capital_slot_manager import CapitalSlotManager

    manager = CapitalSlotManager(restore_persisted=True)
    assert manager.active_slots["TCS"].order_id == "ORD-1"
    assert manager.priority_queue[0]["symbol"] == "INFY"
    assert manager.can_open_new_trade()


def test_slot_reservation_is_atomic_and_idempotent(monkeypatch):
    from services.capital_slot_manager import CapitalSlotManager

    manager = CapitalSlotManager()
    monkeypatch.setattr(
        "services.capital_slot_manager.CAPITAL_CONFIG.MAX_ACTIVE_TRADES", 1
    )

    assert manager.reserve_slot("TCS", "intent-1")
    assert manager.reserve_slot("TCS", "intent-1")
    assert not manager.reserve_slot("INFY", "intent-2")
    assert manager.can_open_new_trade() is False
    assert manager.release_reservation("TCS", "intent-1")
    assert manager.can_open_new_trade() is True


def test_queue_promotion_discards_expired_and_invalid_candidates(monkeypatch):
    from services.capital_slot_manager import CapitalSlotManager

    manager = CapitalSlotManager()
    manager.active_slots["FULL"] = object()
    valid = {"symbol": "VALID", "enqueued_at": time.time(), "expires_at": time.time() + 100}
    manager.priority_queue = [
        {"symbol": "EXPIRED", "enqueued_at": time.time() - 20, "expires_at": time.time() - 1},
        {"symbol": "INVALID", "enqueued_at": time.time(), "expires_at": time.time() + 100},
        valid,
    ]
    monkeypatch.setattr(
        "services.capital_slot_manager.CAPITAL_CONFIG.MAX_ACTIVE_TRADES", 1
    )

    assert manager.release_slot("FULL", validator=lambda item: item["symbol"] == "VALID") == valid
    assert manager.priority_queue == []
