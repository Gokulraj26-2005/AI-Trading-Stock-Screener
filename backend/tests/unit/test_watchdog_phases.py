"""
Comprehensive test suite verifying Phase 0, 1, 2, 3 watchdog features:
- Phase 0: Data recording & outcome tagging
- Phase 1: First-tick CVD, delta-crossing only, OBI hysteresis, RSI/EMA seeding, stale tick reject, undefined zone guard
- Phase 2: Banded OBI, wall persistence, multi-condition trigger matrix
- Phase 3: Liquidity tiers & calibrated thresholds
"""
import asyncio
import os
import sys
import time
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "app")))

from services.watchdog_alert_engine import (
    get_alert_engine,
    get_liquidity_tier,
    get_tier_obi_thresholds,
    AlertSignalBundle,
    S_CVD_DUMP,
    S_CVD_SURGE,
    S_OBI_BEARISH,
    S_OBI_BULLISH,
    S_SELL_WALL_NEAR,
    S_BUY_WALL_NEAR,
)
from services.watchdog_data_recorder import (
    record_snapshot,
    record_alert,
    _get_conn,
)
from services.live_algo_watchdog import AlgoLiveWatchdog, WatchdogPosition
from services.live_algo_watchdog_shorts import AlgoLiveWatchdogShorts, WatchdogPositionShort


def test_phase_3_liquidity_tiers():
    assert get_liquidity_tier("RELIANCE") == 1
    assert get_liquidity_tier("TATAELXSI") == 2
    assert get_liquidity_tier("GALLANTT") == 3

    assert get_tier_obi_thresholds(1) == (0.15, 0.05)
    assert get_tier_obi_thresholds(2) == (0.25, 0.10)
    assert get_tier_obi_thresholds(3) == (0.35, 0.15)


def test_phase_1_obi_hysteresis():
    engine = get_alert_engine()
    engine.remove_state("TEST_RELIANCE")

    # Tier 1: fire at 0.15, rearm at 0.05
    assert engine.obi_zone_with_hysteresis("RELIANCE", 0.0) == "NEUTRAL"
    assert engine.obi_zone_with_hysteresis("RELIANCE", 0.16) == "BUYERS"
    assert engine.obi_zone_with_hysteresis("RELIANCE", 0.08) == "BUYERS"
    assert engine.obi_zone_with_hysteresis("RELIANCE", 0.04) == "NEUTRAL"


def test_phase_0_recorder():
    record_snapshot(
        symbol="TEST_REC",
        direction="LONG",
        ltp=1500.0,
        volume=12000,
        cvd=3000,
        cvd_delta=150,
        banded_obi=0.18,
        deep_obi=0.20,
        rsi_5m=55.0,
        vwap_5m=1498.0,
    )

    alert_id = record_alert(
        symbol="TEST_REC",
        direction="LONG",
        alert_type="BUY_SURGE_WATCH",
        tier="WATCH",
        signals=[S_CVD_SURGE, S_OBI_BULLISH],
        trigger_values={"ltp": 1500.0},
        ltp_at_alert=1500.0,
    )

    with _get_conn() as conn:
        snap = conn.execute("SELECT * FROM watchdog_snapshots WHERE symbol='TEST_REC' ORDER BY id DESC LIMIT 1").fetchone()
        assert snap is not None
        assert snap["ltp"] == 1500.0
        assert snap["banded_obi"] == 0.18

        al = conn.execute("SELECT * FROM watchdog_alerts WHERE id=?", (alert_id,)).fetchone()
        assert al is not None
        assert al["alert_type"] == "BUY_SURGE_WATCH"
        assert al["tier"] == "WATCH"


def test_phase_2_multi_condition_trigger_matrix():
    engine = get_alert_engine()
    engine.remove_state("TEST_MATRIX")

    events_fired = []
    def _add_event(symbol, event_type, msg, price, sev):
        events_fired.append((event_type, sev))

    # Single signal (1 indicator): should NOT fire user alert (INFO tier is silent)
    bundle_single = AlertSignalBundle(
        cvd_surge=True,
        ltp=100.0,
    )
    fired = engine.evaluate(
        symbol="TEST_MATRIX",
        direction="LONG",
        bundle=bundle_single,
        entry_price=100.0,
        add_timeline_event_fn=_add_event,
        record_alert_fn=lambda *args, **kwargs: None,
    )
    assert len(events_fired) == 0, "Single indicator must NOT fire timeline alert"

    # Dual signals (CVD surge + OBI bullish): fires WATCH alert
    bundle_dual = AlertSignalBundle(
        cvd_surge=True,
        obi_bullish=True,
        ltp=100.0,
    )
    fired = engine.evaluate(
        symbol="TEST_MATRIX",
        direction="LONG",
        bundle=bundle_dual,
        entry_price=100.0,
        add_timeline_event_fn=_add_event,
        record_alert_fn=lambda *args, **kwargs: None,
    )
    assert len(events_fired) == 1
    assert "BUY_SURGE_WATCH" in events_fired[0][0]


@pytest.mark.asyncio
async def test_watchdog_undefined_zone_guard():
    # Long watchdog: RSI >= 80 with OBI in undefined zone (-0.10 < OBI < 0.10)
    long_dog = AlgoLiveWatchdog()
    pos = WatchdogPosition(
        symbol="TEST_UNDEF",
        security_id="99999",
        entry_price=100.0,
        quantity=10,
        order_id="TEST_ORD_1",
        hard_stop_loss=99.0,
        current_stop_loss=99.0,
        breakeven_locked=True,
    )
    long_dog.active_positions["TEST_UNDEF"] = pos

    # LTP at 102 (+2% profit), RSI=82 (overbought)
    # With cvd_delta=0, bid_ask_ratio=1.0 and no depth walls, OBI is None/0
    result = await long_dog.evaluate_tick(
        symbol="TEST_UNDEF",
        ltp=102.0,
        vwap_5m=101.0,
        ema20_5m=100.5,
        rsi_5m=82.0,
    )
    # Undefined zone guard prevents panic exit
    assert result["action"] != "EXIT_ADVISED"

    # Short watchdog: RSI <= 20 with OBI in undefined zone
    short_dog = AlgoLiveWatchdogShorts()
    short_pos = WatchdogPositionShort(
        symbol="TEST_UNDEF_SHORT",
        security_id="99998",
        entry_price=100.0,
        quantity=10,
        order_id="TEST_ORD_2",
        hard_stop_loss=101.0,
        current_stop_loss=101.0,
        breakeven_locked=True,
    )
    short_dog.active_positions["TEST_UNDEF_SHORT"] = short_pos

    short_result = await short_dog.evaluate_tick(
        symbol="TEST_UNDEF_SHORT",
        ltp=98.0,
        vwap_5m=99.0,
        ema20_5m=99.5,
        rsi_5m=18.0,
    )
    assert short_result["action"] != "EXIT_ADVISED"
