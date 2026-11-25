"""
Unit tests for Institutional Execution System:
- CapitalSlotManager (₹1.80L max, ₹60k fixed allocation, max 3 slots, priority queue)
- LiveAlgoWatchdog (1% Hard SL floor, +0.8% Breakeven lock, dynamic trailing SL, RSI 80+ audit)
- PromotionManager (2-way entry rule, auto-purge on >0.20% slip)
- DhanMCPClient (Paper execution fallback)
- TelegramHITLService (Markdown alert & inline buttons)
"""

import asyncio
import math
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from config.trading_config import CAPITAL_CONFIG
from services.capital_slot_manager import CapitalSlotManager, SlotPosition
from services.live_algo_watchdog import AlgoLiveWatchdog, WatchdogPosition
from services.promotion_manager import PromotionManager
from services.dhan_mcp_client import DhanMCPClient
from services.telegram_hitl_service import TelegramHITLService


@pytest.fixture(autouse=True)
def reset_trading_config_for_tests():
    """Ensure tests run against standard ₹1.80L / ₹60k baseline and restore afterwards."""
    baseline = {
        "execution_enabled": True,
        "total_capital": 180000.0,
        "per_trade_allocation": 60000.0,
        "max_active_trades": 3,
        "max_hard_stop_loss_percent": 1.0,
        "breakeven_trigger_percent": 0.8,
        "structural_trail_percent": 1.5,
        "purge_slip_tolerance_percent": 0.20,
    }
    prev = CAPITAL_CONFIG.to_dict()
    CAPITAL_CONFIG.update_settings(baseline)
    yield
    CAPITAL_CONFIG.update_settings(prev)


# ==============================================================================
# 1. Capital Slot Manager Tests
# ==============================================================================

def test_capital_slot_manager_sizing_60k():
    """Verify strictly ₹60,000 fixed allocation per trade with MIS dynamic leverage."""
    mgr = CapitalSlotManager()
    
    # Stock @ ₹1,000 with 1x leverage (Cash CNC) -> 60 shares
    assert mgr.calculate_quantity(1000.0, leverage=1.0) == 60
    
    # Stock @ ₹1,000 with default 5x MIS leverage -> 300 shares
    assert mgr.calculate_quantity(1000.0) == 300
    
    # Stock @ ₹2,500 with default 5x MIS leverage -> floor(300000 / 2500) = 120 shares
    assert mgr.calculate_quantity(2500.0) == 120

    # Stock @ ₹3,450 with 5x leverage -> floor(300000 / 3450) = 86 shares
    expected_qty = math.floor(300000 / 3450.0)
    assert mgr.calculate_quantity(3450.0) == expected_qty
    
    # Dynamic leverage test (e.g. 4.0x leverage)
    assert mgr.calculate_quantity(1000.0, leverage=4.0) == 240


def test_capital_slot_manager_risk_sizing_caps_loss_before_leverage():
    """Execution quantity must be constrained by stop-distance risk, not buying power."""
    mgr = CapitalSlotManager()
    quantity = mgr.calculate_execution_quantity(
        entry_price=1000.0,
        stop_price=990.0,
        leverage=5.0,
        risk_budget=600.0,
        slippage_per_share=0.0,
        fees_per_share=0.0,
    )
    assert quantity == 60


def test_capital_slot_manager_risk_based_sizing():
    """Risk sizing uses stop distance and independently caps margin buying power."""
    mgr = CapitalSlotManager()

    # Risk budget: 500 / (₹10 stop distance + ₹0.50 costs) = 47 shares.
    assert mgr.calculate_risk_based_quantity(
        entry_price=100.0,
        stop_price=90.0,
        risk_budget=500.0,
        slippage_per_share=0.25,
        fees_per_share=0.25,
        leverage=1.0,
        margin_allocation=10_000.0,
    ) == 47

    # Risk sizing cannot use leverage to exceed the allocated margin cap.
    assert mgr.calculate_risk_based_quantity(
        entry_price=100.0,
        stop_price=99.0,
        risk_budget=10_000.0,
        leverage=2.0,
        margin_allocation=1_000.0,
    ) == 20

    # Invalid cost inputs are rejected rather than silently changing risk.
    with pytest.raises(ValueError, match="cannot be negative"):
        mgr.calculate_risk_based_quantity(
            entry_price=100.0,
            stop_price=90.0,
            risk_budget=500.0,
            fees_per_share=-0.01,
        )


