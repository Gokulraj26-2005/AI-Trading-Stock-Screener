"""
Dhan Deep Market Depth Tests
============================
Tests the Dhan 20/200-level depth feed parser, DeepDepthSnapshot OBI calculation,
connection limit enforcement without silent truncation, dynamic subscription refresh,
packet validation, and factory validation.
"""
import struct
import pytest
from unittest.mock import MagicMock, AsyncMock

from app.services.dhan_adapter import (
    DhanDepthWebSocket,
    DeepDepthSnapshot,
    DepthLevel,
    get_dhan_depth_ticker,
)


@pytest.fixture(autouse=True)
def mock_zmq(monkeypatch):
    mock = MagicMock()
    mock.publish = AsyncMock()
    monkeypatch.setattr("app.services.dhan_adapter.get_zmq_publisher", lambda: mock)
    return mock


def _build_depth_packet(
    feed_code: int,  # 41=bid, 51=ask
    security_id: int,
    levels: list,    # list of (qty, price_float, orders)
    seq: int = 1,
) -> bytes:
    """Build a synthetic Dhan depth binary packet (12-byte header + 16-byte levels: <dII)."""
    header = struct.pack("<HBBII", 12 + len(levels) * 16, feed_code, 1, security_id, seq)
    body = bytearray()
    for qty, price, orders in levels:
        body.extend(struct.pack("<dII", float(price), int(qty), int(orders)))
    return bytes(header + body)


class TestDepthPacketParser:
    """Test 20-level depth binary packet decoding."""

    def setup_method(self):
        self.depth_ws = DhanDepthWebSocket("client_123", "token_abc", depth_tier=20)
        self.depth_ws.id_to_symbol = {"1333": "HDFCBANK"}
        self.depth_ws.zmq_publisher = MagicMock()
        self.depth_ws.zmq_publisher.publish = AsyncMock()

    @pytest.mark.asyncio
    async def test_parse_bid_depth_packet(self):
        """Feed code 41 (bids) correctly updates snapshot bids and total_bid_qty."""
        bid_levels = [
            (500, 1650.00, 10),
            (300, 1649.50, 8),
            (200, 1649.00, 5),
        ]
        packet = _build_depth_packet(41, 1333, bid_levels)

        await self.depth_ws._parse_depth_packet(packet)

        snapshot = self.depth_ws.depth_snapshots.get("1333")
        assert snapshot is not None
        assert snapshot.is_valid is True
        assert len(snapshot.bids) == 3
        assert snapshot.total_bid_qty == 1000
        assert abs(snapshot.bids[0].price - 1650.00) < 0.01
        assert snapshot.bids[0].quantity == 500
        assert snapshot.bids[0].orders == 10

    @pytest.mark.asyncio
    async def test_parse_ask_depth_and_compute_deep_obi(self):
        """Receiving both bid (41) and ask (51) computes correct deep_obi."""
        bids = [(600, 1650.00, 12)]  # total_bid = 600
        asks = [(400, 1650.50, 8)]   # total_ask = 400
        # Expected deep_obi = (600 - 400) / (600 + 400) = 200 / 1000 = +0.20

        bid_packet = _build_depth_packet(41, 1333, bids)
        ask_packet = _build_depth_packet(51, 1333, asks)

        await self.depth_ws._parse_depth_packet(bid_packet)
        await self.depth_ws._parse_depth_packet(ask_packet)

        snapshot = self.depth_ws.depth_snapshots.get("1333")
        assert snapshot is not None
        assert snapshot.is_valid is True
        assert snapshot.total_bid_qty == 600
        assert snapshot.total_ask_qty == 400
        assert abs(snapshot.deep_obi - 0.20) < 1e-4

        # Verify publication
        self.depth_ws.zmq_publisher.publish.assert_called()
        last_call = self.depth_ws.zmq_publisher.publish.call_args
        topic, payload = last_call[0]
        assert topic == "DEPTH_HDFCBANK"
        assert payload["deep_obi"] == snapshot.deep_obi
        assert payload["total_bid_qty"] == 600
        assert payload["total_ask_qty"] == 400

    @pytest.mark.asyncio
    async def test_depth_packet_truncation_detection(self):
        """Truncated depth packet marks snapshot invalid with explicit reason."""
        packet = _build_depth_packet(41, 1333, [(500, 1650.0, 10)] * 5)
        truncated = packet[:-8]
        await self.depth_ws._parse_depth_packet(truncated)

        snapshot = self.depth_ws.depth_snapshots.get("1333")
        assert snapshot is not None
        assert snapshot.is_valid is False
        assert "truncated" in snapshot.validation_error


