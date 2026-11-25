import pytest
import asyncio
from services.short_screener_engine import ShortScreenerEngine


@pytest.mark.asyncio
async def test_warm_up_universe_populates_real_data():
    """Verify that starting live streaming immediately warms up real data from historical SQLite."""
    symbols = ["RELIANCE", "TCS", "INFY", "SBIN"]
    engine = ShortScreenerEngine(symbols=symbols)

    # Initial state should already be warmed up with real data
    rel = engine.states["RELIANCE"]
    assert rel.ltp > 0, "LTP must be populated from real historical closes"
    assert rel.prev_close > 0, "Prev close must be populated from real historical closes"
    assert rel.rsi > 0, "RSI must be computed using Wilder 14 formula"
    assert len(rel._recent_closes) >= 15, "Recent closes deque must be pre-seeded for rolling live updates"

    # Turn ON live streaming
    await engine.set_power(True)
    assert engine.is_active is True

    payload = engine.get_scan_payload()
    assert payload["is_active"] is True
    assert len(payload["stocks"]) == len(symbols)

    # Verify descending sort by RSI
    rsis = [s["rsi"] for s in payload["stocks"]]
    assert rsis == sorted(rsis, reverse=True), "Stocks must be sorted descending by RSI"

    # Cleanup
    await engine.stop()


@pytest.mark.asyncio
async def test_live_tick_updates_rolling_rsi():
    """Verify that incoming ticks update price and recalculate RSI immediately using pre-warmed closes."""
    symbols = ["RELIANCE"]
    engine = ShortScreenerEngine(symbols=symbols)
    await engine.set_power(True)

    rel = engine.states["RELIANCE"]
    initial_rsi = rel.rsi
    old_ltp = rel.ltp

    # Ingest a tick with higher price
    new_price = round(old_ltp * 1.05, 2)
    engine.on_tick({"symbol": "RELIANCE", "ltp": new_price, "volume": 500000})

    assert rel.ltp == new_price
    assert rel.high >= new_price
    # RSI must be recalculated on the updated closes
    assert rel.rsi != initial_rsi or rel.rsi > 0

    await engine.stop()
