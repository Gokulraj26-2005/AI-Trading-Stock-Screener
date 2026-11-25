# -*- coding: utf-8 -*-
"""
Dhan v2 Binary Packet Test Fixtures
====================================
Hand-crafted binary packets for deterministic testing of Dhan parsers.
Each fixture encodes known values matching official Dhan v2 layout specifications:
- Standard Header: 8 bytes (<BhBi: code, length, exchange_segment, security_id)
- Ticker: 16 bytes (<f ltp, <i ltt)
- Quote: 50 bytes (<f ltp, <h ltq, <i ltt, <f avg, <i vol, <i sell_qty, <i buy_qty, <f open, <f close, <f high, <f low)
- OI: 12 bytes (<i oi)
- Prev Close: 16 bytes (<f prev_close, <i prev_oi)
- Full: 162 bytes (8 header + 54 OHLCV/OI + 100 5-level depth [<iihhff])
- Depth (20-tier): 332 bytes (12 header + 20 x 16 bytes [<dII: price float64, qty uint32, orders uint32])
"""
import struct

# ------------------------------------------------------------------------------
# Helper: build standard 8-byte header
# ------------------------------------------------------------------------------

def _standard_header(
    response_code: int,
    msg_length: int,
    exchange_segment: int,
    security_id: int,
) -> bytes:
    """Build an 8-byte standard feed header: <BhBi."""
    return (
        struct.pack("<B", response_code)       # byte 0: response code
        + struct.pack("<h", msg_length)        # bytes 1-2: packet length
        + struct.pack("<B", exchange_segment)  # byte 3: exchange segment
        + struct.pack("<i", security_id)       # bytes 4-7: security ID
    )


def _depth_header(
    msg_length: int,
    feed_code: int,
    exchange_segment: int,
    security_id: int,
    msg_seq: int,
) -> bytes:
    """Build a 12-byte depth feed header: <HBBII."""
    return (
        struct.pack("<H", msg_length)          # bytes 0-1
        + struct.pack("<B", feed_code)         # byte 2 (41=bid, 51=ask)
        + struct.pack("<B", exchange_segment)  # byte 3
        + struct.pack("<I", security_id)       # bytes 4-7
        + struct.pack("<I", msg_seq)           # bytes 8-11
    )


# ------------------------------------------------------------------------------
# Fixture 1: Ticker Packet (16 bytes)
#   RELIANCE, security_id=2885, LTP=2841.50, LTT=1725619200
# ------------------------------------------------------------------------------

TICKER_SECURITY_ID = 2885
TICKER_LTP = 2841.50
TICKER_LTT = 1725619200

TICKER_PACKET = (
    _standard_header(
        response_code=2,     # ResponseCode.TICKER
        msg_length=16,
        exchange_segment=1,  # NSE_EQ
        security_id=TICKER_SECURITY_ID,
    )
    + struct.pack("<f", TICKER_LTP)  # bytes 8-11: float32 LTP
    + struct.pack("<i", TICKER_LTT)  # bytes 12-15: int32 LTT
)
assert len(TICKER_PACKET) == 16, f"Expected 16 bytes, got {len(TICKER_PACKET)}"


# ------------------------------------------------------------------------------
# Fixture 2: Quote Packet (50 bytes)
#   HDFCBANK, security_id=1333, LTP=1623.25
# ------------------------------------------------------------------------------

QUOTE_SECURITY_ID = 1333
QUOTE_LTP = 1623.25
QUOTE_LTQ = 42
QUOTE_LTT = 1725619200
QUOTE_AVG_PRICE = 1620.10
QUOTE_VOLUME = 5_432_100
QUOTE_TOTAL_SELL_QTY = 250_000
QUOTE_TOTAL_BUY_QTY = 310_000
QUOTE_OPEN = 1615.00
QUOTE_CLOSE = 1618.50
QUOTE_HIGH = 1630.75
QUOTE_LOW = 1612.00

