"""
Unit tests for AI Shortlisting Service, Manual Trade Watchdog Registration, and Endpoints.
"""

import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

from services.ai_shortlist_service import AIShortlistService, get_current_ist_timestamp


@pytest.fixture
def temp_service(tmp_path):
    db_path = tmp_path / "test_shortlist.sqlite"
    service = AIShortlistService(db_path=db_path)
    return service


def test_upsert_and_get_trade(temp_service):
    trade = {
        "symbol": "RELIANCE",
        "action": "BUY",
        "setup_type": "BULLISH_BREAKOUT",
        "entry_price": 1250.0,
        "stop_loss": 1230.0,
        "target_1": 1300.0,
        "target_2": 1340.0,
        "risk_reward_ratio": 2.5,
        "conviction": "VERY_HIGH",
        "current_price": 1245.0,
        "pnl_pct": 0.0,
        "thesis": "Breakout above 50 EMA with strong volume.",
    }
    temp_service.upsert_trade(trade)

    trades = temp_service.get_all_trades()
    assert len(trades) == 1
    assert trades[0]["symbol"] == "RELIANCE"
    assert trades[0]["status"] == "WATCHING"
    assert trades[0]["entry_price"] == 1250.0

    retrieved = temp_service.get_trade("RELIANCE")
    assert retrieved is not None
    assert retrieved["conviction"] == "VERY_HIGH"


def test_timeline_event_logging(temp_service):
    evt = temp_service.add_timeline_event(
        symbol="TCS",
        event_type="MANUAL_TRADE_ARMED",
        price_at_event=4120.0,
        message="Directly armed in Live Watchdog",
        severity="success",
    )
    assert evt["symbol"] == "TCS"
    assert "IST" in evt["timestamp_ist"]

    events = temp_service.get_timeline_events("TCS")
    assert len(events) == 1
    assert events[0]["event_type"] == "MANUAL_TRADE_ARMED"
    assert events[0]["severity"] == "success"


def test_generate_ai_shortlist_deprecated(temp_service):
    """Verifies that legacy generate_ai_shortlist returns empty list and does not crash."""
    candidates = [
        {"sym": "STOCK_A", "px": 100.0, "score": 85},
    ]
    res = temp_service.generate_ai_shortlist(universe_data=candidates)
    assert res == []


@pytest.mark.asyncio
async def test_manual_trade_direct_watchdog_registration(monkeypatch):
    """
    Confirms that adding a manual trade registers it directly into Live Watchdog
    with direction, custom levels, and slot reservation without any 40-tick delay.
    """
    from routers.shortlist import add_manual_trade, AddManualTradeRequest
    from services.live_algo_watchdog import get_algo_live_watchdog
    from config.trading_config import CAPITAL_CONFIG
    CAPITAL_CONFIG.MAX_ACTIVE_TRADES = 3

    watchdog = get_algo_live_watchdog()
    watchdog.active_positions.clear()
    watchdog.slots.active_slots.clear()
    from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
    get_algo_live_watchdog_shorts().active_positions.clear()
    from db_store import delete_lifecycle_position
    for symbol in ["SBIN", "INFY", "TCS", "RELIANCE"]:
        delete_lifecycle_position(symbol)

    req = AddManualTradeRequest(
        symbol="INFY",
        action="BUY",
        entry_price=1800.0,
        stop_loss=1760.0,
        target_1=1900.0,
        target_2=1980.0,
        setup_type="50EMA_BOUNCE",
    )
    res = await add_manual_trade(req, user_id="test_user")

    assert res["status"] == "ok"
    assert "INFY" in watchdog.active_positions
    pos = watchdog.active_positions["INFY"]
    assert pos.symbol == "INFY"
    assert pos.entry_price == 1800.0
    assert pos.hard_stop_loss == 1760.0
    assert pos.target_1 == 1900.0
    assert pos.target_2 == 1980.0
    assert pos.action == "BUY"
    assert pos.is_external is True
    assert pos.status == "ACTIVE"

    # SELL manual trades belong to the dedicated shorts desk.
    req_sell = AddManualTradeRequest(
        symbol="TCS",
        action="SELL",
        entry_price=4000.0,
        stop_loss=4040.0,
        target_1=3900.0,
        target_2=3850.0,
        setup_type="BEARISH_BREAKDOWN",
    )
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await add_manual_trade(req_sell, user_id="test_user")
    assert exc_info.value.status_code == 400
    assert "shorts desk" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_manual_trade_slot_limit_enforced():
    """Confirms that adding trades beyond the 3-slot cap returns 400 error."""
    from fastapi import HTTPException
    from routers.shortlist import add_manual_trade, AddManualTradeRequest
    from services.live_algo_watchdog import get_algo_live_watchdog
    from config.trading_config import CAPITAL_CONFIG
    CAPITAL_CONFIG.MAX_ACTIVE_TRADES = 3

    watchdog = get_algo_live_watchdog()
    watchdog.active_positions.clear()
    watchdog.slots.active_slots.clear()

    # Fill 3 slots
    for sym in ["SBIN", "INFY", "TCS"]:
        req = AddManualTradeRequest(symbol=sym, action="BUY", entry_price=500.0)
        await add_manual_trade(req, user_id="test_user")

    assert len(watchdog.active_positions) == 3

    # Attempt 4th trade -> must raise HTTPException(400)
    req_4th = AddManualTradeRequest(symbol="RELIANCE", action="BUY", entry_price=2500.0)
    with pytest.raises(HTTPException) as exc_info:
        await add_manual_trade(req_4th, user_id="test_user")
    assert exc_info.value.status_code == 400
    assert "occupied" in exc_info.value.detail.lower()

    # Cleanup
    for sym in ["SBIN", "INFY", "TCS", "RELIANCE"]:
        watchdog.deregister_position(sym)
    watchdog.active_positions.clear()
    watchdog.slots.active_slots.clear()