def test_capital_slot_manager_concurrency_3_slots():
    """Verify maximum 3 active concurrent trades and priority queue handling."""
    mgr = CapitalSlotManager()
    assert mgr.can_open_new_trade() is True
    assert mgr.get_portfolio_summary()["available_slots_count"] == 3

    # Fill Slot 1
    s1 = mgr.occupy_slot("TCS", "11536", 3500.0, 17, "ORD-1")
    assert s1 is not None
    assert mgr.get_portfolio_summary()["available_slots_count"] == 2

    # Fill Slot 2
    s2 = mgr.occupy_slot("INFY", "1594", 1500.0, 40, "ORD-2")
    assert s2 is not None
    assert mgr.get_portfolio_summary()["available_slots_count"] == 1

    # Fill Slot 3
    s3 = mgr.occupy_slot("RELIANCE", "2885", 2500.0, 24, "ORD-3")
    assert s3 is not None
    assert mgr.get_portfolio_summary()["available_slots_count"] == 0
    assert mgr.can_open_new_trade() is False

    # Attempt 4th trade -> must raise RuntimeError because slots are full
    with pytest.raises(RuntimeError):
        mgr.occupy_slot("HDFCBANK", "1333", 1600.0, 37, "ORD-4")

    # Enqueue 4th trade into Priority Queue
    candidate_4 = {
        "symbol": "HDFCBANK",
        "security_id": "1333",
        "trigger_price": 1600.0,
        "quantity": 37,
    }
    mgr.enqueue_approved_trade(candidate_4)
    summary = mgr.get_portfolio_summary()
    assert summary["active_slots_count"] == 3
    assert summary["available_slots_count"] == 0
    assert summary["deployed_capital"] == 180000.0
    assert summary["available_capital"] == 0.0
    assert len(summary["priority_queue"]) == 1

    # Release Slot 1 (TCS exits) -> automatically pops HDFCBANK from priority queue!
    next_promoted = mgr.release_slot("TCS")
    assert next_promoted is not None
    assert next_promoted["symbol"] == "HDFCBANK"
    assert mgr.get_portfolio_summary()["available_slots_count"] == 1


# ==============================================================================
# 2. Live Algo Watchdog Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_watchdog_hard_stop_loss_trigger():
    """Verify 1.0% hard stop loss triggers immediate market exit."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()
    mcp_client.emergency_market_exit = AsyncMock(return_value={"status": "CLOSED", "order_id": "EXIT-1"})
    
    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    
    # Register position @ ₹1,000. Hard SL = ₹990.0 (-1.0%)
    pos = watchdog.register_position(
        symbol="TATAMOTORS",
        security_id="3456",
        entry_price=1000.0,
        quantity=60,
        order_id="ORD-100",
    )
    assert pos.hard_stop_loss == 990.0

    # Price drops to ₹991.0 (above hard SL) -> remains ACTIVE
    action = await watchdog.evaluate_tick("TATAMOTORS", 991.0)
    assert action["action"] == "HOLD"
    assert "TATAMOTORS" in watchdog.active_positions

    # Price drops to ₹989.50 (breaches -1.0% hard floor) -> EXIT triggered
    action = await watchdog.evaluate_tick("TATAMOTORS", 989.50)
    assert action["action"] == "CLOSED"
    assert action["reason"] == "HARD_1PCT_SL_HIT"
    mcp_client.emergency_market_exit.assert_awaited_once()


@pytest.mark.asyncio
async def test_watchdog_breakeven_lock():
    """Verify breakeven lock activates at +0.8% advance."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()
    mcp_client.modify_stop_loss = AsyncMock(return_value={"status": "MODIFIED"})

    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    pos = watchdog.register_position(
        symbol="SBIN",
        security_id="3045",
        entry_price=800.0,
        quantity=75,
        order_id="ORD-200",
    )
    assert pos.breakeven_locked is False

    # Price rises +0.5% (to ₹804.0) -> Breakeven not yet locked
    res_hold = await watchdog.evaluate_tick("SBIN", 804.0)
    assert res_hold["action"] == "HOLD"
    assert pos.breakeven_locked is False

    # Price rises +0.85% (to ₹807.0) -> Breakeven LOCK triggered
    # Target SL = entry * (1 + 0.001) = 800 * 1.001 = 800.80
    action = await watchdog.evaluate_tick("SBIN", 807.0)
    assert action["action"] == "MODIFY_SL"
    assert action["reason"] == "BREAKEVEN_LOCKED"
    assert pos.breakeven_locked is True
    assert pos.current_stop_loss >= 800.80
    mcp_client.modify_stop_loss.assert_awaited()


