"""
Integration tests for Chart Analysis API Router.
"""

import pytest
from fastapi.testclient import TestClient



def test_get_analysis_symbols(client):
    response = client.get(
        "/api/analysis/symbols?universe=nifty100",
        headers={"Authorization": "Bearer dev-preview-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "count" in data
    assert data["count"] >= 50
    assert "symbols" in data
    assert any(s["symbol"] == "RELIANCE" for s in data["symbols"])
    assert any(s["symbol"] == "HDFCBANK" for s in data["symbols"])


def test_get_symbol_analysis(client, monkeypatch):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    import services.multi_tf_analysis as mta

    ist = ZoneInfo("Asia/Kolkata")
    start_dt = datetime(2025, 1, 1, 9, 15, tzinfo=ist)
    bars = []
    price = 3500.0
    for i in range(60):
        dt = start_dt + timedelta(days=i)
        o = price
        c = price + (8.0 if i % 2 == 0 else -4.0)
        bars.append(
            mta.CandleBar(
                time=int(dt.timestamp()),
                open=o,
                high=max(o, c) + 5.0,
                low=min(o, c) - 5.0,
                close=c,
                volume=100000.0 + i * 500.0,
            )
        )
        price = c

    service = mta.get_multi_tf_analysis_service()

    async def _fake_daily_candles(symbol, security_id=None, days_back=1500, force_refresh=False):
        return list(bars)

    async def _fake_news(symbol, force_refresh=False, cache_instance=None):
        return "", []

    monkeypatch.setattr(service, "fetch_or_generate_daily_candles", _fake_daily_candles)
    monkeypatch.setattr(mta, "fetch_stock_news_24h", _fake_news)

    response = client.get(
        "/api/analysis/TCS",
        headers={"Authorization": "Bearer dev-preview-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "TCS"
    assert "current_price" in data
    assert "timeframes" in data
    assert "1D" in data["timeframes"]
    assert "1W" in data["timeframes"]
    assert "1M" in data["timeframes"]
    assert len(data["timeframes"]["1D"]["candles"]) > 0
    assert "commentary" in data
    assert "potential_score" in data["commentary"]
    assert "actionable_thesis" in data["commentary"]
    assert "price_lines" in data
    assert "markers" in data


def test_get_symbol_analysis_intraday_mode(client):
    response = client.get(
        "/api/analysis/RELIANCE?mode=intraday",
        headers={"Authorization": "Bearer dev-preview-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "RELIANCE"
    assert data["mode"] == "intraday"
    assert "current_price" in data
    assert "vwap" in data
    assert "candles_5m" in data
    assert len(data["candles_5m"]) > 0
    assert "vwap_series" in data


def test_get_symbol_intraday_endpoint(client):
    response = client.get(
        "/api/analysis/intraday/INFY",
        headers={"Authorization": "Bearer dev-preview-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "INFY"
    assert data["mode"] == "intraday"
    assert "vwap" in data
    assert "candles_5m" in data


def test_rsi_wilder_flat_series_returns_neutral():
    from routers.analysis import _compute_rsi_wilder
    flat_closes = [100.0] * 20
    rsi = _compute_rsi_wilder(flat_closes, period=14)
    assert rsi == 50.0


def test_compute_normalized_rvol():
    from routers.analysis import _compute_normalized_rvol
    rvol = _compute_normalized_rvol(day_volume=50000, avg_daily_volume=100000)
    assert rvol is not None
    assert rvol > 0.0


def test_get_live_ticks_provenance(client):
    response = client.post(
        "/api/analysis/live-ticks",
        json={"symbols": ["RELIANCE", "TCS"]},
        headers={"Authorization": "Bearer dev-preview-token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "ticks" in data
    for sym in ["RELIANCE", "TCS"]:
        if sym in data["ticks"]:
            tick = data["ticks"][sym]
            assert "source" in tick
            assert tick["source"] in ("aggregator", "rest_cache", "synthetic")
            assert "observed_at" in tick
            assert "quote_age_ms" in tick