QUOTE_PACKET = (
    _standard_header(
        response_code=4,     # ResponseCode.QUOTE
        msg_length=50,
        exchange_segment=1,  # NSE_EQ
        security_id=QUOTE_SECURITY_ID,
    )
    + struct.pack(
        "<fhifiiiffff",
        QUOTE_LTP,
        QUOTE_LTQ,
        QUOTE_LTT,
        QUOTE_AVG_PRICE,
        QUOTE_VOLUME,
        QUOTE_TOTAL_SELL_QTY,
        QUOTE_TOTAL_BUY_QTY,
        QUOTE_OPEN,
        QUOTE_CLOSE,
        QUOTE_HIGH,
        QUOTE_LOW,
    )
)
assert len(QUOTE_PACKET) == 50, f"Expected 50 bytes, got {len(QUOTE_PACKET)}"


# ------------------------------------------------------------------------------
# Fixture 3: Full Packet (162 bytes)
#   TCS, security_id=11536, LTP=3921.80
# ------------------------------------------------------------------------------

FULL_SECURITY_ID = 11536
FULL_LTP = 3921.80
FULL_LTQ = 7
FULL_LTT = 1725619300
FULL_AVG_PRICE = 3915.00
FULL_VOLUME = 1_200_000
FULL_TOTAL_SELL_QTY = 80_000
FULL_TOTAL_BUY_QTY = 95_000
FULL_OI = 45_000
FULL_HIGH_OI = 50_000
FULL_LOW_OI = 40_000
FULL_OPEN = 3910.00
FULL_CLOSE = 3905.50
FULL_HIGH = 3930.00
FULL_LOW = 3895.25

# 5 bid and ask depth levels (each level is a 20-byte record: <iihhff)
# (bid_qty, ask_qty, bid_orders, ask_orders, bid_price, ask_price)
FULL_DEPTH_RECORDS = [
    (500, 400, 12, 10, 3921.50, 3922.00),  # Level 0 (best bid / ask)
    (300, 600, 8, 14, 3921.00, 3922.50),   # Level 1
    (750, 250, 15, 7, 3920.50, 3923.00),   # Level 2
    (200, 800, 5, 18, 3920.00, 3923.50),   # Level 3
    (1000, 150, 22, 4, 3919.50, 3924.00),  # Level 4
]

FULL_BIDS = [(rec[0], rec[4], rec[2]) for rec in FULL_DEPTH_RECORDS]
FULL_ASKS = [(rec[1], rec[5], rec[3]) for rec in FULL_DEPTH_RECORDS]

_full_ohlcv_oi = struct.pack(
    "<fhifiiiiiiffff",
    FULL_LTP,
    FULL_LTQ,
    FULL_LTT,
    FULL_AVG_PRICE,
    FULL_VOLUME,
    FULL_TOTAL_SELL_QTY,
    FULL_TOTAL_BUY_QTY,
    FULL_OI,
    FULL_HIGH_OI,
    FULL_LOW_OI,
    FULL_OPEN,
    FULL_CLOSE,
    FULL_HIGH,
    FULL_LOW,
)
assert len(_full_ohlcv_oi) == 54, f"Expected 54 bytes for OHLCV/OI, got {len(_full_ohlcv_oi)}"

_full_depth = b""
for b_qty, a_qty, b_orders, a_orders, b_price, a_price in FULL_DEPTH_RECORDS:
    _full_depth += struct.pack("<iihhff", b_qty, a_qty, b_orders, a_orders, b_price, a_price)

assert len(_full_depth) == 100, f"Expected 100 bytes for 5 depth levels, got {len(_full_depth)}"

FULL_PACKET = (
    _standard_header(
        response_code=8,     # ResponseCode.FULL
        msg_length=162,
        exchange_segment=1,  # NSE_EQ
        security_id=FULL_SECURITY_ID,
    )
    + _full_ohlcv_oi
    + _full_depth
)
assert len(FULL_PACKET) == 162, f"Expected exactly 162 bytes for Full Packet, got {len(FULL_PACKET)}"


# ------------------------------------------------------------------------------
# Fixture 4: 20-Depth Bid & Ask Packets (332 bytes each)
#   RELIANCE, security_id=2885, 20 levels (<dII: float64 price, uint32 qty, uint32 orders)
# ------------------------------------------------------------------------------

