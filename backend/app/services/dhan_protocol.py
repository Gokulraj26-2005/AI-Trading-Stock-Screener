"""
Dhan v2 WebSocket Protocol Contract
=====================================
Authoritative reference for Dhan v2 binary feed layouts, request/response codes,
exchange segments, and subscription limits.

All parsers in dhan_adapter.py MUST use the constants and layouts defined here.
This file is the single source of truth — if the Dhan docs change, update HERE
first, then propagate to parsers.

Sources:
    - https://dhanhq.co/docs/v2/live-market-feed/
    - https://dhanhq.co/docs/v2/full-market-depth/
    - DhanHQ-py official library (binary parsing reference)
"""

from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
# WebSocket URLs
# ──────────────────────────────────────────────────────────────────────────────

DHAN_STANDARD_FEED_URL = "wss://api-feed.dhan.co"
DHAN_20DEPTH_FEED_URL = "wss://depth-api-feed.dhan.co/twentydepth"
DHAN_200DEPTH_FEED_URL = "wss://full-depth-api.dhan.co/twohundreddepth"


def build_auth_url(base_url: str, access_token: str, client_id: str) -> str:
    """Build authenticated WebSocket URL with version=2."""
    return (
        f"{base_url}"
        f"?version=2"
        f"&token={access_token}"
        f"&clientId={client_id}"
        f"&authType=2"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Request Codes (client → Dhan)
# ──────────────────────────────────────────────────────────────────────────────

class RequestCode:
    """JSON subscription request codes sent to Dhan."""
    TICKER = 15            # Subscribe: Ticker mode (LTP only, 16-byte response)
    QUOTE = 17             # Subscribe: Quote mode (OHLCV, 50-byte response)
    FULL = 21              # Subscribe: Full mode (OHLCV + 5-depth, 162-byte response)
    UNSUBSCRIBE = 22       # Unsubscribe from standard feed
    DEPTH_20 = 23          # Subscribe: 20-level market depth
    UNSUBSCRIBE_DEPTH = 24 # Unsubscribe from depth feed


# ──────────────────────────────────────────────────────────────────────────────
# Response Codes (Dhan → client, byte 0 of every binary message)
# ──────────────────────────────────────────────────────────────────────────────

class ResponseCode:
    """Binary response codes in byte 0 of incoming packets."""
    TICKER = 2             # Ticker mode response (16 bytes)
    QUOTE = 4              # Quote mode response (50 bytes)
    OI = 5                 # Open Interest update
    PREV_CLOSE = 6         # Previous close
    FULL = 8               # Full mode response (OHLCV + 5-depth, 162 or 172 bytes)
    MARKET_STATUS = 50     # Market status change
    DEPTH_20_BID = 41      # 20-level depth: bid side
    DEPTH_20_ASK = 51      # 20-level depth: ask side


# ──────────────────────────────────────────────────────────────────────────────
# Exchange Segments
# ──────────────────────────────────────────────────────────────────────────────

class ExchangeSegment:
    """Exchange segment identifiers for subscription and instrument resolution."""
    NSE_EQ = "NSE_EQ"      # NSE Equity (Cash)
    NSE_FNO = "NSE_FNO"    # NSE Futures & Options
    BSE_EQ = "BSE_EQ"      # BSE Equity (Cash)
    BSE_FNO = "BSE_FNO"    # BSE Futures & Options
    MCX_COMM = "MCX_COMM"  # MCX Commodity
    IDX_I = "IDX_I"        # Indices (NIFTY 50, India VIX, SENSEX, etc.)

    # Numeric mapping used in binary packets (byte 3 of header)
    SEGMENT_ID = {
        0: "IDX_I",
        1: "NSE_EQ",
        2: "NSE_FNO",
        3: "BSE_EQ",
        4: "BSE_FNO",
        5: "MCX_COMM",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Subscription Limits
# ──────────────────────────────────────────────────────────────────────────────

# Standard live feed
MAX_INSTRUMENTS_PER_MESSAGE = 100       # Max instruments in a single JSON sub message
MAX_INSTRUMENTS_PER_CONNECTION = 5000   # Max instruments on one WebSocket connection
MAX_CONNECTIONS_PER_USER = 5            # Max concurrent WebSocket connections

# 20-level depth feed
MAX_INSTRUMENTS_20DEPTH = 50            # Max instruments per 20-depth connection

# 200-level depth feed
MAX_INSTRUMENTS_200DEPTH = 1            # MUST be 1 instrument per connection


# ──────────────────────────────────────────────────────────────────────────────
# Standard Feed Binary Packet Layout
# ──────────────────────────────────────────────────────────────────────────────

# Header (8 bytes) — common to all standard feed packets:
#
#   Offset  Size  Type     Field
#   ------  ----  ----     -----
#   0       1     uint8    Response Code (2=Ticker, 4=Quote, 5=OI, 6=PrevClose, 8=Full)
#   1       2     int16le  Message Length
#   3       1     uint8    Exchange Segment (see SEGMENT_ID map)
#   4       4     int32le  Security ID
#
STANDARD_HEADER_SIZE = 8

# Ticker packet: 16 bytes total
#   Header (8 bytes) + Payload (8 bytes)
#
#   Offset  Size  Type      Field
#   ------  ----  ----      -----
#   8       4     float32le LTP
#   12      4     int32le   LTT (epoch seconds)
#
TICKER_PACKET_SIZE = 16

# Prev Close packet: 16 bytes total
#   Header (8 bytes) + Payload (8 bytes)
#
#   Offset  Size  Type      Field
#   ------  ----  ----      -----
#   8       4     float32le Previous Day Close
#   12      4     int32le   Previous Day Open Interest
#
PREV_CLOSE_PACKET_SIZE = 16

# OI packet: 12 bytes total
#   Header (8 bytes) + Payload (4 bytes)
#
#   Offset  Size  Type      Field
#   ------  ----  ----      -----
#   8       4     int32le   Open Interest
#
OI_PACKET_SIZE = 12

# Quote packet: 50 bytes total
#   Header (8 bytes) + Payload (42 bytes)
#
#   Offset  Size  Type      Field
#   ------  ----  ----      -----
#   8       4     float32le LTP
#   12      2     int16le   LTQ (Last Traded Quantity)
#   14      4     int32le   LTT (epoch seconds)
#   18      4     float32le Average Trade Price (ATP)
#   22      4     int32le   Volume
#   26      4     int32le   Total Sell Qty
#   30      4     int32le   Total Buy Qty
#   34      4     float32le Day Open
#   38      4     float32le Day Close
#   42      4     float32le Day High
#   46      4     float32le Day Low
#
QUOTE_PACKET_SIZE = 50

# Full packet: 162 bytes total
#   Header (8 bytes) + OHLCV & OI (54 bytes) + 5-level depth (100 bytes)
#
#   OHLCV & OI section (bytes 8–61):
#   Offset  Size  Type      Field
#   ------  ----  ----      -----
#   8       4     float32le LTP
#   12      2     int16le   LTQ
#   14      4     int32le   LTT (epoch seconds)
#   18      4     float32le Average Trade Price (ATP)
#   22      4     int32le   Volume
#   26      4     int32le   Total Sell Qty
#   30      4     int32le   Total Buy Qty
#   34      4     int32le   Open Interest (OI)
#   38      4     int32le   Highest OI
#   42      4     int32le   Lowest OI
#   46      4     float32le Day Open
#   50      4     float32le Day Close
#   54      4     float32le Day High
#   58      4     float32le Day Low
#
#   5-Level Depth section (bytes 62–161):
#   5 packets of 20 bytes each:
#   Offset  Size  Type      Field
#   ------  ----  ----      -----
#   0       4     int32le   Bid Quantity
#   4       4     int32le   Ask Quantity
#   8       2     int16le   No. of Bid Orders
#   10      2     int16le   No. of Ask Orders
#   12      4     float32le Bid Price
#   16      4     float32le Ask Price
#
FULL_PACKET_SIZE = 162
FULL_DEPTH_OFFSET = 62    # Where 5-level depth starts in a full packet
FULL_DEPTH_LEVEL_SIZE = 20 # 20 bytes per 5-level depth record (bid+ask together)
DEPTH_5_LEVELS = 5


# ──────────────────────────────────────────────────────────────────────────────
# 20-Level & 200-Level Depth Binary Packet Layout
# ──────────────────────────────────────────────────────────────────────────────

# Header (12 bytes):
#   Offset  Size  Type     Field
#   ------  ----  ----     -----
#   0       2     int16le  Message Length (332 for 20-depth, up to 3212 for 200-depth)
#   2       1     uint8    Feed Code (41=Bid, 51=Ask)
#   3       1     uint8    Exchange Segment
#   4       4     int32le  Security ID
#   8       4     uint32le Message Sequence (20-depth) or No. of Rows (200-depth)
#
DEPTH_HEADER_SIZE = 12

# Per-level (16 bytes):
#   Offset  Size  Type      Field
#   ------  ----  ----      -----
#   0       8     float64le Price
#   8       4     uint32le  Quantity
#   12      4     uint32le  Order Count
#
DEPTH_LEVEL_SIZE = 16
DEPTH_20_LEVELS = 20
DEPTH_20_PACKET_SIZE = 12 + DEPTH_20_LEVELS * DEPTH_LEVEL_SIZE  # 332 bytes
DEPTH_200_LEVELS = 200
DEPTH_200_MAX_PACKET_SIZE = 12 + DEPTH_200_LEVELS * DEPTH_LEVEL_SIZE  # 3212 bytes

