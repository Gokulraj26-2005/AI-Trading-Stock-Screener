"""
Dhan Binary Packet Parser Tests
================================
Tests the standard feed header decoder, ticker parser, quote parser, full packet parser,
and depth parser using deterministic binary fixtures.

Runs without any external dependencies (ZMQ, CVD, etc.) — all engines are mocked.
"""
import asyncio
import struct
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure project paths are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fixtures.dhan_packets import (
    TICKER_PACKET, TICKER_SECURITY_ID, TICKER_LTP,
    QUOTE_PACKET, QUOTE_SECURITY_ID, QUOTE_LTP,
    FULL_PACKET, FULL_SECURITY_ID, FULL_LTP, FULL_LTQ, FULL_LTT,
    FULL_AVG_PRICE, FULL_VOLUME, FULL_TOTAL_SELL_QTY, FULL_TOTAL_BUY_QTY,
    FULL_OI, FULL_HIGH_OI, FULL_LOW_OI,
    FULL_OPEN, FULL_CLOSE, FULL_HIGH, FULL_LOW,
    FULL_BIDS, FULL_ASKS,
    MALFORMED_TOO_SHORT, MALFORMED_UNKNOWN_CODE,
    INDEX_TICKER_PACKET, INDEX_SECURITY_ID, INDEX_LTP,
    DEPTH_BID_PACKET, DEPTH_ASK_PACKET, DEPTH_SECURITY_ID,
    DEPTH_BID_LEVELS, DEPTH_ASK_LEVELS,
    OI_PACKET, PREV_CLOSE_PACKET,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_engines():
    """Create mock ZMQ publisher, CVD engine, and regime engine."""
    zmq_pub = MagicMock()
    zmq_pub.publish_tick = AsyncMock()
    zmq_pub.publish = AsyncMock()

    cvd_eng = MagicMock()
    cvd_result = MagicMock()
    cvd_result.tick_volume = 10
    cvd_result.cvd = 0.5
    cvd_result.obi = 0.1
    cvd_eng.process_tick.return_value = cvd_result

    regime_eng = MagicMock()
    regime_eng.set_india_vix = MagicMock()

    return zmq_pub, cvd_eng, regime_eng


@pytest.fixture
def dhan_ws(mock_engines):
    """Create a DhanWebSocket with mocked engines."""
    zmq_pub, cvd_eng, regime_eng = mock_engines

    with patch("app.services.dhan_adapter.get_zmq_publisher", return_value=zmq_pub), \
         patch("app.services.dhan_adapter.get_cvd_engine", return_value=cvd_eng), \
         patch("app.services.dhan_adapter.get_regime_engine", return_value=regime_eng):
        from app.services.dhan_adapter import DhanWebSocket
        ws = DhanWebSocket(client_id="TEST", access_token="test_token")
        ws.id_to_symbol = {
            str(TICKER_SECURITY_ID): "RELIANCE",
            str(QUOTE_SECURITY_ID): "HDFCBANK",
            str(FULL_SECURITY_ID): "TCS",
            str(INDEX_SECURITY_ID): "NIFTY 50",
        }
        ws.nifty50_security_id = str(INDEX_SECURITY_ID)
        return ws, zmq_pub, cvd_eng, regime_eng


@pytest.fixture
def dhan_depth_ws(mock_engines):
    """Create a DhanDepthWebSocket with mocked engines."""
    zmq_pub, cvd_eng, regime_eng = mock_engines

    with patch("app.services.dhan_adapter.get_zmq_publisher", return_value=zmq_pub):
        from app.services.dhan_adapter import DhanDepthWebSocket
        ws = DhanDepthWebSocket(client_id="TEST", access_token="test_token", depth_tier=20)
        ws.id_to_symbol = {str(DEPTH_SECURITY_ID): "RELIANCE"}
        return ws, zmq_pub


# ── Header Parsing ────────────────────────────────────────────────────────────

class TestStandardHeader:
    def test_header_parses_correctly(self, dhan_ws):
        ws, _, _, _ = dhan_ws
        code, length, seg, sec_id = ws._parse_standard_header(TICKER_PACKET)
        assert code == 2                      # ResponseCode.TICKER
        assert sec_id == TICKER_SECURITY_ID   # 2885
        assert seg == 1                       # NSE_EQ

    def test_header_on_full_packet(self, dhan_ws):
        ws, _, _, _ = dhan_ws
        code, length, seg, sec_id = ws._parse_standard_header(FULL_PACKET)
        assert code == 8                      # ResponseCode.FULL
        assert sec_id == FULL_SECURITY_ID     # 11536

    def test_header_on_index_packet(self, dhan_ws):
        ws, _, _, _ = dhan_ws
        code, length, seg, sec_id = ws._parse_standard_header(INDEX_TICKER_PACKET)
        assert code == 2                      # Ticker
        assert sec_id == INDEX_SECURITY_ID    # 13
        assert seg == 0                       # IDX_I segment


# ── Ticker Parser ─────────────────────────────────────────────────────────────

class TestTickerParser:
    @pytest.mark.asyncio
    async def test_ticker_extracts_correct_ltp(self, dhan_ws):
        ws, zmq_pub, _, _ = dhan_ws
        await ws._parse_binary_message(TICKER_PACKET)

        zmq_pub.publish_tick.assert_called_once()
        payload = zmq_pub.publish_tick.call_args[0][0]
        assert payload["symbol"] == "RELIANCE"
        assert abs(payload["ltp"] - TICKER_LTP) < 0.05
        assert payload["token"] == TICKER_SECURITY_ID

    @pytest.mark.asyncio
    async def test_index_ticker_sets_regime(self, dhan_ws):
        ws, zmq_pub, _, regime_eng = dhan_ws
        await ws._parse_binary_message(INDEX_TICKER_PACKET)

        # Nifty 50 ticker published as index
        zmq_pub.publish_tick.assert_called_once()
        payload = zmq_pub.publish_tick.call_args[0][0]
        assert payload["symbol"] == "NIFTY 50"
        assert payload["is_index"] is True
        assert abs(payload["ltp"] - INDEX_LTP) < 0.05


# ── Quote Parser ──────────────────────────────────────────────────────────────

class TestQuoteParser:
    @pytest.mark.asyncio
    async def test_quote_extracts_fields_and_updates_cache(self, dhan_ws):
        ws, zmq_pub, cvd_eng, _ = dhan_ws
        await ws._parse_binary_message(QUOTE_PACKET)

        zmq_pub.publish_tick.assert_called_once()
        payload = zmq_pub.publish_tick.call_args[0][0]
        assert payload["symbol"] == "HDFCBANK"
        assert abs(payload["ltp"] - QUOTE_LTP) < 0.05
        assert payload["token"] == QUOTE_SECURITY_ID

        # Verify latest_quotes cache updated
        assert "HDFCBANK" in ws.latest_quotes
        quote_entry = ws.latest_quotes["HDFCBANK"]
        assert quote_entry["security_id"] == str(QUOTE_SECURITY_ID)
        assert abs(quote_entry["ltp"] - QUOTE_LTP) < 0.05
        assert quote_entry["volume"] == 5_432_100


# ── Full Packet Parser ───────────────────────────────────────────────────────

class TestFullParser:
    @pytest.mark.asyncio
    async def test_full_extracts_ohlcv_and_oi(self, dhan_ws):
        ws, zmq_pub, cvd_eng, _ = dhan_ws
        await ws._parse_binary_message(FULL_PACKET)

        zmq_pub.publish_tick.assert_called_once()
        payload = zmq_pub.publish_tick.call_args[0][0]
        assert payload["symbol"] == "TCS"
        assert abs(payload["ltp"] - FULL_LTP) < 0.05
        assert payload["volume"] == FULL_VOLUME
        assert abs(payload["open"] - FULL_OPEN) < 0.05
        assert abs(payload["high"] - FULL_HIGH) < 0.05
        assert abs(payload["low"] - FULL_LOW) < 0.05
        assert abs(payload["close"] - FULL_CLOSE) < 0.05
        assert abs(payload["avg_price"] - FULL_AVG_PRICE) < 0.05
        assert payload["oi"] == FULL_OI
        assert payload["high_oi"] == FULL_HIGH_OI
        assert payload["low_oi"] == FULL_LOW_OI

    @pytest.mark.asyncio
    async def test_full_passes_to_cvd(self, dhan_ws):
        ws, zmq_pub, cvd_eng, _ = dhan_ws
        await ws._parse_binary_message(FULL_PACKET)

        cvd_eng.process_tick.assert_called_once()
        call_kwargs = cvd_eng.process_tick.call_args
        assert call_kwargs.kwargs.get("instrument_token") == FULL_SECURITY_ID or \
               call_kwargs[1].get("instrument_token") == FULL_SECURITY_ID

    @pytest.mark.asyncio
    async def test_full_parses_5level_depth_and_updates_cache(self, dhan_ws):
        ws, zmq_pub, cvd_eng, _ = dhan_ws
        await ws._parse_binary_message(FULL_PACKET)

        payload = zmq_pub.publish_tick.call_args[0][0]
        bid_price = FULL_BIDS[0][1]  # 3921.50
        ask_price = FULL_ASKS[0][1]  # 3922.00
        assert abs(payload["best_bid"] - bid_price) < 0.05
        assert abs(payload["best_ask"] - ask_price) < 0.05

        # Verify latest_quotes contains spread_pct and obi
        assert "TCS" in ws.latest_quotes
        entry = ws.latest_quotes["TCS"]
        assert entry["security_id"] == str(FULL_SECURITY_ID)
        assert entry["spread_pct"] is not None
        assert entry["spread_pct"] > 0
        assert entry["obi"] is not None


# ── Malformed Packet Handling ─────────────────────────────────────────────────

class TestMalformedPackets:
    @pytest.mark.asyncio
    async def test_too_short_packet_ignored(self, dhan_ws):
        ws, zmq_pub, _, _ = dhan_ws
        await ws._parse_binary_message(MALFORMED_TOO_SHORT)
        zmq_pub.publish_tick.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_code_logged_not_crash(self, dhan_ws):
        ws, zmq_pub, _, _ = dhan_ws
        await ws._parse_binary_message(MALFORMED_UNKNOWN_CODE)
        zmq_pub.publish_tick.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_packet_ignored(self, dhan_ws):
        ws, zmq_pub, _, _ = dhan_ws
        await ws._parse_binary_message(b"")
        zmq_pub.publish_tick.assert_not_called()


# ── Response Code Dispatch ───────────────────────────────────────────────────

class TestResponseCodeRouting:
    @pytest.mark.asyncio
    async def test_full_packet_routes_correctly(self, dhan_ws):
        ws, zmq_pub, _, _ = dhan_ws
        await ws._parse_binary_message(FULL_PACKET)
        zmq_pub.publish_tick.assert_called()
        payload = zmq_pub.publish_tick.call_args[0][0]
        assert payload["token"] == FULL_SECURITY_ID
        assert abs(payload["ltp"] - FULL_LTP) < 0.05

    @pytest.mark.asyncio
    async def test_ticker_packet_routes_correctly(self, dhan_ws):
        ws, zmq_pub, _, _ = dhan_ws
        await ws._parse_binary_message(TICKER_PACKET)
        zmq_pub.publish_tick.assert_called()
        payload = zmq_pub.publish_tick.call_args[0][0]
        assert payload["token"] == TICKER_SECURITY_ID
        assert abs(payload["ltp"] - TICKER_LTP) < 0.05

    @pytest.mark.asyncio
    async def test_oi_and_prev_close_handled_silently(self, dhan_ws):
        ws, zmq_pub, _, _ = dhan_ws
        await ws._parse_binary_message(OI_PACKET)
        await ws._parse_binary_message(PREV_CLOSE_PACKET)
        zmq_pub.publish_tick.assert_not_called()


# ── Depth Parser ──────────────────────────────────────────────────────────────

class TestDepthParser:
    @pytest.mark.asyncio
    async def test_bid_packet_parsed(self, dhan_depth_ws):
        ws, zmq_pub = dhan_depth_ws
        await ws._parse_depth_packet(DEPTH_BID_PACKET)

        snapshot = ws.depth_snapshots.get(str(DEPTH_SECURITY_ID))
        assert snapshot is not None
        assert len(snapshot.bids) == 20
        # First bid level
        assert snapshot.bids[0].quantity == DEPTH_BID_LEVELS[0][0]
        assert abs(snapshot.bids[0].price - DEPTH_BID_LEVELS[0][1]) < 0.05
        assert snapshot.bids[0].orders == DEPTH_BID_LEVELS[0][2]

    @pytest.mark.asyncio
    async def test_ask_packet_completes_snapshot(self, dhan_depth_ws):
        ws, zmq_pub = dhan_depth_ws
        await ws._parse_depth_packet(DEPTH_BID_PACKET)
        await ws._parse_depth_packet(DEPTH_ASK_PACKET)

        snapshot = ws.depth_snapshots.get(str(DEPTH_SECURITY_ID))
        assert snapshot is not None
        assert len(snapshot.bids) == 20
        assert len(snapshot.asks) == 20
        assert snapshot.deep_obi != 0.0
        zmq_pub.publish.assert_called()
        topic = zmq_pub.publish.call_args[0][0]
        assert topic == "DEPTH_RELIANCE"

    @pytest.mark.asyncio
    async def test_depth_payload_not_truncated(self, dhan_depth_ws):
        ws, zmq_pub = dhan_depth_ws
        await ws._parse_depth_packet(DEPTH_BID_PACKET)
        await ws._parse_depth_packet(DEPTH_ASK_PACKET)

        payload = zmq_pub.publish.call_args[0][1]
        assert len(payload["bids"]) == 20
        assert len(payload["asks"]) == 20


# ── Subscription Batching ─────────────────────────────────────────────────────

class TestSubscriptionBatching:
    def test_single_batch_under_100(self, dhan_ws):
        ws, _, _, _ = dhan_ws
        ws._instrument_list = [{"ExchangeSegment": "NSE_EQ", "SecurityId": str(i)} for i in range(50)]
        messages = ws._build_subscription_messages()
        assert len(messages) == 1

    def test_multiple_batches_over_100(self, dhan_ws):
        ws, _, _, _ = dhan_ws
        ws._instrument_list = [{"ExchangeSegment": "NSE_EQ", "SecurityId": str(i)} for i in range(250)]
        messages = ws._build_subscription_messages()
        assert len(messages) == 3

    def test_batch_sizes_correct(self, dhan_ws):
        ws, _, _, _ = dhan_ws
        ws._instrument_list = [{"ExchangeSegment": "NSE_EQ", "SecurityId": str(i)} for i in range(250)]
        messages = ws._build_subscription_messages()
        import json
        counts = [json.loads(m)["InstrumentCount"] for m in messages]
        assert counts == [100, 100, 50]


# ── Auth URL ──────────────────────────────────────────────────────────────────

class TestAuthUrl:
    def test_url_includes_version_2(self, dhan_ws):
        ws, _, _, _ = dhan_ws
        url = ws._build_auth_url("wss://api-feed.dhan.co")
        assert "version=2" in url
        assert "token=test_token" in url
        assert "clientId=TEST" in url
        assert "authType=2" in url