@pytest.mark.asyncio
async def test_watchdog_dynamic_trailing_and_runner():
    """Verify structural dynamic trailing at +1.5% and runner mode."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()
    mcp_client.modify_stop_loss = AsyncMock(return_value={"status": "MODIFIED"})

    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    pos = watchdog.register_position(
        symbol="ICICIBANK",
        security_id="4963",
        entry_price=1200.0,
        quantity=50,
        order_id="ORD-300",
    )
    # Pre-lock breakeven
    pos.breakeven_locked = True

    # Price rises +2.0% (to ₹1224.0) with 5m VWAP at ₹1216.0
    action = await watchdog.evaluate_tick(
        symbol="ICICIBANK",
        ltp=1224.0,
        vwap_5m=1216.0,
        ema20_5m=1214.0,
        rsi_5m=65.0,
    )
    assert action["action"] == "MODIFY_SL"
    assert action["reason"] == "STRUCTURAL_TRAIL"
    assert pos.current_stop_loss >= 1216.0


# ==============================================================================
# 3. Promotion & Auto-Purge Tests
# ==============================================================================

def test_promotion_manager_eligibility():
    """Verify 2-way promotion rule."""
    promo_mgr = PromotionManager()

    # Setup with score < 85 -> Disqualified
    assert promo_mgr.is_eligible_for_promotion(
        symbol="WIPRO",
        score=82,
        live_status="CONFIRMED_TRIGGER",
        trigger_price=500.0,
        current_price=501.0,
        rsi_5m=60.0,
        rvol=1.5,
    ) is False

    # Setup with high RSI >= 73 -> Disqualified
    assert promo_mgr.is_eligible_for_promotion(
        symbol="WIPRO",
        score=90,
        live_status="CONFIRMED_TRIGGER",
        trigger_price=500.0,
        current_price=501.0,
        rsi_5m=75.0,
        rvol=1.5,
    ) is False

    # Setup with status still WARMING_UP (has not sustained breakout) -> Disqualified
    assert promo_mgr.is_eligible_for_promotion(
        symbol="WIPRO",
        score=90,
        live_status="WARMING_UP",
        trigger_price=500.0,
        current_price=501.0,
        rsi_5m=60.0,
        rvol=1.5,
    ) is False

    # Setup with rsi_5m=None -> Disqualified (cannot pass unverified RSI)
    assert promo_mgr.is_eligible_for_promotion(
        symbol="BAJFINANCE",
        score=92,
        live_status="SUSTAINED_BREAKOUT",
        trigger_price=7000.0,
        current_price=7010.0,
        rsi_5m=None,
        rvol=2.1,
    ) is False

    # Setup with rvol=None or rvol < 1.0 -> Disqualified
    assert promo_mgr.is_eligible_for_promotion(
        symbol="BAJFINANCE",
        score=92,
        live_status="SUSTAINED_BREAKOUT",
        trigger_price=7000.0,
        current_price=7010.0,
        rsi_5m=66.0,
        rvol=None,
    ) is False
    assert promo_mgr.is_eligible_for_promotion(
        symbol="BAJFINANCE",
        score=92,
        live_status="SUSTAINED_BREAKOUT",
        trigger_price=7000.0,
        current_price=7010.0,
        rsi_5m=66.0,
        rvol=0.8,
    ) is False

    # Setup passing all criteria -> Eligible!
    assert promo_mgr.is_eligible_for_promotion(
        symbol="BAJFINANCE",
        score=92,
        live_status="SUSTAINED_BREAKOUT",
        trigger_price=7000.0,
        current_price=7010.0,
        rsi_5m=66.0,
        rvol=2.1,
    ) is True


@pytest.mark.asyncio
async def test_promotion_rejects_unverified_live_inputs():
    promo_mgr = PromotionManager()
    promo_mgr.telegram.send_trade_approval_alert = AsyncMock()

    result = await promo_mgr.evaluate_and_promote(
        symbol="UNVERIFIED",
        score=95,
        bias="BULLISH_BREAKOUT",
        live_status="CONFIRMED_TRIGGER",
        trigger_price=100.0,
        current_price=101.0,
        rsi_5m=60.0,
        rvol=1.5,
        quote_source="synthetic",
        quote_age_ms=0,
        rsi_is_real=False,
    )

    assert result == {"promoted": False, "reason": "UNVERIFIED_QUOTE_SOURCE"}
    promo_mgr.telegram.send_trade_approval_alert.assert_not_called() is True


def test_promotion_manager_auto_purge():
    """Verify auto-purge when price slips >0.20% below trigger before fill."""
    promo_mgr = PromotionManager()
    with patch.object(promo_mgr.shortlist_service, "delete_trade") as mock_del:
        # Trigger is ₹7,000. Slip threshold is 0.20% (₹14.0, Floor: ₹6,986.0).
        # Price at ₹6,990 is -0.14% -> within tolerance, no purge
        assert promo_mgr.evaluate_auto_purge("BAJFINANCE", current_price=6990.0, trigger_price=7000.0) is False

        # Price slips to ₹6,980 (< ₹6,986) -> PURGE TRIGGERED
        assert promo_mgr.evaluate_auto_purge("BAJFINANCE", current_price=6980.0, trigger_price=7000.0) is True
        mock_del.assert_called_once_with("BAJFINANCE")


# ==============================================================================
# 4. Dhan MCP Client Paper Fallback Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_dhan_mcp_client_paper_order():
    """Verify deterministic simulated execution when in paper mode."""
    client = DhanMCPClient()
    client.is_configured = MagicMock(return_value=False)
    res = await client.place_mis_order(
        security_id="11536",
        transaction_type="BUY",
        quantity=17,
        price=3500.0,
        order_type="MARKET",
        symbol_hint="TCS",
    )
    assert res["status"] in ["FILLED", "SUCCESS", "EXECUTED"]
    assert res["order_status"] == "TRADED"
    assert res["order_id"].startswith("SIM_")
    assert client._mock_orders[res["order_id"]]["symbol"] == "TCS"
    assert client._mock_orders[res["order_id"]]["quantity"] == 17


# ==============================================================================
# 5. Master Execution Switch & Settings Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_master_execution_switch_blocks_telegram_and_mcp():
    """Verify master execution switch blocks Telegram alert and MCP order placement when OFF."""
    promo_mgr = PromotionManager()
    promo_mgr.telegram.send_trade_approval_alert = AsyncMock()

    # 1. Turn switch OFF
    CAPITAL_CONFIG.toggle_execution(False)
    assert CAPITAL_CONFIG.execution_enabled is False

    sym = "NEW_TEST_SYM"
    promo_mgr.shortlist_service.delete_trade(sym)
    if sym in promo_mgr._promoted_cache:
        del promo_mgr._promoted_cache[sym]

    # Attempt promotion of qualifying candidate
    res = await promo_mgr.evaluate_and_promote(
        symbol=sym,
        score=95,
        bias="BULLISH_BREAKOUT",
        live_status="CONFIRMED_TRIGGER",
        trigger_price=2500.0,
        current_price=2502.0,
        rsi_5m=62.0,
        rvol=1.8,
    )
    assert res["promoted"] is False
    assert res["execution_blocked"] is True
    # Telegram alert was BLOCKED before sending!
    promo_mgr.telegram.send_trade_approval_alert.assert_not_called()

    # Attempt direct trade approval via router function
    from routers.watchdog_router import _execute_trade_approval
    approval_res = await _execute_trade_approval(sym, "9999", 2500.0, 24)
    assert approval_res["status"] == "BLOCKED"
    assert "Execution Switch is currently OFF" in approval_res["message"]

    # Re-enable switch and cleanup
    CAPITAL_CONFIG.toggle_execution(True)
    assert CAPITAL_CONFIG.execution_enabled is True
    promo_mgr.shortlist_service.delete_trade(sym)


def test_watchdog_settings_update_and_persistence():
    """Verify updating capital, trades count, and stop loss updates config dynamically."""
    # Update to custom institutional parameters
    updated = CAPITAL_CONFIG.update_settings({
        "total_capital": 240000.0,
        "per_trade_allocation": 80000.0,
        "max_active_trades": 3,
        "max_hard_stop_loss_percent": 1.5,
        "breakeven_trigger_percent": 0.9,
    })

    assert updated["total_capital"] == 240000.0
    assert updated["per_trade_allocation"] == 80000.0
    assert updated["max_active_trades"] == 3
    assert updated["max_hard_stop_loss_percent"] == 1.5
    assert updated["breakeven_trigger_percent"] == 0.9

    # Verify CapitalSlotManager immediately reflects new sizing:
    # (₹80,000 * 5) / ₹2,000 = 200 shares (or 40 shares at 1x)
    mgr = CapitalSlotManager()
    assert mgr.calculate_quantity(2000.0, leverage=1.0) == 40
    assert mgr.calculate_quantity(2000.0) == 200

    # Reset back to default 1.80L / 60k
    CAPITAL_CONFIG.update_settings({
        "total_capital": 180000.0,
        "per_trade_allocation": 60000.0,
        "max_active_trades": 3,
        "max_hard_stop_loss_percent": 1.0,
        "breakeven_trigger_percent": 0.8,
        "execution_enabled": True,
    })
    assert mgr.calculate_quantity(2000.0, leverage=1.0) == 30
    assert mgr.calculate_quantity(2000.0) == 150


@pytest.mark.asyncio
async def test_watchdog_structural_trail_does_not_skip_rsi_exhaustion():
    """Verify Bug #1 fix: Structural trailing (+1.5% profit) does NOT skip Stage 4 RSI exhaustion exit."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()
    mcp_client.modify_stop_loss = AsyncMock(return_value={"status": "MODIFIED"})
    mcp_client.emergency_market_exit = AsyncMock(return_value={"status": "FILLED"})

    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    pos = watchdog.register_position(
        symbol="TCS",
        security_id="11536",
        entry_price=3000.0,
        quantity=20,
        order_id="ORD-TCS",
    )
    pos.breakeven_locked = True

    # Price at ₹3,060 (+2.0% profit), VWAP at ₹3,030 (qualifies for structural trail),
    # BUT RSI is 85.0 (overbought) and cvd_delta is -500 (orderflow exhaustion).
    action = await watchdog.evaluate_tick(
        symbol="TCS",
        ltp=3060.0,
        vwap_5m=3030.0,
        rsi_5m=85.0,
        cvd_delta=-500.0,
    )
    # Must NOT just return MODIFY_SL from Stage 3; must close position due to exhaustion!
    assert action["action"] == "CLOSED"
    assert action["reason"] == "RSI80_ORDERFLOW_EXHAUSTION"
    assert "TCS" not in watchdog.active_positions
    mcp_client.emergency_market_exit.assert_awaited_once()