class TestConnectionLimits:
    """Test connection instrument limits per depth tier with strict capacity rejection."""

    def test_20depth_rejects_over_50(self):
        """20-depth tier rejects instruments exceeding 50 with ValueError (no silent truncation)."""
        ws = DhanDepthWebSocket("c", "t", depth_tier=20)
        instruments = [{"ExchangeSegment": "NSE_EQ", "SecurityId": str(i)} for i in range(75)]
        with pytest.raises(ValueError, match="Selection must occur in ScreenerFunnelService"):
            ws.set_instrument_list(instruments)

    def test_200depth_rejects_not_exactly_1(self):
        """200-depth tier rejects instruments not exactly 1 with ValueError (no silent truncation)."""
        ws = DhanDepthWebSocket("c", "t", depth_tier=200)
        instruments = [{"ExchangeSegment": "NSE_EQ", "SecurityId": str(i)} for i in range(10)]
        with pytest.raises(ValueError, match="Selection must occur in Dhan200DepthManager"):
            ws.set_instrument_list(instruments)

    def test_factory_validates_depth_tier(self):
        """Factory rejects depth tiers other than 20 or 200."""
        with pytest.raises(ValueError, match="depth_tier must be 20 or 200"):
            get_dhan_depth_ticker("client", "token", depth_tier=5)

    def test_factory_requires_credentials(self):
        """Factory requires non-empty client_id and access_token."""
        with pytest.raises(ValueError, match="client_id and access_token are required"):
            get_dhan_depth_ticker("", "token", depth_tier=20)

        with pytest.raises(ValueError, match="client_id and access_token are required"):
            get_dhan_depth_ticker("client", "", depth_tier=20)


class TestDepthSubscriptionPayloads:
    """Test depth subscription serialization by tier."""

    def test_20depth_payload_structure(self):
        ws = DhanDepthWebSocket("c", "t", depth_tier=20)
        instruments = [
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "1333"},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "2885"},
        ]
        ws.set_instrument_list(instruments)
        payload = ws.build_subscription_payload()

        assert payload["RequestCode"] == 23
        assert payload["InstrumentCount"] == 2
        assert len(payload["InstrumentList"]) == 2
        assert payload["InstrumentList"][0] == {"ExchangeSegment": "NSE_EQ", "SecurityId": "1333"}
        assert payload["InstrumentList"][1] == {"ExchangeSegment": "NSE_EQ", "SecurityId": "2885"}

    def test_20depth_rejects_empty_instruments(self):
        ws = DhanDepthWebSocket("c", "t", depth_tier=20)
        ws.set_instrument_list([])
        with pytest.raises(ValueError, match="20-depth subscription requires at least 1 instrument"):
            ws.build_subscription_payload()

    def test_200depth_flat_payload_structure(self):
        ws = DhanDepthWebSocket("c", "t", depth_tier=200)
        payload = ws.build_subscription_payload([
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "1333"}
        ])

        assert payload == {
            "RequestCode": 23,
            "ExchangeSegment": "NSE_EQ",
            "SecurityId": "1333",
        }
        # Flat format must NOT contain InstrumentList or InstrumentCount
        assert "InstrumentList" not in payload
        assert "InstrumentCount" not in payload

    def test_200depth_rejects_empty_instruments(self):
        ws = DhanDepthWebSocket("c", "t", depth_tier=200)
        with pytest.raises(ValueError, match="200-depth subscription requires exactly 1 instrument, got 0"):
            ws.build_subscription_payload([])

    def test_200depth_rejects_multiple_instruments(self):
        ws = DhanDepthWebSocket("c", "t", depth_tier=200)
        multiple = [
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "1333"},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "2885"},
        ]
        with pytest.raises(ValueError, match="200-depth subscription requires exactly 1 instrument, got 2"):
            ws.build_subscription_payload(multiple)

    @pytest.mark.asyncio
    async def test_reconnect_resubscription_payload(self):
        ws = DhanDepthWebSocket("c", "t", depth_tier=200)
        ws._instrument_list = [{"ExchangeSegment": "NSE_EQ", "SecurityId": "1333"}]
        # First subscription
        p1 = ws.build_subscription_payload()
        assert p1["SecurityId"] == "1333"
        # Simulate reconnect resubscription
        p2 = ws.build_subscription_payload()
        assert p2 == p1

    @pytest.mark.asyncio
    async def test_refresh_subscriptions_atomic_transition(self):
        """Dynamic refresh unsubs removed, subs new, keeps existing, and cleans snapshots."""
        ws = DhanDepthWebSocket("c", "t", depth_tier=20)
        init_inst = [
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "100", "symbol": "A"},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "101", "symbol": "B"},
        ]
        ws.set_instrument_list(init_inst)
        ws.depth_snapshots["100"] = MagicMock()
        ws.depth_snapshots["101"] = MagicMock()

        target_inst = [
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "100", "symbol": "A"},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "102", "symbol": "C"},
        ]
        to_sub, to_unsub = await ws.refresh_subscriptions(target_inst)
        assert to_sub == ["102"]
        assert to_unsub == ["101"]
        assert ws.active_subscriptions == ["100", "102"]
        # 101 snapshot must be cleared
        assert "101" not in ws.depth_snapshots
        # 100 snapshot must be preserved
        assert "100" in ws.depth_snapshots
        # id_to_symbol must contain new symbol
        assert ws.id_to_symbol.get("102") == "C"


