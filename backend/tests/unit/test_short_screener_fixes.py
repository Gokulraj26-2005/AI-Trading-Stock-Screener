"""
Unit tests for Live Short Screener Engine:
- Singleton lifecycle and state management
- Real-time tick ingestion (LTP, high, low, volume)
- True Wilder 14 RSI calculation and caching
- Dynamic RSI velocity and rapid shift detection (is_rapid_rsi)
- Auto-sorting descending by RSI (highest RSI at top)
- Power toggle ON/OFF states
- FinBERT news catalyst and IFP score integration
"""

import asyncio
import pytest
import time
from services.indicator_cache import _compute_rsi_wilder, clear_indicator_cache
from services.short_screener_engine import (
    ShortScreenerEngine,
    get_short_screener_engine,
    LiveShortStockItem,
)


@pytest.fixture(autouse=True)
def clean_screener_state():
    clear_indicator_cache()
    engine = get_short_screener_engine()
    engine.is_active = False
    yield engine
    engine.is_active = False


def test_singleton_engine_instance():
    """Verify get_short_screener_engine returns identical singleton instance."""
    inst1 = ShortScreenerEngine.get_instance()
    inst2 = get_short_screener_engine()
    assert inst1 is inst2


def test_wilder_rsi_calculation_purity():
    """Verify _compute_rsi_wilder math on known series."""
    # Under 15 closes returns None
    assert _compute_rsi_wilder([100.0] * 10, period=14) is None

    # Constant price returns 50.0 (neutral)
    flat_series = [100.0] * 20
    assert _compute_rsi_wilder(flat_series, period=14) == 50.0

    # Strong monotonic increase returns 100.0
    up_series = [100.0 + i * 2.0 for i in range(25)]
    assert _compute_rsi_wilder(up_series, period=14) == 100.0

    # Strong monotonic decrease returns 0.0
    down_series = [200.0 - i * 2.0 for i in range(25)]
    assert _compute_rsi_wilder(down_series, period=14) == 0.0


def test_tick_ingestion_updates_ltp_and_rsi():
    """Verify tick ingestion updates LTP, high, low, and Wilder RSI."""
    engine = get_short_screener_engine()
    sym = engine.symbols[0]
    st = engine.states[sym]

    # Reset low for pure tick tracking test
    st.low = 0.0

    # Feed initial tick
    engine.on_tick({
        "symbol": sym,
        "ltp": 1500.0,
        "prev_close": 1480.0,
        "volume": 50000,
    })

    assert st.ltp == 1500.0
    assert st.high >= 1500.0
    assert st.low == 1500.0
    assert st.volume == 50000
    assert st.change_pct == 1.35  # ((1500 - 1480) / 1480) * 100

    # Feed rising ticks to trigger Wilder RSI calculation
    base_price = 1500.0
    for i in range(16):
        engine.on_tick({
            "symbol": sym,
            "ltp": base_price + i * 3.0,
            "volume": 55000 + i * 1000,
        })

    assert st.rsi > 70.0  # Overbought from rising ticks


def test_rsi_velocity_and_rapid_shift_flag():
    """Verify RSI velocity and is_rapid_rsi trigger on accelerated shifts."""
    engine = get_short_screener_engine()
    sym = engine.symbols[0]
    st = engine.states[sym]
    now = time.time()

    # Seed RSI history
    st._rsi_history.clear()
    st._rsi_history.append((now - 60.0, 60.0))
    engine._update_symbol_rsi(st, 72.0, now)

    # 12.0 point jump in 1 minute -> rapid shift!
    assert st.rsi == 72.0
    assert st.is_rapid_rsi is True
    assert st.rsi_velocity > 0


def test_auto_sorting_descending_by_rsi():
    """Verify stocks are automatically sorted descending by RSI (highest RSI at top)."""
    # Use isolated engine instance with known symbols
    engine = ShortScreenerEngine(symbols=["RELIANCE", "HDFCBANK", "INFY"])
    engine.is_active = True

    # Artificially set diverse RSIs on symbols
    engine.states["RELIANCE"].rsi = 45.0
    engine.states["HDFCBANK"].rsi = 99.5  # Highest
    engine.states["INFY"].rsi = 68.0

    sorted_stocks = engine.get_sorted_stocks()
    top_symbol = sorted_stocks[0]["symbol"]
    assert top_symbol == "HDFCBANK"
    assert sorted_stocks[0]["rsi"] == 99.5
    assert sorted_stocks[0]["short_rank"] == 1

    # Verify descending ordering
    rsis = [s["rsi"] for s in sorted_stocks]
    for i in range(len(rsis) - 1):
        assert rsis[i] >= rsis[i + 1]


def test_screener_power_toggle():
    """Verify Master Power switch turns ON/OFF cleanly while preserving stock universe."""
    async def _test():
        engine = get_short_screener_engine()

        # Turn ON
        await engine.set_power(True)
        assert engine.is_active is True
        payload_on = engine.get_scan_payload()
        assert payload_on["is_active"] is True
        assert len(payload_on["stocks"]) > 0

        # Turn OFF (pause)
        await engine.set_power(False)
        assert engine.is_active is False
        payload_off = engine.get_scan_payload()
        assert payload_off["is_active"] is False
        # Universe remains fully populated and readable in Standby mode
        assert len(payload_off["stocks"]) > 0

        # Re-enable for subsequent usage
        await engine.set_power(True)
        assert engine.is_active is True

    asyncio.run(_test())


def test_real_universe_warmup_mode():
    """Verify engine warms up real stock data from SQLite with prices, RSIs, and news catalysts."""
    async def _test():
        engine = get_short_screener_engine()

        # Turn ON Live Streaming
        await engine.set_power(True)
        assert engine.is_active is True

        payload = engine.get_scan_payload()
        assert payload["is_active"] is True
        assert len(payload["stocks"]) > 0

        # Verify real attributes are present
        top_stock = payload["stocks"][0]
        assert "ltp" in top_stock
        assert "rsi" in top_stock
        assert "news_catalyst_score" in top_stock
        assert "ifp_score" in top_stock
        assert "is_rapid_rsi" in top_stock

    asyncio.run(_test())


def test_rsi_historical_convergence_and_stitching():
    """Verify RSI calculation converges on 150 bars and stitches prev_close + LTP."""
    engine = get_short_screener_engine()
    st = engine.states.get("GREAVESCOT")
    assert st is not None
    # Verify historical closes are converged (up to 150 bars)
    assert len(st._historical_closes) >= 100

    # Test prev_close and LTP stitching
    hist = list(st._historical_closes)
    prev = st.prev_close
    ltp = st.ltp

    # Feed tick with new LTP
    engine.on_tick({
        "symbol": "GREAVESCOT",
        "ltp": ltp + 1.0,
        "prev_close": prev,
        "volume": st.volume + 100,
    })

    assert st.rsi > 0.0
    assert abs(st.ltp - (ltp + 1.0)) < 0.01


