"""
Integration tests for Live Short Screener REST routes and engine pipeline.
"""

import pytest
from services.short_screener_engine import get_short_screener_engine


@pytest.fixture(autouse=True)
def setup_screener_universe():
    engine = get_short_screener_engine()
    original_symbols = list(engine.symbols)
    engine.initialize(["RELIANCE", "TCS", "BPCL", "HINDPETRO", "IOC"])
    engine.is_active = False
    yield engine
    engine.initialize(original_symbols)
    engine.is_active = False


def test_screener_scan_and_stocks_endpoints(client, session_headers):
    # Standby mode check
    response = client.get("/api/short-screener/stocks", headers=session_headers)
    assert response.status_code == 200
    data = response.json()
    assert "is_active" in data
    assert data["is_active"] is False
    assert "stocks" in data
    assert data["stocks"] == []

    # Alias /scan
    scan_res = client.get("/api/short-screener/scan", headers=session_headers)
    assert scan_res.status_code == 200
    assert scan_res.json()["is_active"] is False


def test_screener_power_toggle_flow(client, session_headers):
    # Initial power state
    pow_res = client.get("/api/short-screener/power", headers=session_headers)
    assert pow_res.status_code == 200
    assert pow_res.json()["is_active"] is False

    # Turn ON
    turn_on = client.post("/api/short-screener/power", headers=session_headers, json={"active": True})
    assert turn_on.status_code == 200
    assert turn_on.json()["is_active"] is True

    # Check scan endpoint now reflects is_active = True and returns stocks list
    stocks_res = client.get("/api/short-screener/stocks", headers=session_headers).json()
    assert stocks_res["is_active"] is True
    assert len(stocks_res["stocks"]) > 0

    # Turn back OFF
    turn_off = client.post("/api/short-screener/power", headers=session_headers, json={"active": False})
    assert turn_off.status_code == 200
    assert turn_off.json()["is_active"] is False

    # In standby mode, stocks list must be strictly empty
    stocks_off = client.get("/api/short-screener/stocks", headers=session_headers).json()
    assert stocks_off["is_active"] is False
    assert stocks_off["stocks"] == []


def test_screener_real_stocks_live_flow(client, session_headers):
    # Turn ON live streaming
    turn_on = client.post("/api/short-screener/power", headers=session_headers, json={"active": True})
    assert turn_on.status_code == 200
    assert turn_on.json()["is_active"] is True

    # Scan reflects is_active = True and returns populated real stocks
    scan_res = client.get("/api/short-screener/stocks", headers=session_headers).json()
    assert scan_res["is_active"] is True
    assert len(scan_res["stocks"]) > 0

    first_stock = scan_res["stocks"][0]
    assert first_stock["ltp"] > 0
    assert first_stock["prev_close"] > 0
    assert first_stock["rsi"] > 0
    assert "news_catalyst_score" in first_stock
    assert "ifp_score" in first_stock

    # Verify descending RSI sort
    rsis = [s["rsi"] for s in scan_res["stocks"]]
    assert rsis == sorted(rsis, reverse=True)

    # Turn OFF live streaming
    turn_off = client.post("/api/short-screener/power", headers=session_headers, json={"active": False})
    assert turn_off.status_code == 200
    assert turn_off.json()["is_active"] is False