DEPTH_SECURITY_ID = 2885
DEPTH_BID_LEVELS = [
    (1000 + i * 50, round(2841.00 - i * 0.05, 2), 20 + i) for i in range(20)
]

_depth_bid_body = b""
for qty, price, orders in DEPTH_BID_LEVELS:
    _depth_bid_body += struct.pack("<dII", float(price), int(qty), int(orders))

assert len(_depth_bid_body) == 320, f"Expected 320 bytes for 20 depth levels, got {len(_depth_bid_body)}"

DEPTH_BID_PACKET = (
    _depth_header(
        msg_length=332,
        feed_code=41,         # Bid side
        exchange_segment=1,   # NSE_EQ
        security_id=DEPTH_SECURITY_ID,
        msg_seq=1001,
    )
    + _depth_bid_body
)
assert len(DEPTH_BID_PACKET) == 332, f"Expected 332 bytes for Depth Bid Packet, got {len(DEPTH_BID_PACKET)}"

DEPTH_ASK_LEVELS = [
    (800 + i * 40, round(2841.50 + i * 0.05, 2), 15 + i) for i in range(20)
]

_depth_ask_body = b""
for qty, price, orders in DEPTH_ASK_LEVELS:
    _depth_ask_body += struct.pack("<dII", float(price), int(qty), int(orders))

DEPTH_ASK_PACKET = (
    _depth_header(
        msg_length=332,
        feed_code=51,         # Ask side
        exchange_segment=1,
        security_id=DEPTH_SECURITY_ID,
        msg_seq=1002,
    )
    + _depth_ask_body
)
assert len(DEPTH_ASK_PACKET) == 332, f"Expected 332 bytes for Depth Ask Packet, got {len(DEPTH_ASK_PACKET)}"


# ------------------------------------------------------------------------------
# Fixture 5: Malformed packets (for negative testing)
# ------------------------------------------------------------------------------

# Too short - less than 8-byte header
MALFORMED_TOO_SHORT = b"\x02\x10\x00"

# Unknown response code (99)
MALFORMED_UNKNOWN_CODE = _standard_header(
    response_code=99,
    msg_length=16,
    exchange_segment=1,
    security_id=2885,
) + b"\x00" * 8


# ------------------------------------------------------------------------------
# Fixture 6: Index Ticker (NIFTY 50, segment=0 for IDX_I, security_id=13)
# ------------------------------------------------------------------------------

INDEX_SECURITY_ID = 13
INDEX_LTP = 24850.75
INDEX_LTT = 1725619200

INDEX_TICKER_PACKET = (
    _standard_header(
        response_code=2,
        msg_length=16,
        exchange_segment=0,  # IDX_I segment ID
        security_id=INDEX_SECURITY_ID,
    )
    + struct.pack("<f", INDEX_LTP)
    + struct.pack("<i", INDEX_LTT)
)
assert len(INDEX_TICKER_PACKET) == 16


# ------------------------------------------------------------------------------
# Fixture 7: OI Packet (response_code=5, 12 bytes)
# ------------------------------------------------------------------------------

OI_PACKET = (
    _standard_header(
        response_code=5,     # ResponseCode.OI
        msg_length=12,
        exchange_segment=1,
        security_id=TICKER_SECURITY_ID,
    )
    + struct.pack("<i", 150000)  # Open interest
)
assert len(OI_PACKET) == 12


# ------------------------------------------------------------------------------
# Fixture 8: Previous Close Packet (response_code=6, 16 bytes)
# ------------------------------------------------------------------------------

PREV_CLOSE_PACKET = (
    _standard_header(
        response_code=6,     # ResponseCode.PREV_CLOSE
        msg_length=16,
        exchange_segment=1,
        security_id=TICKER_SECURITY_ID,
    )
    + struct.pack("<f", 2900.50)  # Prev close price (float32)
    + struct.pack("<i", 140000)   # Prev OI (int32)
)
assert len(PREV_CLOSE_PACKET) == 16