@pytest.mark.asyncio
async def test_watchdog_runner_same_tick_stop_hit():
    """Verify Bug #4 fix: Runner mode catches same-tick SL hit."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()
    mcp_client.modify_stop_loss = AsyncMock(return_value={"status": "MODIFIED"})
    mcp_client.emergency_market_exit = AsyncMock(return_value={"status": "FILLED"})

    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    pos = watchdog.register_position(
        symbol="INFY",
        security_id="1594",
        entry_price=1500.0,
        quantity=40,
        order_id="ORD-INFY",
    )
    pos.breakeven_locked = True
    pos.current_stop_loss = 1510.0
    pos.peak_price = 1530.0  # Peak reached earlier

    # In runner mode with tight trail (0.35% below price):
    # Suppose price is evaluated at 1530.0, runner_sl = 1530 * (1 - 0.0035) = 1524.64.
    # But suppose ltp is exactly at or below runner_sl (e.g. flash-wick):
    action = await watchdog.evaluate_tick(
        symbol="INFY",
        ltp=1524.0,  # Below the tight runner trail
        rsi_5m=82.0,
        cvd_delta=200.0,  # Positive CVD -> runner mode branch
        bid_ask_ratio=1.2,
    )
    # The runner SL hit on the same tick is caught immediately
    assert action["action"] == "CLOSED"
    assert action["reason"] in ("RUNNER_STOP_HIT", "TRAILED_STOP_HIT")
    assert "INFY" not in watchdog.active_positions


@pytest.mark.asyncio
async def test_watchdog_emergency_kill_all_realistic_pnl():
    """Verify Bug #3 fix: Emergency kill-all uses realistic SL reference, never inflated peak_price."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()
    mcp_client.emergency_market_exit = AsyncMock(return_value={"status": "FILLED"})

    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    pos = watchdog.register_position(
        symbol="RELIANCE",
        security_id="2885",
        entry_price=2800.0,
        quantity=20,
        order_id="ORD-REL",
    )
    pos.current_stop_loss = 2820.0
    pos.peak_price = 3000.0  # Artificially high peak in the past

    kill_res = await watchdog.emergency_kill_all()
    assert kill_res["status"] == "ALL_POSITIONS_KILLED"
    rel_res = kill_res["results"]["RELIANCE"]
    assert rel_res["action"] == "CLOSED"
    # PnL should be calculated from conservative stop loss (2820.0), NOT peak_price (3000.0)
    assert rel_res["exit_price"] == 2820.0
    assert rel_res["pnl"] == (2820.0 - 2800.0) * 20  # +400, NOT +4000


