"""
Dhan WebSocket Adapter
======================
Connects to Dhan's binary WebSocket feeds
for live market data with 20-level and optional 200-level deep market depth.

Architecture:
    DhanWebSocket         — Live standard feed (LTP, OHLCV, 5-depth)
    DhanDepthWebSocket    — 20-level or 200-level deep order book
    MockDhanWebSocket     — Dev/test mock that generates synthetic ticks

All adapters publish parsed ticks to the shared ZMQ bus using the same
payload schema expected by downstream engines (CVD, Candle Aggregator, etc).

Dhan Binary Packet Reference:
    Standard feed: wss://api-feed.dhan.co
    20-depth:      wss://depth-api-feed.dhan.co/twentydepth
    200-depth:     wss://full-depth-api.dhan.co/twohundreddepth

    Auth via query params: ?token={access_token}&clientId={client_id}&authType=2
    Subscription via JSON: {"RequestCode": 21, "InstrumentCount": N, "InstrumentList": [...]}
"""

import asyncio
import json
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .cvd_engine import get_cvd_engine
from .regime_engine import get_regime_engine
from .zmq_publisher import get_zmq_publisher

logger = logging.getLogger(__name__)

USE_MOCK = os.getenv("USE_DHAN_MOCK", "false").lower() == "true"
PRODUCTION_MODE = os.getenv("PRODUCTION_MODE", "false").lower() == "true"
FAIL_CLOSED_ON_LIVE_ERROR = os.getenv("FAIL_CLOSED_ON_LIVE_ERROR", "false").lower() == "true"

# ── Dhan WebSocket URLs ──────────────────────────────────────────────────────
# Import authoritative protocol constants
from .dhan_protocol import (
    DHAN_STANDARD_FEED_URL as DHAN_FEED_URL,
    DHAN_20DEPTH_FEED_URL as DHAN_20DEPTH_URL,
    DHAN_200DEPTH_FEED_URL as DHAN_200DEPTH_URL,
    RequestCode,
    ResponseCode,
    ExchangeSegment,
    MAX_INSTRUMENTS_PER_MESSAGE,
    MAX_INSTRUMENTS_20DEPTH,
    MAX_INSTRUMENTS_200DEPTH,
    STANDARD_HEADER_SIZE,
    DEPTH_HEADER_SIZE,
    TICKER_PACKET_SIZE,
    QUOTE_PACKET_SIZE,
    FULL_PACKET_SIZE,
    FULL_DEPTH_OFFSET,
    DEPTH_LEVEL_SIZE,
    DEPTH_5_LEVELS,
    build_auth_url,
)

# Dhan request codes for subscription (aliases for backward compat)
REQUEST_CODE_SUBSCRIBE = RequestCode.FULL
REQUEST_CODE_UNSUBSCRIBE = RequestCode.UNSUBSCRIBE
REQUEST_CODE_SUBSCRIBE_DEPTH = RequestCode.DEPTH_20

# Dhan feed response codes
FEED_RESPONSE_TICKER = ResponseCode.TICKER
FEED_RESPONSE_QUOTE = ResponseCode.QUOTE
FEED_RESPONSE_OI = ResponseCode.OI
FEED_RESPONSE_PREV_CLOSE = ResponseCode.PREV_CLOSE
FEED_RESPONSE_FULL = ResponseCode.FULL
FEED_RESPONSE_DEPTH = 11
FEED_RESPONSE_TWENTY_DEPTH = ResponseCode.DEPTH_20_BID

# ── Compiled Struct Unpackers (High-Performance Zero-Slice Deserialization) ──
_STANDARD_HEADER_STRUCT = struct.Struct("<BHBi")
_TICKER_BODY_STRUCT     = struct.Struct("<fi")
_QUOTE_BODY_STRUCT      = struct.Struct("<fhifiiiffff")
_FULL_BODY_STRUCT       = struct.Struct("<fhifiiiiiiffff")
_DEPTH_5_LEVEL_STRUCT   = struct.Struct("<iihhff")
_DEPTH_20_LEVEL_STRUCT  = struct.Struct("<dII")
_DEPTH_200_LEVEL_STRUCT = struct.Struct("<IiI")

# ── Depth Level Dataclass ────────────────────────────────────────────────────

@dataclass(slots=True)
class DepthLevel:
    """Single level in the order book."""
    price: float
    quantity: int
    orders: int = 0


@dataclass(slots=True)
class DeepDepthSnapshot:
    """Complete 20 or 200 level depth snapshot for a single instrument."""
    security_id: str
    timestamp: float
    bids: List[DepthLevel] = field(default_factory=list)
    asks: List[DepthLevel] = field(default_factory=list)
    total_bid_qty: int = 0
    total_ask_qty: int = 0
    deep_obi: float = 0.0  # OBI computed over all available levels
    is_valid: bool = True
    validation_error: Optional[str] = None


