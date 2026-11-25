import pytest

from services.ai_shortlist_service import AIShortlistService
from services.telegram_hitl_service import TelegramHITLService


def _trade(symbol="RELIANCE"):
    return {
        "symbol": symbol,
        "action": "BUY",
        "setup_type": "BREAKOUT",
        "entry_price": 100.0,
        "stop_loss": 98.0,
        "target_1": 104.0,
        "target_2": 108.0,
        "risk_reward_ratio": 2.0,
        "conviction": "HIGH",
        "current_price": 100.0,
    }


def test_trade_intent_is_immutable_and_claim_is_idempotent(tmp_path):
    service = AIShortlistService(tmp_path / "shortlist.sqlite")
    service.upsert_trade(_trade())

    intent_id = service.create_trade_intent("RELIANCE", "123", 101.0, 7, "BUY")
    assert intent_id
    assert service.create_trade_intent("RELIANCE", "999", 999.0, 99, "SELL") == intent_id

    first = service.claim_trade_intent(intent_id)
    assert first["status"] == "CLAIMED"
    assert first["trade"]["intent_security_id"] == "123"
    assert first["trade"]["intent_trigger_price"] == 101.0
    assert first["trade"]["intent_quantity"] == 7
    assert service.claim_trade_intent(intent_id)["status"] == "IN_PROGRESS"

    result = {"status": "EXECUTED", "order_id": "order-1"}
    service.complete_trade_intent(intent_id, result)
    replay = service.claim_trade_intent(intent_id)
    assert replay["status"] == "REPLAY"
    assert replay["result"] == result


@pytest.mark.asyncio
async def test_telegram_approval_callback_contains_only_intent_id(monkeypatch, tmp_path):
    service = AIShortlistService(tmp_path / "shortlist.sqlite")
    service.upsert_trade(_trade("INFY"))
    intent_id = service.create_trade_intent("INFY", "456", 200.0, 3, "BUY")
    monkeypatch.setattr(
        "services.ai_shortlist_service.get_ai_shortlist_service",
        lambda: service,
    )

    telegram = TelegramHITLService()
    captured = {}

    async def approval(**kwargs):
        captured.update(kwargs)
        return {"status": "EXECUTED"}

    telegram.register_callbacks(approval, lambda symbol: None)
    result = await telegram.process_callback_data(f"APPROVE_INTENT:{intent_id}")

    assert result["status"] == "EXECUTED"
    assert captured == {"intent_id": intent_id}