# ==============================================================================
# 6. 200-Depth Order Book Watchdog Integration Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_watchdog_200depth_slot_opens():
    """Verify register_position dispatches update_candidates with 1 active trade to 200-depth manager."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()
    mock_depth_mgr = MagicMock()
    mock_depth_mgr.update_candidates = AsyncMock()

    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    watchdog._depth_manager = mock_depth_mgr

    pos = watchdog.register_position(
        symbol="HDFCBANK",
        security_id="1333",
        entry_price=1600.0,
        quantity=37,
        order_id="ORD-HDFC-1",
    )

    assert pos.symbol == "HDFCBANK"
    assert pos.depth_level_active == "20-level"  # Initial state before tick evaluation
    await asyncio.sleep(0.01)

    mock_depth_mgr.update_candidates.assert_awaited_once()
    candidates = mock_depth_mgr.update_candidates.await_args[0][0]
    assert len(candidates) == 1
    assert candidates[0]["symbol"] == "HDFCBANK"
    assert candidates[0]["SecurityId"] == "1333"
    assert candidates[0]["score"] == 100.0


@pytest.mark.asyncio
async def test_watchdog_200depth_slot_closes():
    """Verify _close_trade releases socket and calls update_candidates with empty list when last trade exits."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()
    mcp_client.emergency_market_exit = AsyncMock(return_value={"status": "FILLED"})
    mock_depth_mgr = MagicMock()
    mock_depth_mgr.update_candidates = AsyncMock()
    mock_depth_mgr.active_connections = {"1333": MagicMock()}
    mock_depth_mgr.connection_tasks = {}
    mock_depth_mgr.active_metadata = {"1333": {}}

    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    watchdog._depth_manager = mock_depth_mgr

    pos = watchdog.register_position(
        symbol="HDFCBANK",
        security_id="1333",
        entry_price=1600.0,
        quantity=37,
        order_id="ORD-HDFC-1",
    )
    await asyncio.sleep(0.01)
    mock_depth_mgr.update_candidates.reset_mock()

    close_res = await watchdog._close_trade(pos, 1620.0, "TAKE_PROFIT")
    assert close_res["action"] == "CLOSED"
    assert "1333" not in mock_depth_mgr.active_connections

    await asyncio.sleep(0.01)
    mock_depth_mgr.update_candidates.assert_awaited_once_with([])