class DhanWebSocket:
    """
    Dhan WebSocket adapter for standard live market feed.

    Connects to wss://api-feed.dhan.co and parses binary tick packets.
    Publishes to the ZMQ bus in the canonical tick payload format.
    """

    # Jitter quality thresholds (milliseconds)
    JITTER_QUALITY_GOOD = 100
    JITTER_QUALITY_ACCEPTABLE = 500
    JITTER_QUALITY_POOR = 2000

    def __init__(self, client_id: str, access_token: str):
        self.client_id = client_id
        self.access_token = access_token
        self.zmq_publisher = get_zmq_publisher()
        self.cvd_engine = get_cvd_engine()
        self.regime_engine = get_regime_engine()
        self.running = False
        self._ws = None
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 50

        # Symbol resolution maps (set externally)
        self.id_to_symbol: Dict[str, str] = {}
        self.index_id_to_symbol: Dict[str, str] = {"13": "NIFTY 50", "21": "INDIA VIX", "26017": "INDIA VIX"}
        self.vix_security_id: Optional[str] = "21"
        self.nifty50_security_id: Optional[str] = "13"

        # Subscription list (set externally)
        self._instrument_list: List[Dict[str, str]] = []

        # Latest standard feed metrics per symbol for screener funnel
        self.latest_quotes: Dict[str, Dict[str, Any]] = {}

        # Numpy arrays for fast LTP/volume tracking
        self.id_to_idx: Dict[str, int] = {}
        self.idx_counter = 0
        cap = 5000
        self.ltp_arr = np.zeros(cap, dtype=np.float32)
        self.vol_arr = np.zeros(cap, dtype=np.int32)

        # Jitter tracking
        self.jitter_ema: float = 0.0
        self.jitter_alpha: float = 0.1
        self.jitter_samples: int = 0
        self.jitter_max: float = 0.0
        self.jitter_min: float = float('inf')
        self._recv_buffer: bytearray = bytearray()

    def set_instrument_list(self, instruments: List[Dict[str, str]]) -> None:
        """Set the list of instruments to subscribe to (rejects over 5000 capacity)."""
        if len(instruments) > 5000:
            raise ValueError(f"Dhan standard connection allows max 5000 instruments, got {len(instruments)}")
        self._instrument_list = list(instruments)

    def set_symbol_lookup(
        self,
        id_to_symbol: Dict[str, str],
        index_id_to_symbol: Optional[Dict[str, str]] = None,
    ) -> None:
        """Set security_id → symbol lookup map with explicit index isolation."""
        self.id_to_symbol = dict(id_to_symbol)
        if index_id_to_symbol is not None:
            self.index_id_to_symbol = dict(index_id_to_symbol)
        else:
            self.index_id_to_symbol = getattr(self, "index_id_to_symbol", {
                "13": "NIFTY 50",
                "21": "INDIA VIX",
                "26017": "INDIA VIX",
            })
        # Identify index symbols from index map first
        for sec_id, symbol in self.index_id_to_symbol.items():
            if symbol == "INDIA VIX":
                self.vix_security_id = sec_id
                logger.info("Identified India VIX security_id: %s", sec_id)
            elif symbol == "NIFTY 50":
                self.nifty50_security_id = sec_id
                logger.info("Identified Nifty 50 security_id: %s", sec_id)

        # Fallback to general map only if not identified
        if not self.nifty50_security_id:
            for sec_id, symbol in self.id_to_symbol.items():
                if symbol == "NIFTY 50":
                    self.nifty50_security_id = sec_id
                    break
        if not self.vix_security_id:
            for sec_id, symbol in self.id_to_symbol.items():
                if symbol == "INDIA VIX":
                    self.vix_security_id = sec_id
                    break

    def _get_jitter_quality(self, jitter_ms: float) -> str:
        if jitter_ms < self.JITTER_QUALITY_GOOD:
            return "good"
        elif jitter_ms < self.JITTER_QUALITY_ACCEPTABLE:
            return "acceptable"
        elif jitter_ms < self.JITTER_QUALITY_POOR:
            return "poor"
        else:
            return "critical"

    def _update_jitter_ema(self, jitter_ms: float) -> float:
        self.jitter_samples += 1
        self.jitter_max = max(self.jitter_max, jitter_ms)
        self.jitter_min = min(self.jitter_min, jitter_ms)
        if self.jitter_samples == 1:
            self.jitter_ema = jitter_ms
        else:
            self.jitter_ema = self.jitter_alpha * jitter_ms + (1 - self.jitter_alpha) * self.jitter_ema
        return self.jitter_ema

    def get_jitter_stats(self) -> Dict[str, Any]:
        return {
            "jitter_ema_ms": round(self.jitter_ema, 2),
            "jitter_min_ms": round(self.jitter_min, 2) if self.jitter_min != float('inf') else 0,
            "jitter_max_ms": round(self.jitter_max, 2),
            "jitter_samples": self.jitter_samples,
            "jitter_quality": self._get_jitter_quality(self.jitter_ema),
        }

    def _get_idx(self, security_id: str) -> int:
        if security_id not in self.id_to_idx:
            if self.idx_counter >= len(self.ltp_arr):
                logger.error("Index cap exceeded (%d). Dropping security %s.", len(self.ltp_arr), security_id)
                return -1
            self.id_to_idx[security_id] = self.idx_counter
            self.idx_counter += 1
        return self.id_to_idx[security_id]

    def _build_auth_url(self, base_url: str) -> str:
        """Build authenticated WebSocket URL with version=2."""
        cid = (
            self.client_id
            or os.getenv("DHAN_USER_ID")
            or os.getenv("DHAN_CLIENT_ID")
            or os.getenv("DHAN_API_KEY")
        )
        return build_auth_url(base_url, self.access_token, str(cid))

    def _build_subscription_messages(self, request_code: int = REQUEST_CODE_SUBSCRIBE) -> list:
        """Build batched JSON subscription messages (max 100 instruments each)."""
        if request_code == REQUEST_CODE_UNSUBSCRIBE:
            messages = []
            for i in range(0, len(self._instrument_list), MAX_INSTRUMENTS_PER_MESSAGE):
                batch = self._instrument_list[i:i + MAX_INSTRUMENTS_PER_MESSAGE]
                messages.append(json.dumps({
                    "RequestCode": request_code,
                    "InstrumentCount": len(batch),
                    "InstrumentList": batch,
                }))
            return messages

        # Separate equity and index instruments (Dhan requires Quote mode 17 for IDX_I, Full mode 21 for Equities)
        eq_instruments = []
        idx_instruments = []
        for inst in self._instrument_list:
            if inst.get("ExchangeSegment") == "IDX_I":
                idx_instruments.append(inst)
            else:
                eq_instruments.append(inst)

        messages = []
        for i in range(0, len(eq_instruments), MAX_INSTRUMENTS_PER_MESSAGE):
            batch = eq_instruments[i:i + MAX_INSTRUMENTS_PER_MESSAGE]
            messages.append(json.dumps({
                "RequestCode": request_code,
                "InstrumentCount": len(batch),
                "InstrumentList": batch,
            }))

        if idx_instruments:
            # Indices do not support Full mode (21); use Quote mode (17) for index OHLCV
            idx_code = RequestCode.QUOTE if request_code == REQUEST_CODE_SUBSCRIBE else request_code
            for i in range(0, len(idx_instruments), MAX_INSTRUMENTS_PER_MESSAGE):
                batch = idx_instruments[i:i + MAX_INSTRUMENTS_PER_MESSAGE]
                messages.append(json.dumps({
                    "RequestCode": idx_code,
                    "InstrumentCount": len(batch),
                    "InstrumentList": batch,
                }))

        return messages

    def _parse_standard_header(self, data: bytes):
        """Parse 8-byte standard feed header.

        Returns: (response_code, msg_length, exchange_segment, security_id)
        """
        return _STANDARD_HEADER_STRUCT.unpack_from(data, 0)

    async def _process_stream_bytes(self, chunk: bytes) -> None:
        """Accumulate incoming binary chunks and frame them into complete Dhan packets."""
        if not chunk:
            return
        self._recv_buffer.extend(chunk)

        while len(self._recv_buffer) >= STANDARD_HEADER_SIZE:
            response_code = self._recv_buffer[0]
            declared_len = struct.unpack("<H", self._recv_buffer[1:3])[0]

            known_codes = (
                FEED_RESPONSE_TICKER,
                FEED_RESPONSE_QUOTE,
                FEED_RESPONSE_OI,
                FEED_RESPONSE_PREV_CLOSE,
                FEED_RESPONSE_FULL,
                ResponseCode.MARKET_STATUS,
                7,
            )
            is_valid_code = response_code in known_codes
            is_valid_len = (STANDARD_HEADER_SIZE <= declared_len <= 512)

            if not (is_valid_code and is_valid_len):
                # Desynchronized: search forward for next valid header
                sync_idx = -1
                for i in range(1, len(self._recv_buffer) - 2):
                    if self._recv_buffer[i] in known_codes:
                        plen = struct.unpack("<H", self._recv_buffer[i + 1:i + 3])[0]
                        if STANDARD_HEADER_SIZE <= plen <= 512:
                            sync_idx = i
                            break
                if sync_idx > 0:
                    del self._recv_buffer[:sync_idx]
                else:
                    del self._recv_buffer[:-2]
                continue

            if len(self._recv_buffer) < declared_len:
                break

            packet = bytes(self._recv_buffer[:declared_len])
            del self._recv_buffer[:declared_len]

            await self._parse_binary_message(packet)

    async def _parse_binary_message(self, data: bytes) -> None:
        """
        Parse Dhan's binary market data packet.

        Header (8 bytes):
            Byte 0:    Response Code (uint8)
            Bytes 1-2: Message Length (int16le)
            Byte 3:    Exchange Segment (uint8)
            Bytes 4-7: Security ID (int32le)

        Payload starts at byte 8.
        """
        if not data or len(data) < STANDARD_HEADER_SIZE:
            return

        receipt_ts = time.time()
        try:
            response_code, msg_length, exchange_seg, security_id = self._parse_standard_header(data)

            # Handle bundled packets if called with multiple packets concatenated
            if msg_length < len(data) and msg_length >= STANDARD_HEADER_SIZE:
                offset = 0
                while offset + STANDARD_HEADER_SIZE <= len(data):
                    plen = struct.unpack("<H", data[offset + 1:offset + 3])[0]
                    if plen < STANDARD_HEADER_SIZE or offset + plen > len(data):
                        break
                    await self._parse_binary_message(data[offset:offset + plen])
                    offset += plen
                return

            if msg_length > len(data):
                # Incomplete standalone packet - drop
                return


            # Route by response code + packet size
            if response_code == FEED_RESPONSE_FULL and len(data) >= FULL_PACKET_SIZE:
                await self._parse_full_packet(data, receipt_ts, str(security_id), exchange_seg)
            elif response_code == FEED_RESPONSE_QUOTE and len(data) >= QUOTE_PACKET_SIZE:
                await self._parse_quote_packet(data, receipt_ts, str(security_id), exchange_seg)
            elif response_code == FEED_RESPONSE_TICKER and len(data) >= TICKER_PACKET_SIZE:
                await self._parse_ticker_packet(data, receipt_ts, str(security_id), exchange_seg)
            elif response_code in (FEED_RESPONSE_OI, ResponseCode.PREV_CLOSE, ResponseCode.MARKET_STATUS):
                # Known non-price / auxiliary codes — parse metadata
                symbol = self.id_to_symbol.get(str(security_id), str(security_id))
                if response_code == ResponseCode.MARKET_STATUS:
                    disconnect_code = struct.unpack("<h", data[8:10])[0] if len(data) >= 10 else None
                    logger.warning("Dhan server sent Disconnection/Market Status packet (code 50): reason code=%s", disconnect_code)
                elif response_code == ResponseCode.PREV_CLOSE and len(data) >= PREV_CLOSE_PACKET_SIZE:
                    prev_close = float(struct.unpack("<f", data[8:12])[0])
                    prev_oi = struct.unpack("<i", data[12:16])[0]
                    if symbol not in self.latest_quotes:
                        self.latest_quotes[symbol] = {}
                    self.latest_quotes[symbol]["prev_close"] = prev_close
                    self.latest_quotes[symbol]["close"] = prev_close
                    self.latest_quotes[symbol]["prev_oi"] = prev_oi
                elif response_code == FEED_RESPONSE_OI and len(data) >= OI_PACKET_SIZE:
                    oi_val = struct.unpack("<i", data[8:12])[0]
                    if symbol in self.latest_quotes:
                        self.latest_quotes[symbol]["oi"] = oi_val
            else:
                logger.debug("Unknown/short Dhan packet: code=%d, size=%d", response_code, len(data))

        except Exception as exc:
            logger.error("Error parsing Dhan binary packet (%d bytes): %s", len(data), exc)

    def _forward_tick_to_screener(self, payload: Dict[str, Any]) -> None:
        try:
            from services.short_screener_engine import get_short_screener_engine
            screener = get_short_screener_engine()
            if screener.is_active:
                screener.on_tick(payload)
        except Exception:
            pass

    async def _parse_ticker_packet(self, data: bytes, receipt_ts: float, security_id: str, exchange_seg: int) -> None:
        """Parse a ticker packet (16 bytes: 8-byte header + float32 LTP + int32 LTT)."""
        try:
            raw_ltp, ltt = _TICKER_BODY_STRUCT.unpack_from(data, 8)
            ltp = float(raw_ltp)

            symbol = self.id_to_symbol.get(security_id, security_id)

            # Index fast-path (only when exchange segment is IDX_I == 0)
            if exchange_seg in (0, "0"):
                idx_symbol = self.index_id_to_symbol.get(
                    security_id,
                    "NIFTY 50" if security_id == self.nifty50_security_id else ("INDIA VIX" if security_id == self.vix_security_id else None)
                )
                idx_token = 10_000_000 + int(security_id)
                if idx_symbol == "INDIA VIX" or security_id == self.vix_security_id:
                    self.regime_engine.set_india_vix(ltp)
                    payload = {
                        "token": idx_token,
                        "symbol": "INDIA VIX",
                        "ltp": ltp,
                        "timestamp": receipt_ts,
                        "is_index": True,
                    }
                    await self.zmq_publisher.publish_tick(payload)
                    return

                if idx_symbol == "NIFTY 50" or security_id == self.nifty50_security_id:
                    close_p = self.latest_quotes.get("NIFTY 50", {}).get("close", ltp)
                    chg_pct = round((ltp - close_p) / close_p * 100.0, 2) if close_p > 0 else 0.0
                    self.latest_quotes["NIFTY 50"] = {
                        "security_id": security_id,
                        "ltp": ltp,
                        "close": close_p,
                        "change_pct": chg_pct,
                        "timestamp": receipt_ts,
                        "is_index": True,
                    }
                    payload = {
                        "token": idx_token,
                        "symbol": "NIFTY 50",
                        "ltp": ltp,
                        "close": close_p,
                        "change_pct": chg_pct,
                        "timestamp": receipt_ts,
                        "is_index": True,
                    }
                    await self.zmq_publisher.publish_tick(payload)
                    return

            idx = self._get_idx(security_id)
            if idx >= 0:
                self.ltp_arr[idx] = ltp

            if symbol in self.latest_quotes:
                self.latest_quotes[symbol]["ltp"] = ltp
                self.latest_quotes[symbol]["timestamp"] = receipt_ts
            else:
                self.latest_quotes[symbol] = {
                    "ltp": ltp,
                    "volume": 0,
                    "best_bid": ltp - 0.05,
                    "best_ask": ltp + 0.05,
                    "spread_pct": 0.0001,
                    "obi": 0.0,
                    "timestamp": receipt_ts,
                }

            payload = {
                "token": int(security_id),
                "symbol": symbol,
                "ltp": ltp,
                "timestamp": float(ltt) if ltt > 0 else receipt_ts,
            }
            await self.zmq_publisher.publish_tick(payload)
            self._forward_tick_to_screener(payload)

        except Exception as exc:
            logger.error("Error parsing ticker packet: %s", exc)

    async def _parse_quote_packet(self, data: bytes, receipt_ts: float, security_id: str, exchange_seg: int) -> None:
        """Parse a quote packet (50 bytes: 8-byte header + OHLCV)."""
        try:
            (
                raw_ltp, ltq, ltt, raw_avg, volume,
                total_sell_qty, total_buy_qty,
                raw_op, raw_cp, raw_hp, raw_lp
            ) = _QUOTE_BODY_STRUCT.unpack_from(data, 8)
            ltp = float(raw_ltp)
            avg_price = float(raw_avg)
            open_price = float(raw_op)
            close_price = float(raw_cp)
            high_price = float(raw_hp)
            low_price = float(raw_lp)

            symbol = self.id_to_symbol.get(security_id, security_id)

            # Index fast-path in quote mode (only when exchange segment is IDX_I == 0)
            is_index_inst = exchange_seg in (0, "0")
            if is_index_inst:
                idx_symbol = self.index_id_to_symbol.get(
                    security_id,
                    "NIFTY 50" if security_id == self.nifty50_security_id else ("INDIA VIX" if security_id == self.vix_security_id else "INDEX")
                )
                if idx_symbol == "INDIA VIX" or security_id == self.vix_security_id:
                    self.regime_engine.set_india_vix(ltp)
                chg_pct = round((ltp - close_price) / close_price * 100.0, 2) if close_price > 0 else 0.0
                self.latest_quotes[idx_symbol] = {
                    "security_id": security_id,
                    "ltp": ltp,
                    "close": close_price,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "change_pct": chg_pct,
                    "volume": volume,
                    "average_price": avg_price,
                    "timestamp": receipt_ts,
                    "is_index": True,
                }
                idx_token = 10_000_000 + int(security_id)
                payload = {
                    "token": idx_token,
                    "symbol": idx_symbol,
                    "ltp": ltp,
                    "close": close_price,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "change_pct": chg_pct,
                    "volume": volume,
                    "avg_price": avg_price,
                    "timestamp": float(ltt) if ltt > 0 else receipt_ts,
                    "is_index": True,
                }
                await self.zmq_publisher.publish_tick(payload)
                return

            idx = self._get_idx(security_id)
            if idx >= 0:
                self.ltp_arr[idx] = ltp
                self.vol_arr[idx] = volume

            best_bid = ltp - 0.05
            best_ask = ltp + 0.05
            spread_pct = (best_ask - best_bid) / ltp if ltp > 0 else 0.0

            cvd_result = self.cvd_engine.process_tick(
                instrument_token=int(security_id),
                last_price=ltp,
                current_volume=volume,
                best_bid=best_bid,
                best_ask=best_ask,
                total_bid_qty=total_buy_qty,
                total_ask_qty=total_sell_qty,
            )

            if symbol not in self.latest_quotes:
                self.latest_quotes[symbol] = {}
            self.latest_quotes[symbol].update({
                "security_id": security_id,
                "ltp": ltp,
                "volume": volume,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_pct": spread_pct,
                "obi": cvd_result.obi,
                "total_bid_qty": total_buy_qty,
                "total_sell_qty": total_sell_qty,
                "timestamp": receipt_ts,
                "open": open_price,
                "close": close_price,
                "prev_close": close_price if close_price > 0 else self.latest_quotes[symbol].get("prev_close", 0.0),
                "high": high_price,
                "low": low_price,
            })

            payload = {
                "token": int(security_id),
                "symbol": symbol,
                "ltp": ltp,
                "volume": volume,
                "tick_vol": cvd_result.tick_volume,
                "cvd": cvd_result.cvd,
                "obi": cvd_result.obi,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "total_bid_qty": total_buy_qty,
                "total_sell_qty": total_sell_qty,
                "bid_price": best_bid,
                "ask_price": best_ask,
                "bid_qty": total_buy_qty,
                "ask_qty": total_sell_qty,
                "exchange_ts": ltt,
                "receipt_ts": receipt_ts,
                "timestamp": float(ltt) if ltt > 0 else receipt_ts,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "ltq": ltq,
                "avg_price": avg_price,
            }
            await self.zmq_publisher.publish_tick(payload)
            self._forward_tick_to_screener(payload)

        except Exception as exc:
            logger.error("Error parsing quote packet: %s", exc)

    async def _parse_full_packet(self, data: bytes, receipt_ts: float, security_id: str, exchange_seg: int) -> None:
        """Parse a full market data packet (162 bytes: 8-byte header + OHLCV & OI 54 bytes + 5-level depth 100 bytes)."""
        try:
            (
                raw_ltp, ltq, ltt, raw_avg, volume,
                total_sell_qty, total_buy_qty,
                oi, high_oi, low_oi,
                raw_op, raw_cp, raw_hp, raw_lp
            ) = _FULL_BODY_STRUCT.unpack_from(data, 8)
            ltp = float(raw_ltp)
            avg_price = float(raw_avg)
            open_price = float(raw_op)
            close_price = float(raw_cp)
            high_price = float(raw_hp)
            low_price = float(raw_lp)

            symbol = self.id_to_symbol.get(security_id, security_id)

            # Jitter calculation from LTT
            jitter_ms = 0.0
            jitter_ema_ms = 0.0
            jitter_quality = "good"
            if ltt > 0:
                clock_delta = receipt_ts - float(ltt)
                if -30.0 <= clock_delta <= 30.0:
                    jitter_ms = clock_delta * 1000.0
                    jitter_ema_ms = self._update_jitter_ema(jitter_ms)
                    jitter_quality = self._get_jitter_quality(jitter_ema_ms)

            # Index fast-path (only when exchange segment is IDX_I == 0)
            if exchange_seg in (0, "0"):
                idx_symbol = self.index_id_to_symbol.get(
                    security_id,
                    "NIFTY 50" if security_id == self.nifty50_security_id else ("INDIA VIX" if security_id == self.vix_security_id else None)
                )
                idx_token = 10_000_000 + int(security_id)
                if idx_symbol == "INDIA VIX" or security_id == self.vix_security_id:
                    self.regime_engine.set_india_vix(ltp)
                    payload = {
                        "token": idx_token,
                        "symbol": "INDIA VIX",
                        "ltp": ltp,
                        "timestamp": receipt_ts,
                        "is_index": True,
                    }
                    await self.zmq_publisher.publish_tick(payload)
                    return

                if idx_symbol == "NIFTY 50" or security_id == self.nifty50_security_id:
                    payload = {
                        "token": idx_token,
                        "symbol": "NIFTY 50",
                        "ltp": ltp,
                        "timestamp": receipt_ts,
                        "is_index": True,
                    }
                    await self.zmq_publisher.publish_tick(payload)
                    return

            # Update tracking arrays
            idx = self._get_idx(security_id)
            if idx >= 0:
                self.ltp_arr[idx] = ltp
                self.vol_arr[idx] = volume

            best_bid = 0.0
            best_ask = 0.0
            total_bid_qty_5 = 0
            total_ask_qty_5 = 0
            has_depth = len(data) >= FULL_PACKET_SIZE

            if has_depth:
                doff = FULL_DEPTH_OFFSET  # byte 62
                for lvl in range(DEPTH_5_LEVELS):
                    b_qty, a_qty, b_ord, a_ord, b_px, a_px = _DEPTH_5_LEVEL_STRUCT.unpack_from(data, doff)
                    if lvl == 0:
                        best_bid = float(b_px)
                        best_ask = float(a_px)
                    total_bid_qty_5 += max(0, b_qty)
                    total_ask_qty_5 += max(0, a_qty)
                    doff += 20
                if total_buy_qty == 0:
                    total_buy_qty = total_bid_qty_5
                if total_sell_qty == 0:
                    total_sell_qty = total_ask_qty_5
            else:
                best_bid = ltp - 0.05
                best_ask = ltp + 0.05

            spread_pct = (best_ask - best_bid) / ltp if (best_bid > 0 and best_ask > 0 and ltp > 0) else 0.0
            denom = total_bid_qty_5 + total_ask_qty_5
            obi_5 = (total_bid_qty_5 - total_ask_qty_5) / denom if denom > 0 else 0.0

            # Process through CVD engine
            cvd_result = self.cvd_engine.process_tick(
                instrument_token=int(security_id),
                last_price=ltp,
                current_volume=volume,
                best_bid=best_bid,
                best_ask=best_ask,
                total_bid_qty=total_buy_qty,
                total_ask_qty=total_sell_qty,
            )

            if symbol not in self.latest_quotes:
                self.latest_quotes[symbol] = {}
            self.latest_quotes[symbol].update({
                "security_id": security_id,
                "ltp": ltp,
                "volume": volume,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_pct": spread_pct,
                "obi": obi_5,
                "total_bid_qty": total_buy_qty,
                "total_sell_qty": total_sell_qty,
                "oi": oi,
                "high_oi": high_oi,
                "low_oi": low_oi,
                "timestamp": receipt_ts,
                "open": open_price,
                "close": close_price,
                "prev_close": close_price if close_price > 0 else self.latest_quotes[symbol].get("prev_close", 0.0),
                "high": high_price,
                "low": low_price,
                "avg_price": avg_price,
            })

            # Warn on high jitter
            if jitter_ema_ms > 500 and jitter_quality in ("poor", "critical"):
                logger.warning(
                    "High network jitter %.1fms (quality=%s) for %s",
                    jitter_ema_ms, jitter_quality, symbol,
                )

            ts_value = float(ltt) if ltt > 0 else receipt_ts

            payload = {
                "token": int(security_id),
                "symbol": symbol,
                "ltp": ltp,
                "volume": volume,
                "tick_vol": cvd_result.tick_volume,
                "cvd": cvd_result.cvd,
                "obi": cvd_result.obi,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "total_bid_qty": total_buy_qty,
                "total_sell_qty": total_sell_qty,
                "bid_price": best_bid,
                "ask_price": best_ask,
                "bid_qty": total_buy_qty,
                "ask_qty": total_sell_qty,
                "exchange_ts": ltt,
                "receipt_ts": receipt_ts,
                "timestamp": ts_value,
                "jitter_ms": jitter_ms,
                "jitter_ema_ms": round(jitter_ema_ms, 2),
                "jitter_quality": jitter_quality,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "avg_price": avg_price,
                "ltq": ltq,
                "oi": oi,
                "high_oi": high_oi,
                "low_oi": low_oi,
            }
            await self.zmq_publisher.publish_tick(payload)
            self._forward_tick_to_screener(payload)

        except Exception as exc:
            logger.error("Error parsing full packet for security %s: %s", security_id, exc)

    async def connect(self) -> None:
        """Connect to Dhan WebSocket and start consuming data."""
        import websockets

        self.running = True
        url = self._build_auth_url(DHAN_FEED_URL)
        logger.info("Connecting to Dhan WebSocket at %s", DHAN_FEED_URL)

        while self.running and self._reconnect_attempts < self._max_reconnect_attempts:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=None,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=2**20,  # 1MB max message
                ) as ws:
                    self._ws = ws
                    logger.info("Dhan WebSocket connected. Subscribing to %d instruments.", len(self._instrument_list))

                    # Send batched subscriptions (max 100 instruments per message)
                    sub_messages = self._build_subscription_messages(REQUEST_CODE_SUBSCRIBE)
                    for sub_msg in sub_messages:
                        await ws.send(sub_msg)
                    logger.info("Sent %d subscription batches for %d instruments", len(sub_messages), len(self._instrument_list))

                    # Read messages
                    packet_count = 0
                    self._recv_buffer.clear()
                    async for message in ws:
                        if isinstance(message, bytes):
                            await self._process_stream_bytes(message)
                            packet_count += 1
                            if packet_count >= 5:
                                self._reconnect_attempts = 0
                        else:
                            # JSON response (ack, error, etc.)
                            try:
                                resp = json.loads(message)
                                logger.debug("Dhan WS JSON response: %s", resp)
                            except json.JSONDecodeError:
                                pass

            except Exception as exc:
                self._reconnect_attempts += 1
                wait_time = min(2 ** self._reconnect_attempts, 60)
                exc_str = str(exc).lower()
                if "429" in exc_str or "rate" in exc_str:
                    wait_time = max(30, wait_time)
                    logger.error(
                        "Dhan WebSocket received HTTP 429 Rate Limit. Backing off for %ds (attempt %d/%d)",
                        wait_time, self._reconnect_attempts, self._max_reconnect_attempts,
                    )
                else:
                    logger.warning(
                        "Dhan WebSocket disconnected (%s). Reconnect attempt %d/%d in %ds",
                        exc, self._reconnect_attempts, self._max_reconnect_attempts, wait_time,
                    )
                await asyncio.sleep(wait_time)

        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.critical("Dhan WebSocket: max reconnect attempts exhausted.")

    async def subscribe_instrument(
        self,
        symbol: str,
        security_id: str,
        exchange_segment: str = "NSE_EQ",
        request_code: int = REQUEST_CODE_SUBSCRIBE,
    ) -> bool:
        """Dynamically subscribe to an instrument on the active WebSocket connection."""
        sec_id_str = str(security_id).strip()
        if not sec_id_str:
            return False
        self.id_to_symbol[sec_id_str] = symbol
        self._get_idx(sec_id_str)

        inst = {
            "ExchangeSegment": exchange_segment,
            "SecurityId": sec_id_str,
        }
        if not any(str(i.get("SecurityId")) == sec_id_str for i in self._instrument_list):
            self._instrument_list.append(inst)

        if self._ws and not getattr(self._ws, "closed", False):
            try:
                code = RequestCode.QUOTE if exchange_segment == "IDX_I" else request_code
                payload = json.dumps({
                    "RequestCode": code,
                    "InstrumentCount": 1,
                    "InstrumentList": [inst],
                })
                await self._ws.send(payload)
                logger.info("Dynamically subscribed Dhan standard feed to %s (SecurityId: %s)", symbol, sec_id_str)
                return True
            except Exception as exc:
                logger.warning("Failed to send dynamic subscription for %s (%s): %s", symbol, sec_id_str, exc)
                return False
        return False

    async def close(self) -> None:
        """Close the WebSocket connection."""
        self.running = False
        self._recv_buffer.clear()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        logger.info("Dhan WebSocket closed.")