class TestDhan200DepthManager:
    """Test 200-depth connection manager rotation & limits."""

    @pytest.mark.asyncio
    async def test_manager_caps_at_max_connections(self, monkeypatch):
        from app.services.dhan_adapter import Dhan200DepthManager
        monkeypatch.setattr(DhanDepthWebSocket, "connect", AsyncMock())

        mgr = Dhan200DepthManager("c", "t", max_connections=5)
        await mgr.start()

        candidates = [{"ExchangeSegment": "NSE_EQ", "SecurityId": str(i), "symbol": f"SYM_{i}"} for i in range(10)]
        await mgr.update_candidates(candidates)

        assert len(mgr.active_connections) == 5
        assert set(mgr.active_connections.keys()) == {"0", "1", "2", "3", "4"}

        await mgr.stop()
        assert len(mgr.active_connections) == 0

    @pytest.mark.asyncio
    async def test_manager_rotates_candidates_with_cooldown(self, monkeypatch):
        from app.services.dhan_adapter import Dhan200DepthManager
        monkeypatch.setattr(DhanDepthWebSocket, "connect", AsyncMock())

        mgr = Dhan200DepthManager("c", "t", max_connections=3, rotation_cooldown_sec=0.0)
        await mgr.start()

        c1 = [
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "100", "symbol": "RELIANCE"},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "101", "symbol": "TCS"},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "102", "symbol": "INFY"},
        ]
        await mgr.update_candidates(c1)
        assert set(mgr.active_connections.keys()) == {"100", "101", "102"}

        # New ranking: 100 stays, 101 stays, 103 enters, 102 drops
        c2 = [
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "100", "symbol": "RELIANCE"},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "101", "symbol": "TCS"},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "103", "symbol": "HDFCBANK"},
        ]
        await mgr.update_candidates(c2)
        assert set(mgr.active_connections.keys()) == {"100", "101", "103"}
        assert "102" not in mgr.active_connections

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_manager_enforces_score_advantage_threshold(self, monkeypatch):
        from app.services.dhan_adapter import Dhan200DepthManager
        monkeypatch.setattr(DhanDepthWebSocket, "connect", AsyncMock())

        # Requires 5.0 points score advantage to displace an active slot
        mgr = Dhan200DepthManager("c", "t", max_connections=2, rotation_cooldown_sec=0.0, min_score_advantage=5.0)
        await mgr.start()

        # Initial candidates with scores 80.0 and 70.0
        c1 = [
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "100", "symbol": "RELIANCE", "score": 80.0},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "101", "symbol": "TCS", "score": 70.0},
        ]
        await mgr.update_candidates(c1)
        assert set(mgr.active_connections.keys()) == {"100", "101"}

        # Challenger 102 has score 72.0 (only +2.0 over 101's 70.0, less than min_score_advantage 5.0)
        c2 = [
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "100", "symbol": "RELIANCE", "score": 80.0},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "102", "symbol": "INFY", "score": 72.0},
        ]
        await mgr.update_candidates(c2)
        # Should be rejected: 101 retained, 102 marked as skipped
        assert set(mgr.active_connections.keys()) == {"100", "101"}
        assert any(s["symbol"] == "INFY" and "insufficient_score_advantage" in s["reason"] for s in mgr.skipped_candidates)

        # Challenger 103 has score 78.0 (+8.0 over 101's 70.0, exceeds 5.0 advantage)
        c3 = [
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "100", "symbol": "RELIANCE", "score": 80.0},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "103", "symbol": "HDFCBANK", "score": 78.0},
        ]
        await mgr.update_candidates(c3)
        assert set(mgr.active_connections.keys()) == {"100", "103"}
        assert any(e["symbol"] == "TCS" for e in mgr.evicted_candidates)

        await mgr.stop()

    @pytest.mark.asyncio
    async def test_manager_tracks_rotation_status_telemetry(self, monkeypatch):
        from app.services.dhan_adapter import Dhan200DepthManager
        monkeypatch.setattr(DhanDepthWebSocket, "connect", AsyncMock())

        mgr = Dhan200DepthManager("c", "t", max_connections=2, rotation_cooldown_sec=0.0)
        await mgr.start()

        candidates = [
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "100", "symbol": "RELIANCE", "score": 85.0},
            {"ExchangeSegment": "NSE_EQ", "SecurityId": "101", "symbol": "TCS", "score": 75.0},
        ]
        await mgr.update_candidates(candidates)

        status = mgr.get_rotation_status()
        assert status["max_connections"] == 2
        assert status["active_count"] == 2
        assert len(status["active_candidates"]) == 2
        assert status["active_candidates"][0]["symbol"] in ("RELIANCE", "TCS")

        await mgr.stop()