@pytest.mark.asyncio
async def test_evaluate_tick_uses_deep_obi_for_runner():
    """Verify deep_obi=+0.23 leads to runner mode tightening rather than exhaustion when RSI >= 80."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()
    mcp_client.modify_stop_loss = AsyncMock(return_value={"status": "MODIFIED"})

    mock_depth_mgr = MagicMock()
    mock_depth_mgr.active_connections = {"1333": MagicMock()}

    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    watchdog._depth_manager = mock_depth_mgr

    pos = watchdog.register_position(
        symbol="HDFCBANK",
        security_id="1333",
        entry_price=1600.0,
        quantity=37,
        order_id="ORD-HDFC-1",
    )
    pos.breakeven_locked = True
    pos.current_stop_loss = 1610.0
    pos.peak_price = 1630.0

    mock_cvd = MagicMock()
    mock_cvd.get_deep_obi.return_value = 0.23  # Strongly positive 200-depth book
    mock_cvd.get_deep_depth_summary.return_value = {"walls": []}  # No seller walls

    with patch("services.cvd_engine.get_cvd_engine", return_value=mock_cvd):
        action = await watchdog.evaluate_tick(
            symbol="HDFCBANK",
            ltp=1630.0,
            rsi_5m=82.0,
        )

    assert action["action"] == "MODIFY_SL"
    assert action["reason"] == "RUNNER_TIGHT_TRAIL"
    assert pos.is_runner_mode is True
    assert pos.depth_level_active == "200-level"


@pytest.mark.asyncio
async def test_evaluate_tick_ask_wall_exhaustion():
    """Verify that an ask wall within 0.5% of LTP triggers RSI80_DEEP_ORDERFLOW_EXHAUSTION."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()
    mcp_client.emergency_market_exit = AsyncMock(return_value={"status": "FILLED"})

    mock_depth_mgr = MagicMock()
    mock_depth_mgr.active_connections = {"1333": MagicMock()}

    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    watchdog._depth_manager = mock_depth_mgr

    pos = watchdog.register_position(
        symbol="HDFCBANK",
        security_id="1333",
        entry_price=1600.0,
        quantity=37,
        order_id="ORD-HDFC-1",
    )
    pos.breakeven_locked = True
    pos.current_stop_loss = 1610.0
    pos.peak_price = 1630.0

    mock_cvd = MagicMock()
    mock_cvd.get_deep_obi.return_value = 0.05
    # Institutional ask wall sitting at ₹1632 (within 0.12% of LTP ₹1630)
    mock_cvd.get_deep_depth_summary.return_value = {
        "walls": [
            {"price": 1632.0, "side": "ask", "strength": 6.5}
        ]
    }

    with patch("services.cvd_engine.get_cvd_engine", return_value=mock_cvd):
        action = await watchdog.evaluate_tick(
            symbol="HDFCBANK",
            ltp=1630.0,
            rsi_5m=81.5,
        )

    assert action["action"] == "CLOSED"
    assert action["reason"] == "RSI80_DEEP_ORDERFLOW_EXHAUSTION"
    assert pos.depth_level_active == "200-level"
    assert "HDFCBANK" not in watchdog.active_positions