class DhanDepthWebSocket:
    """
    Dhan 20-level or 200-level deep market depth WebSocket.

    Connects to the dedicated depth feed and publishes deep order book
    snapshots to the ZMQ bus with topic DEPTH_{symbol}.
    """

    def __init__(
        self,
        client_id: str,
        access_token: str,
        depth_tier: int = 20,
    ):
        self.client_id = client_id
        self.access_token = access_token
        self.depth_tier = depth_tier
        self.zmq_publisher = get_zmq_publisher()
        self.running = False
        self._ws = None
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 50

        self.id_to_symbol: Dict[str, str] = {}
        self._instrument_list: List[Dict[str, str]] = []
        self.cvd_engine = get_cvd_engine()

        # Latest depth snapshots per security_id
        self.depth_snapshots: Dict[str, DeepDepthSnapshot] = {}
        self._recv_buffer: bytearray = bytearray()

    @property
    def active_subscriptions(self) -> List[str]:
        """Return list of actively subscribed security IDs."""
        return [str(inst.get("SecurityId")) for inst in self._instrument_list if inst.get("SecurityId")]

    def set_instrument_list(self, instruments: List[Dict[str, str]]) -> None:
        """Set instrument list, strictly rejecting over-capacity lists."""
        if self.depth_tier == 20:
            if len(instruments) > MAX_INSTRUMENTS_20DEPTH:
                raise ValueError(
                    f"20-depth connection accepts max {MAX_INSTRUMENTS_20DEPTH} instruments, "
                    f"got {len(instruments)}. Selection must occur in ScreenerFunnelService."
                )
        elif self.depth_tier == 200:
            if len(instruments) != 1:
                raise ValueError(
                    f"200-depth connection requires exactly 1 instrument, "
                    f"got {len(instruments)}. Selection must occur in Dhan200DepthManager."
                )
        self._instrument_list = list(instruments)

    def set_symbol_lookup(self, id_to_symbol: Dict[str, str]) -> None:
        self.id_to_symbol = dict(id_to_symbol)

    async def refresh_subscriptions(self, target_instruments: List[Dict[str, str]]) -> Tuple[List[str], List[str]]:
        """
        Dynamically refresh 20-depth subscriptions when the funnel shortlist changes.
        Unsubscribes removed instruments, subscribes new instruments, preserves existing valid subscriptions,
        and clears snapshots for removed instruments atomically.
        """
        if self.depth_tier != 20:
            raise ValueError("Dynamic shortlist refresh is only supported on 20-depth tier")

        if len(target_instruments) > MAX_INSTRUMENTS_20DEPTH:
            raise ValueError(
                f"Target shortlist exceeds 20-depth capacity (max {MAX_INSTRUMENTS_20DEPTH}, got {len(target_instruments)})"
            )

        current_map = {str(inst.get("SecurityId")): inst for inst in self._instrument_list if inst.get("SecurityId")}
        target_map = {str(inst.get("SecurityId")): inst for inst in target_instruments if inst.get("SecurityId")}

        to_unsubscribe_ids = [sid for sid in current_map if sid not in target_map]
        to_subscribe_ids = [sid for sid in target_map if sid not in current_map]

        if not to_unsubscribe_ids and not to_subscribe_ids:
            return [], []

        # 1. Send Unsubscribe if connected
        if to_unsubscribe_ids and self._ws and not getattr(self._ws, "closed", False):
            unsub_list = [current_map[sid] for sid in to_unsubscribe_ids]
            payload = {
                "RequestCode": RequestCode.UNSUBSCRIBE_DEPTH,
                "InstrumentCount": len(unsub_list),
                "InstrumentList": [
                    {
                        "ExchangeSegment": inst.get("ExchangeSegment", "NSE_EQ"),
                        "SecurityId": str(inst.get("SecurityId")),
                    }
                    for inst in unsub_list
                ],
            }
            try:
                await self._ws.send(json.dumps(payload))
                logger.info("Sent 20-depth unsubscription for %d instruments: %s", len(unsub_list), to_unsubscribe_ids)
            except Exception as exc:
                logger.error("Failed to send 20-depth unsubscription: %s", exc)
                return [], []

        # 2. Clear snapshots of removed instruments
        # 3. Send Subscribe if connected
        if to_subscribe_ids and self._ws and not getattr(self._ws, "closed", False):
            sub_list = [target_map[sid] for sid in to_subscribe_ids]
            payload = {
                "RequestCode": RequestCode.DEPTH_20,
                "InstrumentCount": len(sub_list),
                "InstrumentList": [
                    {
                        "ExchangeSegment": inst.get("ExchangeSegment", "NSE_EQ"),
                        "SecurityId": str(inst.get("SecurityId")),
                    }
                    for inst in sub_list
                ],
            }
            try:
                await self._ws.send(json.dumps(payload))
                logger.info("Sent 20-depth subscription for %d instruments: %s", len(sub_list), to_subscribe_ids)
            except Exception as exc:
                logger.error("Failed to send 20-depth subscription: %s", exc)
                return [], []

        # 4. Atomically update instrument list and lookup
        for sid in to_unsubscribe_ids:
            self.depth_snapshots.pop(sid, None)
        for inst in target_instruments:
            sid = str(inst.get("SecurityId") or "")
            sym = inst.get("symbol")
            if sid and sym:
                self.id_to_symbol[sid] = sym
        self._instrument_list = list(target_instruments)
        return to_subscribe_ids, to_unsubscribe_ids

    def build_subscription_payload(self, instruments: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        """
        Build JSON subscription payload per Dhan v2 specifications.

        - 20-depth tier:
            {"RequestCode": 23, "InstrumentCount": N, "InstrumentList": [{"ExchangeSegment": ..., "SecurityId": ...}]}
        - 200-depth tier:
            {"RequestCode": 23, "ExchangeSegment": "...", "SecurityId": "..."}
        """
        target = instruments if instruments is not None else self._instrument_list
        if self.depth_tier == 20:
            if not target:
                raise ValueError("20-depth subscription requires at least 1 instrument")
            if len(target) > MAX_INSTRUMENTS_20DEPTH:
                raise ValueError(
                    f"20-depth subscription payload accepts max {MAX_INSTRUMENTS_20DEPTH} instruments, "
                    f"got {len(target)}. Selection must occur in ScreenerFunnelService."
                )
            return {
                "RequestCode": REQUEST_CODE_SUBSCRIBE_DEPTH,
                "InstrumentCount": len(target),
                "InstrumentList": [
                    {
                        "ExchangeSegment": inst.get("ExchangeSegment", "NSE_EQ"),
                        "SecurityId": str(inst.get("SecurityId")),
                    }
                    for inst in target
                ],
            }
        elif self.depth_tier == 200:
            if not target:
                raise ValueError("200-depth subscription requires exactly 1 instrument, got 0")
            if len(target) != 1:
                raise ValueError(f"200-depth subscription requires exactly 1 instrument, got {len(target)}")
            inst = target[0]
            sec_id = inst.get("SecurityId")
            if not sec_id:
                raise ValueError("200-depth instrument missing SecurityId")
            return {
                "RequestCode": REQUEST_CODE_SUBSCRIBE_DEPTH,
                "ExchangeSegment": inst.get("ExchangeSegment", "NSE_EQ"),
                "SecurityId": str(sec_id),
            }
        else:
            raise ValueError(f"Unsupported depth tier: {self.depth_tier}")

    def _get_ws_url(self) -> str:
        base = DHAN_20DEPTH_URL if self.depth_tier == 20 else DHAN_200DEPTH_URL
        cid = (
            self.client_id
            or os.getenv("DHAN_USER_ID")
            or os.getenv("DHAN_CLIENT_ID")
            or os.getenv("DHAN_API_KEY")
        )
        return build_auth_url(base, self.access_token, str(cid))

    async def _process_depth_stream_bytes(self, chunk: bytes) -> None:
        """Accumulate incoming binary chunks and frame them into complete Dhan depth packets."""
        if not chunk:
            return
        self._recv_buffer.extend(chunk)

        while len(self._recv_buffer) >= DEPTH_HEADER_SIZE:
            declared_len = struct.unpack("<H", self._recv_buffer[0:2])[0]
            feed_code = self._recv_buffer[2]

            known_depth_codes = (ResponseCode.DEPTH_20_BID, ResponseCode.DEPTH_20_ASK, ResponseCode.MARKET_STATUS)
            is_valid_code = feed_code in known_depth_codes
            is_valid_len = (DEPTH_HEADER_SIZE <= declared_len <= 4096)

            if not (is_valid_code and is_valid_len):
                sync_idx = -1
                for i in range(1, len(self._recv_buffer) - 2):
                    if i + 2 < len(self._recv_buffer) and self._recv_buffer[i + 2] in known_depth_codes:
                        plen = struct.unpack("<H", self._recv_buffer[i:i + 2])[0]
                        if DEPTH_HEADER_SIZE <= plen <= 4096:
                            sync_idx = i
                            break
                if sync_idx > 0:
                    del self._recv_buffer[:sync_idx]
                else:
                    del self._recv_buffer[:-2]
                continue

            if len(self._recv_buffer) < declared_len:
                break

            packet = bytes(self._recv_buffer[:declared_len])
            del self._recv_buffer[:declared_len]

            await self._parse_depth_packet(packet)

    async def _parse_depth_packet(self, data: bytes) -> None:
        """
        Parse 20/200-level depth binary packet.

        Depth packet header (12 bytes):
            Bytes 0-1:   Message length (int16le)
            Byte 2:      Feed code (uint8) - 41 for bid, 51 for ask
            Byte 3:      Exchange segment (uint8)
            Bytes 4-7:   Security ID (int32le)
            Bytes 8-11:  Message sequence (20-depth) or No. of Rows (200-depth) (uint32le)
        Data (N levels x 16 bytes each):
            Per level:
                Bytes 0-7:  Price (float64le)
                Bytes 8-11: Quantity (uint32le)
                Bytes 12-15: Orders (uint32le)
        """
        if not data or len(data) < DEPTH_HEADER_SIZE:
            return

        receipt_ts = time.time()

        try:
            declared_length = struct.unpack("<H", data[0:2])[0]
            if declared_length < DEPTH_HEADER_SIZE or declared_length > len(data):
                logger.warning("Invalid depth packet length: declared=%d received=%d", declared_length, len(data))
                if len(data) >= 8:
                    security_id = str(struct.unpack("<i", data[4:8])[0])
                    snapshot = self.depth_snapshots.setdefault(
                        security_id,
                        DeepDepthSnapshot(security_id=security_id, timestamp=receipt_ts),
                    )
                    snapshot.timestamp = receipt_ts
                    snapshot.is_valid = False
                    snapshot.validation_error = "truncated_packet"
                return
            if declared_length < len(data):
                for offset in range(0, len(data), declared_length):
                    packet = data[offset:offset + declared_length]
                    if len(packet) != declared_length:
                        return
                    await self._parse_depth_packet(packet)
                return

            # Parse header
            msg_len = struct.unpack("<h", data[0:2])[0]
            feed_code = struct.unpack("<B", data[2:3])[0]
            exchange_seg = struct.unpack("<B", data[3:4])[0]
            sec_id_int = struct.unpack("<i", data[4:8])[0]
            security_id = str(sec_id_int)
            seq_or_rows = struct.unpack("<I", data[8:12])[0]

            if sec_id_int <= 0:
                logger.debug("Invalid depth packet: non-positive security ID %d", sec_id_int)
                return

            symbol = self.id_to_symbol.get(security_id, security_id)

            # Determine if this is a bid (41) or ask (51) packet
            is_bid = feed_code == 41
            is_ask = feed_code == 51

            if not (is_bid or is_ask):
                logger.debug("Unknown depth feed code: %d", feed_code)
                return

            # Strict packet length & truncation validation
            if msg_len != len(data) or ((len(data) - 12) % DEPTH_LEVEL_SIZE != 0):
                logger.warning("Truncated depth packet for %s: got %d bytes, declared %d", symbol, len(data), msg_len)
                if security_id not in self.depth_snapshots:
                    self.depth_snapshots[security_id] = DeepDepthSnapshot(
                        security_id=security_id,
                        timestamp=receipt_ts,
                    )
                snapshot = self.depth_snapshots[security_id]
                snapshot.is_valid = False
                snapshot.validation_error = "truncated_packet"
                return

            available_levels = (len(data) - 12) // DEPTH_LEVEL_SIZE
            if self.depth_tier == 20:
                num_levels = min(20, available_levels)
            elif self.depth_tier == 200:
                num_levels = min(seq_or_rows if seq_or_rows > 0 else 200, available_levels)
            else:
                num_levels = min(self.depth_tier, available_levels)

            # Parse levels
            levels = []
            offset = 12
            level_size = DEPTH_LEVEL_SIZE

            total_qty = 0
            for i in range(num_levels):
                if offset + level_size > len(data):
                    break
                raw_price, qty, orders = _DEPTH_20_LEVEL_STRUCT.unpack_from(data, offset)
                price = float(raw_price)
                offset += level_size

                if qty > 0 and price > 0:
                    levels.append(DepthLevel(price=price, quantity=qty, orders=orders))
                    total_qty += qty

            # Update or create snapshot
            if security_id not in self.depth_snapshots:
                self.depth_snapshots[security_id] = DeepDepthSnapshot(
                    security_id=security_id,
                    timestamp=receipt_ts,
                )

            snapshot = self.depth_snapshots[security_id]
            snapshot.timestamp = receipt_ts
            snapshot.is_valid = True
            snapshot.validation_error = None

            if is_bid:
                snapshot.bids = levels
                snapshot.total_bid_qty = total_qty
            elif is_ask:
                snapshot.asks = levels
                snapshot.total_ask_qty = total_qty

            # Compute deep OBI when we have both sides
            if snapshot.bids and snapshot.asks:
                total_bid = snapshot.total_bid_qty
                total_ask = snapshot.total_ask_qty
                denom = total_bid + total_ask
                snapshot.deep_obi = (total_bid - total_ask) / denom if denom > 0 else 0.0

                if hasattr(self, "cvd_engine") and self.cvd_engine:
                    try:
                        token_id = int(security_id) if security_id.isdigit() else 0
                        bid_tuples = [(l.price, l.quantity) for l in snapshot.bids]
                        ask_tuples = [(l.price, l.quantity) for l in snapshot.asks]
                        self.cvd_engine.process_deep_depth(token_id, bid_tuples, ask_tuples)
                    except Exception as exc:
                        logger.debug("Failed updating cvd deep depth: %s", exc)

                # Publish depth snapshot
                depth_payload = {
                    "type": "depth",
                    "security_id": security_id,
                    "symbol": symbol,
                    "timestamp": receipt_ts,
                    "depth_tier": self.depth_tier,
                    "total_bid_qty": total_bid,
                    "total_ask_qty": total_ask,
                    "deep_obi": snapshot.deep_obi,
                    "bid_levels": len(snapshot.bids),
                    "ask_levels": len(snapshot.asks),
                    "bids": [
                        {"price": l.price, "qty": l.quantity, "orders": l.orders}
                        for l in snapshot.bids
                    ],
                    "asks": [
                        {"price": l.price, "qty": l.quantity, "orders": l.orders}
                        for l in snapshot.asks
                    ],
                }
                await self.zmq_publisher.publish(f"DEPTH_{symbol}", depth_payload)

        except Exception as exc:
            logger.error("Error parsing depth packet: %s", exc)

    async def _parse_combined_depth(
        self,
        data: bytes,
        security_id: str,
        symbol: str,
        receipt_ts: float,
    ) -> None:
        """
        Parse a combined depth packet that may contain both bid and ask levels
        in a single message. This handles alternative packet formats from Dhan.
        """
        try:
            offset = 12  # Skip header
            level_size = 16
            available_levels = (len(data) - offset) // level_size

            # If we have enough data for both sides, split in half
            half = available_levels // 2
            bids = []
            asks = []
            total_bid_qty = 0
            total_ask_qty = 0

            for i in range(half):
                if offset + level_size > len(data):
                    break
                qty, raw_price, orders = _DEPTH_200_LEVEL_STRUCT.unpack_from(data, offset)
                price = raw_price / 100.0
                offset += level_size
                if qty > 0 and price > 0:
                    bids.append(DepthLevel(price=price, quantity=qty, orders=orders))
                    total_bid_qty += qty

            for i in range(half):
                if offset + level_size > len(data):
                    break
                qty, raw_price, orders = _DEPTH_200_LEVEL_STRUCT.unpack_from(data, offset)
                price = raw_price / 100.0
                offset += level_size
                if qty > 0 and price > 0:
                    asks.append(DepthLevel(price=price, quantity=qty, orders=orders))
                    total_ask_qty += qty

            if bids or asks:
                denom = total_bid_qty + total_ask_qty
                deep_obi = (total_bid_qty - total_ask_qty) / denom if denom > 0 else 0.0

                snapshot = DeepDepthSnapshot(
                    security_id=security_id,
                    timestamp=receipt_ts,
                    bids=bids,
                    asks=asks,
                    total_bid_qty=total_bid_qty,
                    total_ask_qty=total_ask_qty,
                    deep_obi=deep_obi,
                )
                self.depth_snapshots[security_id] = snapshot

                if hasattr(self, "cvd_engine") and self.cvd_engine and bids and asks:
                    try:
                        token_id = int(security_id) if security_id.isdigit() else 0
                        bid_tuples = [(l.price, l.quantity) for l in bids]
                        ask_tuples = [(l.price, l.quantity) for l in asks]
                        self.cvd_engine.process_deep_depth(token_id, bid_tuples, ask_tuples)
                    except Exception as exc:
                        logger.debug("Failed updating cvd deep depth: %s", exc)

                depth_payload = {
                    "type": "depth",
                    "security_id": security_id,
                    "symbol": symbol,
                    "timestamp": receipt_ts,
                    "depth_tier": self.depth_tier,
                    "total_bid_qty": total_bid_qty,
                    "total_ask_qty": total_ask_qty,
                    "deep_obi": deep_obi,
                    "bid_levels": len(bids),
                    "ask_levels": len(asks),
                    "bids": [
                        {"price": l.price, "qty": l.quantity, "orders": l.orders}
                        for l in bids
                    ],
                    "asks": [
                        {"price": l.price, "qty": l.quantity, "orders": l.orders}
                        for l in asks
                    ],
                }
                await self.zmq_publisher.publish(f"DEPTH_{symbol}", depth_payload)

        except Exception as exc:
            logger.error("Error parsing combined depth for %s: %s", symbol, exc)

    def get_depth_snapshot(self, security_id: str) -> Optional[DeepDepthSnapshot]:
        """Get the latest depth snapshot for a given security."""
        return self.depth_snapshots.get(security_id)

    async def connect(self) -> None:
        """Connect to Dhan depth WebSocket."""
        import websockets

        # ── Enforce depth subscription limits ──
        max_instruments = MAX_INSTRUMENTS_20DEPTH if self.depth_tier == 20 else MAX_INSTRUMENTS_200DEPTH
        if len(self._instrument_list) > max_instruments:
            raise ValueError(
                f"Dhan {self.depth_tier}-depth supports max {max_instruments} "
                f"instruments per connection, got {len(self._instrument_list)}."
            )

        self.running = True
        url = self._get_ws_url()
        tier_name = f"{self.depth_tier}-depth"
        logger.info("Connecting to Dhan %s WebSocket (%d instruments)", tier_name, len(self._instrument_list))

        while self.running and self._reconnect_attempts < self._max_reconnect_attempts:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=10,
                    ping_timeout=10,
                    close_timeout=5,
                    max_size=2**20,
                ) as ws:
                    self._ws = ws
                    logger.info("Dhan %s WebSocket connected. Subscribing...", tier_name)

                    sub_payload = self.build_subscription_payload()
                    sub_msg = json.dumps(sub_payload)
                    await ws.send(sub_msg)
                    logger.info("Sent %s subscription payload: %s", tier_name, sub_msg)

                    packet_count = 0
                    self._recv_buffer.clear()
                    async for message in ws:
                        if isinstance(message, bytes):
                            await self._process_depth_stream_bytes(message)
                            packet_count += 1
                            if packet_count >= 5:
                                self._reconnect_attempts = 0
                        else:
                            try:
                                resp = json.loads(message)
                                logger.debug("Dhan %s JSON: %s", tier_name, resp)
                            except json.JSONDecodeError:
                                pass

            except Exception as exc:
                self._reconnect_attempts += 1
                wait_time = min(2 ** self._reconnect_attempts, 60)
                exc_str = str(exc).lower()
                if "429" in exc_str or "rate" in exc_str:
                    wait_time = max(30, wait_time)
                    logger.error(
                        "Dhan %s WebSocket received HTTP 429 Rate Limit. Backing off for %ds (attempt %d/%d)",
                        tier_name, wait_time, self._reconnect_attempts, self._max_reconnect_attempts,
                    )
                else:
                    logger.warning(
                        "Dhan %s disconnected (%s). Reconnect %d/%d in %ds",
                        tier_name, exc, self._reconnect_attempts, self._max_reconnect_attempts, wait_time,
                    )
                await asyncio.sleep(wait_time)

    async def close(self) -> None:
        self.running = False
        self._recv_buffer.clear()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass


class MockDhanWebSocket:
    """Mock tick source for development/testing without live Dhan credentials."""

    def __init__(self, symbols: Optional[List[str]] = None):
        self.zmq_publisher = get_zmq_publisher()
        self.cvd_engine = get_cvd_engine()
        self.regime_engine = get_regime_engine()
        from config.symbols import INDEX_SYMBOLS
        from .dhan_instrument_resolver import NIFTY_100_SYMBOLS
        self.symbols = symbols or NIFTY_100_SYMBOLS[:50]  # Default to first 50
        self.index_symbols = INDEX_SYMBOLS
        self.running = True

        all_symbols = self.symbols + self.index_symbols
        self.token_map = {symbol: abs(hash(symbol)) % 1000000 for symbol in all_symbols}
        self.base_price = {
            "NIFTY 50": 24500.0,
            "INDIA VIX": 13.5,
            **{symbol: 1000.0 + abs(hash(symbol)) % 3000 for symbol in self.symbols}
        }
        self.cum_volume = {symbol: 0 for symbol in all_symbols}

        # Simulate 20-level depth snapshots
        self.depth_snapshots: Dict[str, DeepDepthSnapshot] = {}

    async def simulate_ticks(self):
        """Generate synthetic ticks with optional deep depth data."""
        all_symbols = self.symbols + self.index_symbols
        logger.info(
            "Started MockDhanWebSocket tick simulator for %d symbols (%d indexes)",
            len(self.symbols), len(self.index_symbols),
        )
        while self.running:
            for symbol in all_symbols:
                token = self.token_map[symbol]
                is_index = symbol in self.index_symbols

                # Realistic price moves
                if symbol == "INDIA VIX":
                    move = np.random.randn() * 0.05
                elif symbol == "NIFTY 50":
                    move = np.random.randn() * 15.0
                else:
                    move = np.random.randn() * 1.5

                self.base_price[symbol] = max(1.0, self.base_price[symbol] + move)
                ltp = float(round(self.base_price[symbol], 2))

                tick_vol = int(np.random.randint(10, 120))
                self.cum_volume[symbol] += tick_vol

                best_bid = float(round(ltp - 0.1, 2))
                best_ask = float(round(ltp + 0.1, 2))
                total_bid_qty = int(np.random.randint(500, 5000))
                total_ask_qty = int(np.random.randint(500, 5000))

                payload: dict = {
                    "token": token,
                    "symbol": symbol,
                    "ltp": ltp,
                    "timestamp": time.time(),
                }

                if is_index:
                    payload["is_index"] = True
                    if symbol == "INDIA VIX":
                        self.regime_engine.set_india_vix(ltp)
                else:
                    cvd_result = self.cvd_engine.process_tick(
                        instrument_token=token,
                        last_price=ltp,
                        current_volume=self.cum_volume[symbol],
                        best_bid=best_bid,
                        best_ask=best_ask,
                        total_bid_qty=total_bid_qty,
                        total_ask_qty=total_ask_qty,
                    )
                    payload.update({
                        "volume": self.cum_volume[symbol],
                        "tick_vol": cvd_result.tick_volume,
                        "cvd": cvd_result.cvd,
                        "obi": cvd_result.obi,
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "total_bid_qty": total_bid_qty,
                        "total_ask_qty": total_ask_qty,
                        "bid_price": best_bid,
                        "ask_price": best_ask,
                        "bid_qty": total_bid_qty,
                        "ask_qty": total_ask_qty,
                    })

                    # Generate mock 20-level depth every 5th tick
                    if np.random.randint(0, 5) == 0:
                        mock_bids = []
                        mock_asks = []
                        for i in range(20):
                            bid_price = round(ltp - 0.05 * (i + 1), 2)
                            ask_price = round(ltp + 0.05 * (i + 1), 2)
                            # Simulate institutional wall at random level
                            bid_qty = int(np.random.randint(100, 500))
                            ask_qty = int(np.random.randint(100, 500))
                            if i == np.random.randint(5, 15):
                                bid_qty *= 20  # Institutional wall
                            mock_bids.append(DepthLevel(price=bid_price, quantity=bid_qty))
                            mock_asks.append(DepthLevel(price=ask_price, quantity=ask_qty))

                        total_bid = sum(l.quantity for l in mock_bids)
                        total_ask = sum(l.quantity for l in mock_asks)
                        denom = total_bid + total_ask
                        deep_obi = (total_bid - total_ask) / denom if denom > 0 else 0.0

                        snapshot = DeepDepthSnapshot(
                            security_id=str(token),
                            timestamp=time.time(),
                            bids=mock_bids,
                            asks=mock_asks,
                            total_bid_qty=total_bid,
                            total_ask_qty=total_ask,
                            deep_obi=deep_obi,
                        )
                        self.depth_snapshots[str(token)] = snapshot

                        depth_payload = {
                            "type": "depth",
                            "security_id": str(token),
                            "symbol": symbol,
                            "timestamp": time.time(),
                            "depth_tier": 20,
                            "total_bid_qty": total_bid,
                            "total_ask_qty": total_ask,
                            "deep_obi": deep_obi,
                            "bid_levels": 20,
                            "ask_levels": 20,
                            "bids": [{"price": l.price, "qty": l.quantity, "orders": 0} for l in mock_bids],
                            "asks": [{"price": l.price, "qty": l.quantity, "orders": 0} for l in mock_asks],
                        }
                        await self.zmq_publisher.publish(f"DEPTH_{symbol}", depth_payload)

                await self.zmq_publisher.publish_tick(payload)

            await asyncio.sleep(1)

    async def connect(self):
        await self.simulate_ticks()

    async def subscribe_instrument(
        self,
        symbol: str,
        security_id: str,
        exchange_segment: str = "NSE_EQ",
    ) -> bool:
        """Dynamically add an instrument to the mock simulator."""
        sec_id_str = str(security_id).strip()
        if not sec_id_str:
            return False
        if not hasattr(self, "id_to_symbol"):
            self.id_to_symbol = {}
        self.id_to_symbol[sec_id_str] = symbol
        if symbol not in self.symbols:
            self.symbols.append(symbol)
            token = int(sec_id_str) if sec_id_str.isdigit() else abs(hash(symbol)) % 1000000
            self.token_map[symbol] = token
            self.base_price[symbol] = 1000.0 + abs(hash(symbol)) % 2000
            self.cum_volume[symbol] = 50000
            logger.info("Dynamically subscribed MockDhanWebSocket to %s (%s)", symbol, sec_id_str)
        return True

    async def close(self):
        self.stop()

    def stop(self):
        self.running = False