def test_shortlist_router_endpoints(client):
    from security import create_jwt
    from services.live_algo_watchdog import get_algo_live_watchdog

    watchdog = get_algo_live_watchdog()
    watchdog.active_positions.clear()
    watchdog.slots.active_slots.clear()

    token = create_jwt({"sub": "test_user"})
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Clear existing
    res_clear = client.post("/api/shortlist/clear", headers=headers)
    assert res_clear.status_code == 200

    # 2. Call generate -> deprecated status returned
    res_gen = client.post("/api/shortlist/generate", json={"universe_data": []}, headers=headers)
    assert res_gen.status_code == 200
    assert res_gen.json()["status"] == "deprecated"

    # 3. Manually add trade
    res_add = client.post("/api/shortlist/add", json={
        "symbol": "RELIANCE",
        "action": "BUY",
        "entry_price": 1280.0,
        "stop_loss": 1260.0,
        "target_1": 1330.0,
        "target_2": 1360.0,
        "setup_type": "50EMA_BOUNCE",
    }, headers=headers)
    assert res_add.status_code == 200
    assert res_add.json()["status"] == "ok"
    assert res_add.json()["trade"]["symbol"] == "RELIANCE"
    assert "position" in res_add.json()

    # 4. Get shortlist
    res_get = client.get("/api/shortlist", headers=headers)
    assert res_get.status_code == 200
    assert len(res_get.json()["trades"]) >= 1

    # 5. Poll status -> returns trades and telemetry
    res_poll = client.post("/api/shortlist/poll-status", headers=headers)
    assert res_poll.status_code == 200
    assert "trades" in res_poll.json()
    assert "new_events" in res_poll.json()

    # 6. Get timeline
    res_tl = client.get("/api/shortlist/timeline", headers=headers)
    assert res_tl.status_code == 200
    assert len(res_tl.json()["events"]) >= 1

    # 7. Delete single trade
    res_del = client.delete("/api/shortlist/RELIANCE", headers=headers)
    assert res_del.status_code == 200

    # Cleanup
    watchdog.active_positions.clear()
    watchdog.slots.active_slots.clear()


@pytest.mark.asyncio
async def test_add_manual_trade_auto_enrichment_from_chart_analysis(monkeypatch):
    """
    Confirms that when a stock is manually added without custom levels,
    it is auto-enriched from multi-timeframe analysis and does NOT default to 100 rs.
    """
    from routers.shortlist import add_manual_trade, AddManualTradeRequest
    from services.multi_tf_analysis import MultiTFAnalysisService
    from services.live_algo_watchdog import get_algo_live_watchdog

    watchdog = get_algo_live_watchdog()
    watchdog.active_positions.clear()
    watchdog.slots.active_slots.clear()

    mock_analysis = {
        "symbol": "TATAMOTORS",
        "current_price": 945.50,
        "commentary": {
            "trigger_price": 952.00,
            "stop_loss": 920.00,
            "target_price_1": 995.00,
            "target_price_2": 1030.00,
            "bias": "BULLISH_BREAKOUT",
            "actionable_thesis": "Clean breakout above ascending channel resistance.",
            "latest_news": "Record commercial vehicle sales beat.",
            "institutional_flow_detail": {"score": 68, "classification": "ACCUMULATION"},
        }
    }

    async def mock_analyze(self, sym):
        return mock_analysis

    monkeypatch.setattr(MultiTFAnalysisService, "analyze_symbol", mock_analyze)

    # Add stock without specifying entry_price, stop_loss, target_1, target_2
    req = AddManualTradeRequest(symbol="TATAMOTORS", action="BUY")
    res = await add_manual_trade(req, user_id="test_user")

    assert res["status"] == "ok"
    trade = res["trade"]
    assert trade["symbol"] == "TATAMOTORS"
    assert trade["current_price"] == 945.50  # Must NOT be 100.0
    assert trade["entry_price"] == 952.00    # Must NOT be 100.0
    assert trade["stop_loss"] == 920.00      # Must NOT be 98.5
    assert trade["target_1"] == 995.00       # Must NOT be 103.0
    assert trade["target_2"] == 1030.00      # Must NOT be 106.0
    assert trade["setup_type"] == "BULLISH_BREAKOUT"
    assert "breakout" in trade["thesis"].lower()

    # Cleanup
    watchdog.active_positions.clear()
    watchdog.slots.active_slots.clear()