@pytest.mark.asyncio
async def test_evaluate_tick_fallback_20level():
    """Verify fallback to 20-level when position is not in 200-depth manager's active_connections."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()

    mock_depth_mgr = MagicMock()
    mock_depth_mgr.active_connections = {}  # 200-depth socket not yet established

    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    watchdog._depth_manager = mock_depth_mgr

    pos = watchdog.register_position(
        symbol="RELIANCE",
        security_id="2885",
        entry_price=2500.0,
        quantity=24,
        order_id="ORD-REL-1",
    )

    mock_cvd = MagicMock()
    mock_cvd.get_deep_obi.return_value = 0.08  # Obtained from 20-level feed
    mock_cvd.get_deep_depth_summary.return_value = {"walls": []}

    with patch("services.cvd_engine.get_cvd_engine", return_value=mock_cvd):
        action = await watchdog.evaluate_tick(
            symbol="RELIANCE",
            ltp=2505.0,
        )

    assert action["action"] == "HOLD"
    # Should fall back to 20-level (NEVER 5-level)
    assert pos.depth_level_active == "20-level"


@pytest.mark.asyncio
async def test_watchdog_settings_depth_intelligence_telemetry():
    """Verify GET /api/watchdog/settings exposes depth_intelligence with fallback chain and telemetry."""
    from routers.watchdog_router import get_watchdog_settings_endpoint
    from services.live_algo_watchdog import get_algo_live_watchdog

    watchdog = get_algo_live_watchdog()
    resp = await get_watchdog_settings_endpoint(user_id="dev-user")
    assert resp["status"] == "ok"
    assert "depth_intelligence" in resp
    di = resp["depth_intelligence"]
    assert di["analytical_pool_size"] == 47
    assert di["watchdog_reserved_slots"] == 3
    assert di["fallback_chain"] == "200-level → 20-level (never 5-level)"
    assert "summary" in di
    assert "per_position" in di


@pytest.mark.asyncio
async def test_telegram_webhook_hmac_and_chat_security(client, monkeypatch):
    """Verify that unauthorized secret token or chat ID returns 403 on telegram-webhook."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "super-secret-token-123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999888777")

    # 1. Missing secret token header -> 403
    resp1 = client.post(
        "/api/watchdog/telegram-webhook",
        json={"callback_query": {"data": "APPROVE:TATAMOTORS:3456:1000.0:60"}},
    )
    assert resp1.status_code == 403

    # 2. Wrong secret token header -> 403
    resp2 = client.post(
        "/api/watchdog/telegram-webhook",
        json={"callback_query": {"data": "APPROVE:TATAMOTORS:3456:1000.0:60"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-token"},
    )
    assert resp2.status_code == 403

    # 3. Valid secret token header, but unauthorized chat_id -> 403
    resp3 = client.post(
        "/api/watchdog/telegram-webhook",
        json={
            "callback_query": {
                "data": "APPROVE:TATAMOTORS:3456:1000.0:60",
                "message": {"chat": {"id": "111222333"}},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "super-secret-token-123"},
    )
    assert resp3.status_code == 403


@pytest.mark.asyncio
async def test_watchdog_failed_exit_retains_position():
    """Verify that if emergency market exit fails, position is NOT removed and slot is NOT released."""
    slot_mgr = CapitalSlotManager()
    mcp_client = DhanMCPClient()
    # Broker rejects exit order
    mcp_client.emergency_market_exit = AsyncMock(return_value={"status": "FAILED", "reason": "Broker unreachable"})

    watchdog = AlgoLiveWatchdog(slot_manager=slot_mgr, mcp_client=mcp_client)
    pos = watchdog.register_position(
        symbol="SBIN",
        security_id="3045",
        entry_price=800.0,
        quantity=50,
        order_id="ORD-SBIN",
    )

    # Evaluate tick hitting hard stop loss
    action = await watchdog.evaluate_tick("SBIN", 790.0)
    assert action["action"] == "EXIT_FAILED"
    assert pos.status == "EXIT_FAILED"
    # Position must STILL be monitored and slot retained!
    assert "SBIN" in watchdog.active_positions
    assert slot_mgr.get_portfolio_summary()["active_slots_count"] == 1