# ── Factory function ──────────────────────────────────────────────────────────

def get_dhan_ticker(
    client_id: str = None,
    access_token: str = None,
    symbols: Optional[List[str]] = None,
    use_mock: Optional[bool] = None,
):
    """
    Factory function to create the appropriate Dhan ticker.

    Returns MockDhanWebSocket in mock mode, DhanWebSocket in live mode.
    """
    effective_mock = USE_MOCK if use_mock is None else bool(use_mock)

    if effective_mock:
        logger.info("Using MockDhanWebSocket for live tick pipeline")
        return MockDhanWebSocket(symbols=symbols)

    from .dhan_token_service import get_dhan_token
    resolved_client_id = client_id or os.getenv("DHAN_USER_ID") or os.getenv("DHAN_CLIENT_ID") or os.getenv("DHAN_API_KEY")
    resolved_access_token = access_token or get_dhan_token() or os.getenv("DHAN_ACCESS_TOKEN") or os.getenv("DHAN_API_SECRET")

    if not resolved_client_id or not resolved_access_token:
        raise RuntimeError(
            "DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required when USE_DHAN_MOCK=false"
        )

    return DhanWebSocket(resolved_client_id, resolved_access_token)


def get_dhan_depth_ticker(
    client_id: str = None,
    access_token: str = None,
    depth_tier: int = 20,
) -> DhanDepthWebSocket:
    """Create a Dhan depth WebSocket for 20 or 200-level deep book."""
    if depth_tier not in (20, 200):
        raise ValueError(f"depth_tier must be 20 or 200, got {depth_tier}")
    if client_id is not None and not str(client_id).strip():
        raise ValueError("client_id and access_token are required")
    if access_token is not None and not str(access_token).strip():
        raise ValueError("client_id and access_token are required")
    from .dhan_token_service import get_dhan_token
    resolved_client_id = client_id or os.getenv("DHAN_USER_ID") or os.getenv("DHAN_CLIENT_ID") or os.getenv("DHAN_API_KEY")
    resolved_access_token = access_token or get_dhan_token() or os.getenv("DHAN_ACCESS_TOKEN") or os.getenv("DHAN_API_SECRET")
    if not resolved_client_id or not resolved_access_token:
        raise ValueError("client_id and access_token are required")
    return DhanDepthWebSocket(resolved_client_id, resolved_access_token, depth_tier=depth_tier)


