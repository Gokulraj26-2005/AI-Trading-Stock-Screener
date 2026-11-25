"""
Comprehensive Unit Tests for Shorts Pipeline & Longs/Shorts Separation:
- AIShortlistServiceShorts (isolated SQLite DB, short CRUD, timeline logs)
- PromotionManagerShorts (pure bearish gating, short sizing, ceiling SL, targets, auto-purge)
- AlgoLiveWatchdogShorts (direction-pure short math: ceiling SL, breakeven lock, structural resistance, RSI <= 20 exhaustion, cover exit with BUY)
- Longs Pipeline Purity (PromotionManager & AlgoLiveWatchdog strictly long-only)
- Shared Resources (CapitalSlotManager shared 3 slots, settings synchronization)
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from config.trading_config import CAPITAL_CONFIG
from services.capital_slot_manager import CapitalSlotManager
from services.ai_shortlist_service_shorts import AIShortlistServiceShorts
from services.promotion_manager_shorts import PromotionManagerShorts
from services.live_algo_watchdog_shorts import AlgoLiveWatchdogShorts, WatchdogPositionShort
from services.promotion_manager import PromotionManager
from services.live_algo_watchdog import AlgoLiveWatchdog, WatchdogPosition
from services.live_trigger_state import LiveTriggerStateStore


@pytest.fixture(autouse=True)
def reset_config_baseline():
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
# 1. AIShortlistServiceShorts Tests
# ==============================================================================

def test_ai_shortlist_service_shorts_crud(tmp_path):
    db_file = tmp_path / "test_shortlist_shorts.sqlite"
    svc = AIShortlistServiceShorts(db_path=db_file)
    
    # Insert a short trade
    trade = {
        "symbol": "TATASTEEL",
        "action": "SELL",
        "setup_type": "BEARISH_BREAKDOWN",
        "entry_price": 150.0,
        "stop_loss": 151.5,
        "target_1": 145.5,
        "target_2": 141.0,
        "risk_reward_ratio": 3.0,
        "conviction": "HIGH",
        "status": "WATCHING",
        "current_price": 150.0,
        "pnl_pct": 0.0,
    }
    svc.upsert_trade(trade)

    retrieved = svc.get_trade("TATASTEEL")
    assert retrieved is not None
    assert retrieved["symbol"] == "TATASTEEL"
    assert retrieved["action"] == "SELL"
    assert retrieved["stop_loss"] == 151.5

    # Update price & status
    svc.update_trade_price_and_status("TATASTEEL", 147.0, 2.0, "ACTIVE_IN_TRADE")
    updated = svc.get_trade("TATASTEEL")
    assert updated["current_price"] == 147.0
    assert updated["status"] == "ACTIVE_IN_TRADE"

    # Timeline event
    ev = svc.add_timeline_event("TATASTEEL", "TEST_EVENT", "Breakdown confirmed", price_at_event=147.0)
    assert ev["symbol"] == "TATASTEEL"
    events = svc.get_timeline_events(symbol="TATASTEEL")
    assert len(events) == 1
    assert events[0]["message"] == "Breakdown confirmed"

    # Delete trade
    deleted = svc.delete_trade("TATASTEEL")
    assert deleted is True
    assert svc.get_trade("TATASTEEL") is None


# ==============================================================================
# 2. PromotionManagerShorts Tests
# ==============================================================================

def test_promotion_manager_shorts_gating(monkeypatch):
    from config.trading_config import CAPITAL_CONFIG
    monkeypatch.setattr(CAPITAL_CONFIG, "PROMOTION_CUTOFF_TIME_IST", "23:59")
    promo = PromotionManagerShorts()

    # Bullish bias should be rejected immediately by shorts promotion manager
    assert promo.is_eligible_for_promotion(
        symbol="RELIANCE",
        score=90,
        live_status="CONFIRMED_TRIGGER",
        trigger_price=2500.0,
        current_price=2505.0,
        rsi_5m=60.0,
        rvol=1.5,
        bias="BULLISH_BREAKOUT",
    ) is False

    # Low score rejected
    assert promo.is_eligible_for_promotion(
        symbol="RELIANCE",
        score=75,
        live_status="CONFIRMED_TRIGGER",
        trigger_price=2500.0,
        current_price=2495.0,
        rsi_5m=45.0,
        rvol=1.5,
        bias="BEARISH_BREAKDOWN",
    ) is False

    # Oversold RSI (<= 27.0) rejected to prevent shorting into exhaustion
    assert promo.is_eligible_for_promotion(
        symbol="RELIANCE",
        score=90,
        live_status="CONFIRMED_TRIGGER",
        trigger_price=2500.0,
        current_price=2495.0,
        rsi_5m=25.0,
        rvol=1.5,
        bias="BEARISH_BREAKDOWN",
    ) is False

    # Low RVOL rejected
    assert promo.is_eligible_for_promotion(
        symbol="RELIANCE",
        score=90,
        live_status="CONFIRMED_TRIGGER",
        trigger_price=2500.0,
        current_price=2495.0,
        rsi_5m=45.0,
        rvol=0.7,
        bias="BEARISH_BREAKDOWN",
    ) is False

    # Valid short breakdown passes
    assert promo.is_eligible_for_promotion(
        symbol="RELIANCE",
        score=90,
        live_status="CONFIRMED_TRIGGER",
        trigger_price=2500.0,
        current_price=2495.0,
        rsi_5m=45.0,
        rvol=1.5,
        bias="BEARISH_BREAKDOWN",
    ) is True


@pytest.mark.asyncio
async def test_short_promotion_rejects_unverified_market_inputs():
    promo = PromotionManagerShorts()
    common = {
        "symbol": "RELIANCE",
        "score": 90,
        "bias": "BEARISH_BREAKDOWN",
        "live_status": "CONFIRMED_TRIGGER",
        "trigger_price": 2500.0,
        "current_price": 2495.0,
        "rsi_5m": 45.0,
        "rvol": 1.5,
        "quote_source": "aggregator",
        "quote_age_ms": 1000,
        "rsi_is_real": True,
    }
    result = await promo.evaluate_and_promote(**{**common, "quote_source": "web"})
    assert result["reason"] == "UNVERIFIED_QUOTE_SOURCE"

    result = await promo.evaluate_and_promote(**{**common, "quote_age_ms": 75_001})
    assert result["reason"] == "STALE_QUOTE"

    result = await promo.evaluate_and_promote(**{**common, "rsi_is_real": False})
    assert result["reason"] == "UNVERIFIED_RSI"


def test_promotion_manager_shorts_auto_purge(tmp_path):
    promo = PromotionManagerShorts()
    db_file = tmp_path / "test_purge_shorts.sqlite"
    promo.shortlist_service = AIShortlistServiceShorts(db_path=db_file)
    promo.shortlist_service.upsert_trade({"symbol": "INFY", "action": "SELL", "entry_price": 1500.0})

    # Trigger = 1500. Purge ceiling is 1500 * (1 + 0.0020) = 1503.00
    # Price rises to 1501 -> not purged
    assert promo.evaluate_auto_purge("INFY", current_price=1501.0, trigger_price=1500.0) is False

    # Price slips up to 1505 (> 1503.00) -> auto-purged
    assert promo.evaluate_auto_purge("INFY", current_price=1505.0, trigger_price=1500.0) is True
    assert promo.shortlist_service.get_trade("INFY") is None


# ==============================================================================
# 3. AlgoLiveWatchdogShorts Math & Evaluation Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_watchdog_shorts_position_direction_math():
    pos = WatchdogPositionShort(
        symbol="TCS",
        security_id="11536",
        entry_price=3500.0,
        quantity=85,
        order_id="ORD-S1",
        action="SELL",
    )
    # Stop loss must be ceiling ABOVE entry (3500 * 1.01 = 3535.0)
    assert pos.hard_stop_loss == 3535.0
    assert pos.current_stop_loss == 3535.0
    assert pos.peak_price == 3500.0

    # Serialization: price falls to 3465 (gain)
    d_gain = pos.to_dict(current_ltp=3465.0)
    assert d_gain["pnl"] == round((3500.0 - 3465.0) * 85, 2)
    assert d_gain["pnl_pct"] == 1.0  # +1.0% profit on short

    # Serialization: price rises to 3535 (loss)
    d_loss = pos.to_dict(current_ltp=3535.0)
    assert d_loss["pnl"] == round((3500.0 - 3535.0) * 85, 2)
    assert d_loss["pnl_pct"] == -1.0  # -1.0% loss on short


@pytest.mark.asyncio
async def test_watchdog_shorts_evaluate_tick_flow():
    mock_mcp = MagicMock()
    mock_mcp.modify_stop_loss = AsyncMock()
    mock_mcp.emergency_market_exit = AsyncMock(return_value={"status": "SUCCESS"})
    mock_tg = MagicMock()
    mock_tg.send_trade_closed_audit = AsyncMock()
    mock_tg.send_trade_approval_alert = AsyncMock()

    shared_slots = CapitalSlotManager()
    watchdog = AlgoLiveWatchdogShorts(
        mcp_client=mock_mcp,
        telegram_service=mock_tg,
        slot_manager=shared_slots,
    )

    pos = watchdog.register_position(
        symbol="TATASTEEL",
        security_id="3499",
        entry_price=100.0,
        quantity=600,
        order_id="ORD-TS",
    )
    # Hard SL is 101.00 (ceiling)
    assert pos.hard_stop_loss == 101.0
    assert shared_slots.get_portfolio_summary()["available_slots_count"] == 2

    # 1. Price rises to 101.05 -> Hard SL Ceiling Hit -> Covers via BUY
    res_sl = await watchdog.evaluate_tick("TATASTEEL", ltp=101.05)
    assert res_sl["action"] == "CLOSED"
    assert res_sl["reason"] == "HARD_1PCT_SL_HIT"
    assert res_sl["pnl"] < 0
    mock_mcp.emergency_market_exit.assert_called_with(
        security_id="3499", quantity=600, symbol_hint="TATASTEEL", transaction_type="BUY"
    )
    assert shared_slots.get_portfolio_summary()["available_slots_count"] == 3
    from db_store import get_lifecycle_positions
    assert not any(p["symbol"] == "TATASTEEL" for p in get_lifecycle_positions())

    # Re-register for Breakeven and Trailing tests
    pos2 = watchdog.register_position(
        symbol="SBIN",
        security_id="3045",
        entry_price=800.0,
        quantity=375,
        order_id="ORD-SB",
    )
    # 2. Dynamic Breakeven Lock at +0.8% gain (price drops to 800 * 0.992 = 793.60)
    # Breakeven SL moves down to entry * (1 - 0.0010) = 800 * 0.999 = 799.20
    res_be = await watchdog.evaluate_tick("SBIN", ltp=793.0)
    assert res_be["action"] == "MODIFY_SL"
    assert res_be["reason"] == "BREAKEVEN_LOCKED"
    assert pos2.breakeven_locked is True
    assert pos2.current_stop_loss == 799.20

    # 3. Structural Resistance Trailing at +1.5% gain (price drops to 788.0)
    # VWAP above price at 792.0 acts as resistance ceiling
    res_trail = await watchdog.evaluate_tick("SBIN", ltp=788.0, vwap_5m=792.0)
    assert res_trail["action"] == "MODIFY_SL"
    assert res_trail["reason"] == "STRUCTURAL_TRAIL"
    assert pos2.current_stop_loss == 792.0  # Ratchets down to 792.0

    # 4. Trailing stop hit: price bounces back UP to 792.50 (>= 792.0)
    res_trail_hit = await watchdog.evaluate_tick("SBIN", ltp=792.50)
    assert res_trail_hit["action"] == "CLOSED"
    assert res_trail_hit["reason"] == "TRAILED_STOP_HIT"
    # Sold short @ 800, covered @ 792.50 -> profit = (800 - 792.50) * 375 = +2812.50
    assert res_trail_hit["pnl"] > 0


# ==============================================================================
# 4. Longs Pipeline Direction Purity Tests
# ==============================================================================

def test_longs_promotion_manager_rejects_bearish():
    promo_long = PromotionManager()
    # PromotionManager should reject bearish setups
    assert promo_long.is_eligible_for_promotion(
        symbol="TCS",
        score=90,
        live_status="CONFIRMED_TRIGGER",
        trigger_price=3500.0,
        current_price=3490.0,
        rsi_5m=40.0,
        rvol=1.5,
        bias="BEARISH_BREAKDOWN",
    ) is False


@pytest.mark.asyncio
async def test_promotion_rejects_stale_persisted_trigger_identity(tmp_path):
    store = LiveTriggerStateStore(tmp_path / "trigger.sqlite")
    store.observe(
        {
            "symbol": "RELIANCE",
            "trigger_price": 2500.0,
            "correlation_id": "corr-live",
        },
        2501.0,
        observed_at=1_000.0,
    )

    common = {
        "symbol": "RELIANCE",
        "score": 90,
        "bias": "BULLISH_BREAKOUT",
        "live_status": "CONFIRMED_TRIGGER",
        "trigger_price": 2500.0,
        "current_price": 2501.0,
        "rsi_5m": 55.0,
        "rvol": 1.5,
        "quote_source": "aggregator",
        "quote_age_ms": 1000,
        "rsi_is_real": True,
        "trigger_correlation_id": "corr-stale",
    }

    with patch("services.live_trigger_state.get_live_trigger_state_store", return_value=store):
        result = await PromotionManager().evaluate_and_promote(**common)

    assert result == {"promoted": False, "reason": "UNVERIFIED_TRIGGER_STATE"}


@pytest.mark.asyncio
async def test_short_promotion_rejects_stale_persisted_trigger_identity(tmp_path):
    store = LiveTriggerStateStore(tmp_path / "trigger.sqlite")
    store.observe(
        {
            "symbol": "TCS",
            "trigger_price": 3500.0,
            "direction": "BEAR",
            "correlation_id": "corr-short-live",
        },
        3499.0,
        observed_at=1_000.0,
    )

    with patch("services.live_trigger_state.get_live_trigger_state_store", return_value=store):
        result = await PromotionManagerShorts().evaluate_and_promote(
            symbol="TCS",
            score=90,
            bias="BEARISH_BREAKDOWN",
            live_status="CONFIRMED_TRIGGER",
            trigger_price=3500.0,
            current_price=3499.0,
            rsi_5m=55.0,
            rvol=1.5,
            quote_source="aggregator",
            quote_age_ms=1000,
            rsi_is_real=True,
            trigger_correlation_id="corr-stale",
        )

    assert result == {"promoted": False, "reason": "UNVERIFIED_TRIGGER_STATE"}


@pytest.mark.asyncio
async def test_watchdog_longs_pure_math():
    mock_mcp = MagicMock()
    mock_mcp.modify_stop_loss = AsyncMock()
    mock_mcp.emergency_market_exit = AsyncMock(return_value={"status": "SUCCESS"})
    mock_tg = MagicMock()
    mock_tg.send_trade_closed_audit = AsyncMock()
    mock_tg.send_trade_approval_alert = AsyncMock()

    shared_slots = CapitalSlotManager()
    watchdog = AlgoLiveWatchdog(
        mcp_client=mock_mcp,
        telegram_service=mock_tg,
        slot_manager=shared_slots,
    )

    pos = watchdog.register_position(
        symbol="INFY",
        security_id="1594",
        entry_price=1000.0,
        quantity=300,
        order_id="ORD-L1",
        action="BUY",
    )
    # Long stop loss is floor BELOW entry: 1000 * 0.99 = 990.0
    assert pos.hard_stop_loss == 990.0

    # Price drops to 989 -> Hard SL floor hit -> Exits with SELL
    res = await watchdog.evaluate_tick("INFY", ltp=989.0)
    assert res["action"] == "CLOSED"
    mock_mcp.emergency_market_exit.assert_called_with(
        security_id="1594", quantity=300, symbol_hint="INFY", transaction_type="SELL"
    )
    from db_store import get_lifecycle_positions
    assert not any(p["symbol"] == "INFY" for p in get_lifecycle_positions())


# ==============================================================================
# 5. Shared Capital Pool (3 Slots Across Longs and Shorts)
# ==============================================================================

@pytest.mark.asyncio
async def test_shared_capital_pool_across_longs_and_shorts():
    shared_slots = CapitalSlotManager()
    
    mock_mcp = MagicMock()
    mock_mcp.modify_stop_loss = AsyncMock()
    mock_mcp.emergency_market_exit = AsyncMock(return_value={"status": "SUCCESS"})
    mock_tg = MagicMock()
    mock_tg.send_trade_closed_audit = AsyncMock()
    mock_tg.send_trade_approval_alert = AsyncMock()

    watchdog_longs = AlgoLiveWatchdog(mcp_client=mock_mcp, telegram_service=mock_tg, slot_manager=shared_slots)
    watchdog_shorts = AlgoLiveWatchdogShorts(mcp_client=mock_mcp, telegram_service=mock_tg, slot_manager=shared_slots)

    assert shared_slots.can_open_new_trade() is True
    assert shared_slots.get_portfolio_summary()["available_slots_count"] == 3

    # Long 1 takes Slot 1
    watchdog_longs.register_position("RELIANCE", "2885", 2500.0, 120, "ORD-L1")
    assert shared_slots.get_portfolio_summary()["available_slots_count"] == 2

    # Short 1 takes Slot 2
    watchdog_shorts.register_position("HDFCBANK", "1333", 1600.0, 187, "ORD-S1")
    assert shared_slots.get_portfolio_summary()["available_slots_count"] == 1

    # Long 2 takes Slot 3
    watchdog_longs.register_position("ICICIBANK", "4963", 1000.0, 300, "ORD-L2")
    assert shared_slots.get_portfolio_summary()["available_slots_count"] == 0
    assert shared_slots.can_open_new_trade() is False

    # Deregister Short 1 -> frees 1 slot for either pipeline
    watchdog_shorts.deregister_position("HDFCBANK")
    assert shared_slots.get_portfolio_summary()["available_slots_count"] == 1
    assert shared_slots.can_open_new_trade() is True

    # Deregister remaining Longs
    watchdog_longs.deregister_position("RELIANCE")
    watchdog_longs.deregister_position("ICICIBANK")
    assert shared_slots.get_portfolio_summary()["available_slots_count"] == 3


@pytest.mark.asyncio
async def test_add_manual_short_trade_geometry_sanitization():
    from routers.shortlist_shorts import add_manual_short_trade, AddManualShortTradeRequest
    from services.live_algo_watchdog_shorts import get_algo_live_watchdog_shorts
    from services.live_algo_watchdog import get_algo_live_watchdog

    watchdog_shorts = get_algo_live_watchdog_shorts()
    watchdog_longs = get_algo_live_watchdog()
    watchdog_shorts.active_positions.clear()
    watchdog_longs.active_positions.clear()

    # Case 1: Inverted targets (e.g. leaked from long commentary: T1=1030, T2=1060 for entry=1000)
    req = AddManualShortTradeRequest(
        symbol="RELIANCE",
        action="SELL",
        entry_price=1000.0,
        stop_loss=1010.0,
        target_1=1030.0,  # Invalid for short (above entry)
        target_2=1060.0,  # Invalid for short (above entry)
        quantity=10,
    )
    res = await add_manual_short_trade(payload=req, user_id="test_user")
    assert res["status"] == "ok"
    assert res["trade"]["action"] == "SELL"
    # Target 1 must be auto-sanitized below entry (e.g. 970)
    assert res["trade"]["target_1"] < 1000.0
    # Target 2 must be auto-sanitized below Target 1 (e.g. 940)
    assert res["trade"]["target_2"] < res["trade"]["target_1"]
    assert res["trade"]["stop_loss"] > 1000.0

    # Clean up
    watchdog_shorts.deregister_position("RELIANCE")


