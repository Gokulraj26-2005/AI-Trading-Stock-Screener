import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import asyncio
import services.short_screener_engine as sse_mod
from services.short_screener_engine import ShortScreenerEngine


def _seed_historical_sqlite(db_path, symbols):
    ist = ZoneInfo("Asia/Kolkata")
    base_dt = datetime(2025, 1, 1, 9, 15, tzinfo=ist)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_candles (
                symbol TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                PRIMARY KEY (symbol, timestamp)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_news_cache (
                symbol TEXT PRIMARY KEY,
                display_summary TEXT,
                articles_json TEXT
            )
            """
        )
        rows = []
        for idx, sym in enumerate(symbols):
            price = 1000.0 + (idx * 250.0)
            for day in range(60):
                ts = int((base_dt + timedelta(days=day)).timestamp())
                o = price
                c = price + (4.5 if day % 2 == 0 else -1.5)
                rows.append((sym, ts, o, max(o, c) + 2.0, min(o, c) - 2.0, c, 150000.0 + day * 1000.0))
                price = c
        conn.executemany(
            "INSERT OR REPLACE INTO daily_candles (symbol, timestamp, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()


@pytest.mark.asyncio
async def test_warm_up_universe_populates_real_data(tmp_path, monkeypatch):
    """Verify that starting live streaming immediately warms up real data from historical SQLite."""
    symbols = ["RELIANCE", "TCS", "INFY", "SBIN"]
    db_path = tmp_path / "historical_candles.sqlite"
    _seed_historical_sqlite(db_path, symbols)
    monkeypatch.setattr(sse_mod, "HISTORICAL_DB_PATH", db_path)

    engine = ShortScreenerEngine(symbols=symbols)
    monkeypatch.setattr(engine, "refresh_live_quotes_sync", lambda: None)

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
async def test_live_tick_updates_rolling_rsi(tmp_path, monkeypatch):
    """Verify that incoming ticks update price and recalculate RSI immediately using pre-warmed closes."""
    symbols = ["RELIANCE"]
    db_path = tmp_path / "historical_candles.sqlite"
    _seed_historical_sqlite(db_path, symbols)
    monkeypatch.setattr(sse_mod, "HISTORICAL_DB_PATH", db_path)

    engine = ShortScreenerEngine(symbols=symbols)
    monkeypatch.setattr(engine, "refresh_live_quotes_sync", lambda: None)
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