class Dhan200DepthManager:
    """
    Manages up to 5 concurrent 200-depth WebSocket connections for top candidates.

    Architecture (Funnel):
      Stage 1: Standard feed (100+ stocks continuously)
      Stage 2: 20-level depth (top 20-50 stocks continuously, max 50/connection)
      Stage 3: 200-level depth (top 5 stocks via this manager, 1 per connection)

    Features:
      - Stable slot assignments (retains incumbents that stay in top 5)
      - Minimum score advantage threshold to prevent rapid churning
      - Replacement cooldown timers
      - Full tracking: active, pending, evicted, skipped, and failed
      - Graceful WebSocket teardown and auto-reconnecting socket tasks
      - Guarantees universe is never silently truncated
    """

    def __init__(
        self,
        client_id: str,
        access_token: str,
        max_connections: int = 5,
        rotation_cooldown_sec: float = 10.0,
        min_score_advantage: float = 0.0,
    ):
        self.client_id = client_id
        self.access_token = access_token
        self.max_connections = min(5, max(1, max_connections))
        self.rotation_cooldown_sec = rotation_cooldown_sec
        self.min_score_advantage = min_score_advantage
        self.active_connections: Dict[str, DhanDepthWebSocket] = {}
        self.connection_tasks: Dict[str, asyncio.Task] = {}
        self.active_metadata: Dict[str, Dict[str, Any]] = {}
        self.pending_candidates: List[Dict[str, Any]] = []
        self.evicted_candidates: List[Dict[str, Any]] = []
        self.skipped_candidates: List[Dict[str, Any]] = []
        self.failed_symbols: List[Dict[str, Any]] = []
        self.last_rotation_ts: float = 0.0
        self.running: bool = False

    async def update_candidates(self, top_candidates: List[Dict[str, Any]]) -> None:
        """
        Update candidate list and rotate 200-depth connections safely.
        top_candidates is a list of instrument dicts sorted by conviction:
        [{"ExchangeSegment": "NSE_EQ", "SecurityId": "1333", "symbol": "HDFCBANK", "score": 88.5}, ...]
        """
        if not self.running:
            return

        now = time.time()
        self.pending_candidates = list(top_candidates)

        # Update scores of existing active connections if present in top_candidates
        candidate_map = {str(c.get("SecurityId")): c for c in top_candidates if c.get("SecurityId")}
        for sec_id, meta in self.active_metadata.items():
            if sec_id in candidate_map:
                meta["score"] = float(candidate_map[sec_id].get("score", meta.get("score", 50.0)))

        # Target instruments: top N (up to max_connections)
        target_candidates = [c for c in top_candidates if c.get("SecurityId")][:self.max_connections]
        target_dict = {str(c.get("SecurityId")): c for c in target_candidates}
        target_ids = set(target_dict.keys())
        current_ids = set(self.active_connections.keys())

        # If stable: no new candidates to add or remove
        if current_ids == target_ids and len(current_ids) > 0:
            return

        to_remove = current_ids - target_ids
        to_add = target_ids - current_ids

        # If under capacity, fill immediately without cooldown constraint
        if len(self.active_connections) < self.max_connections and to_add:
            fill_candidates = [c for c in target_candidates if str(c.get("SecurityId")) in to_add]
            for inst in fill_candidates:
                if len(self.active_connections) >= self.max_connections:
                    break
                sec_id = str(inst.get("SecurityId"))
                symbol = inst.get("symbol", sec_id)
                score = float(inst.get("score", 50.0))
                ws = DhanDepthWebSocket(self.client_id, self.access_token, depth_tier=200)
                ws.set_instrument_list([inst])
                ws.set_symbol_lookup({sec_id: symbol})
                self.active_connections[sec_id] = ws
                self.active_metadata[sec_id] = {
                    "symbol": symbol,
                    "security_id": sec_id,
                    "score": score,
                    "connected_at": now,
                }
                self.connection_tasks[sec_id] = asyncio.create_task(ws.connect())
                logger.info("200-depth manager: initialized slot for security %s (%s)", sec_id, symbol)
            to_add = target_ids - set(self.active_connections.keys())

        if not to_remove and not to_add:
            return

        # Cooldown check for rotations (displacements)
        if self.active_connections and (now - self.last_rotation_ts < self.rotation_cooldown_sec):
            for sec_id in to_add:
                inst = target_dict[sec_id]
                self.skipped_candidates.append({
                    "symbol": inst.get("symbol", sec_id),
                    "security_id": sec_id,
                    "reason": "cooldown_active",
                    "cooldown_remaining": round(self.rotation_cooldown_sec - (now - self.last_rotation_ts), 2),
                    "timestamp": now,
                })
            return

        # Score advantage check: newcomer must beat outgoer by min_score_advantage
        removals_to_execute = []
        additions_to_execute = []

        sorted_to_remove = sorted(
            list(to_remove),
            key=lambda sid: self.active_metadata.get(sid, {}).get("score", 0.0)
        )
        sorted_to_add = sorted(
            [target_dict[sid] for sid in to_add],
            key=lambda inst: float(inst.get("score", 50.0)),
            reverse=True
        )

        for newcomer in sorted_to_add:
            new_id = str(newcomer.get("SecurityId"))
            new_score = float(newcomer.get("score", 50.0))
            new_symbol = newcomer.get("symbol", new_id)

            if sorted_to_remove:
                out_id = sorted_to_remove[0]
                out_score = float(self.active_metadata.get(out_id, {}).get("score", 0.0))
                score_diff = new_score - out_score

                required_advantage = self.min_score_advantage
                # Lower displacement barrier to 0.5 for active breakouts, early ignition, or volume expansion >= 1.3x
                newcomer_metrics = newcomer.get("metrics", {})
                if (
                    newcomer_metrics.get("is_breakout")
                    or newcomer_metrics.get("is_early_ignition")
                    or float(newcomer_metrics.get("volume_ratio", 1.0)) >= 1.30
                ):
                    required_advantage = min(required_advantage, 0.5)

                if required_advantage > 0.0 and score_diff < required_advantage:
                    self.skipped_candidates.append({
                        "symbol": new_symbol,
                        "security_id": new_id,
                        "reason": f"insufficient_score_advantage (diff={score_diff:.2f} < {required_advantage:.2f})",
                        "timestamp": now,
                    })
                    continue

                sorted_to_remove.pop(0)
                removals_to_execute.append(out_id)
                additions_to_execute.append(newcomer)
            elif len(self.active_connections) < self.max_connections:
                additions_to_execute.append(newcomer)

        # Execute removals
        for sec_id in removals_to_execute:
            ws = self.active_connections.pop(sec_id, None)
            if ws:
                ws.running = False
                try:
                    close_coro = ws.close()
                    if asyncio.iscoroutine(close_coro):
                        asyncio.create_task(close_coro)
                except Exception:
                    pass
            task = self.connection_tasks.pop(sec_id, None)
            if task and not task.done():
                task.cancel()
            meta = self.active_metadata.pop(sec_id, {})
            self.evicted_candidates.append({
                "symbol": meta.get("symbol", sec_id),
                "security_id": sec_id,
                "score": meta.get("score", 0.0),
                "evicted_at": now,
            })
            logger.info("200-depth manager: evicted security %s (%s)", sec_id, meta.get("symbol", sec_id))

        # Execute additions
        for inst in additions_to_execute:
            sec_id = str(inst.get("SecurityId"))
            symbol = inst.get("symbol", sec_id)
            score = float(inst.get("score", 50.0))
            ws = DhanDepthWebSocket(self.client_id, self.access_token, depth_tier=200)
            ws.set_instrument_list([inst])
            ws.set_symbol_lookup({sec_id: symbol})
            self.active_connections[sec_id] = ws
            self.active_metadata[sec_id] = {
                "symbol": symbol,
                "security_id": sec_id,
                "score": score,
                "connected_at": now,
            }
            self.connection_tasks[sec_id] = asyncio.create_task(ws.connect())
            logger.info("200-depth manager: connected security %s (%s) with score %.2f", sec_id, symbol, score)

        if removals_to_execute or additions_to_execute:
            self.last_rotation_ts = now

    async def start(self, initial_candidates: Optional[List[Dict[str, str]]] = None) -> None:
        self.running = True
        if initial_candidates:
            await self.update_candidates(initial_candidates)

    async def stop(self) -> None:
        self.running = False
        for sec_id, ws in list(self.active_connections.items()):
            ws.running = False
            try:
                close_coro = ws.close()
                if asyncio.iscoroutine(close_coro):
                    await close_coro
            except Exception:
                pass
        for sec_id, task in list(self.connection_tasks.items()):
            if not task.done():
                task.cancel()
        self.active_connections.clear()
        self.connection_tasks.clear()
        self.active_metadata.clear()

    def get_depth_snapshots(self) -> Dict[str, DeepDepthSnapshot]:
        """Return the latest snapshots from all active 200-depth connections."""
        snapshots: Dict[str, DeepDepthSnapshot] = {}
        for security_id, websocket in self.active_connections.items():
            snapshot = websocket.depth_snapshots.get(str(security_id))
            if snapshot is not None:
                snapshots[str(security_id)] = snapshot
        return snapshots

    def get_rotation_status(self) -> Dict[str, Any]:
        """Telemetry view of 200-depth rotation manager."""
        now = time.time()
        return {
            "max_connections": self.max_connections,
            "active_count": len(self.active_connections),
            "active_candidates": [
                {
                    "symbol": meta.get("symbol", sid),
                    "security_id": sid,
                    "score": meta.get("score", 0.0),
                    "uptime_sec": round(now - meta.get("connected_at", now), 1),
                }
                for sid, meta in self.active_metadata.items()
            ],
            "pending_count": len(self.pending_candidates),
            "evicted_count": len(self.evicted_candidates),
            "evicted_candidates": self.evicted_candidates[-10:],
            "skipped_count": len(self.skipped_candidates),
            "skipped_candidates": self.skipped_candidates[-10:],
            "failed_count": len(self.failed_symbols),
            "last_rotation_ts": self.last_rotation_ts,
        }
