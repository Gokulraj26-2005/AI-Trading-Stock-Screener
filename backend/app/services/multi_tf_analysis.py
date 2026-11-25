"""
Multi-Timeframe Chart Analysis Engine for VIA-Agent.

Provides:
- Ingestion of daily historical candles from Dhan API (with local SQLite caching)
- Resampling into 1D (Daily), 1W (Weekly), and 1M (Monthly) OHLCV series
- Vectorized technical indicators: 50 EMA, 50 SMA, 200 SMA, 20 EMA, Weekly/Monthly AVWAP, ATR, Bollinger Squeeze
- Algorithmic pattern recognition: Consolidation zones, VCP coiling, moving average touches/bounces, candlestick patterns
- Potential scoring (0-100) and directional setup classification
- Visual chart annotations: Support/Resistance levels, directional arrows, and confluence zones
- Structured rule-based natural language technical narrative commentary
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
import email.utils
import html
import json
import math
import cachetools
import os
from pathlib import Path
import re
import sqlite3
try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore
import statistics
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

from logger import logger

try:
    from app.app_config import QUANT_CONFIG, QUANT_CONFIG_HASH
except ImportError:
    from app_config import QUANT_CONFIG, QUANT_CONFIG_HASH

from services.institutional_flow_engine import (
    MarketSnapshot,
    InstitutionalFlowResult,
    get_institutional_flow_engine,
)

# Module-level cached service lookups to avoid per-symbol import-lock overhead (OPT-05)
try:
    from services.dhan_instrument_resolver import get_instrument_resolver
except ImportError:
    get_instrument_resolver = None  # type: ignore

try:
    from services.cvd_engine import get_cvd_engine
except ImportError:
    get_cvd_engine = None  # type: ignore

try:
    from services.bulk_deals_service import get_bulk_deals_service
except ImportError:
    get_bulk_deals_service = None  # type: ignore

_IST = ZoneInfo("Asia/Kolkata")
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "historical_candles.sqlite"
DUCKDB_PATH = Path(__file__).resolve().parent.parent / "data" / "historical_candles.duckdb" 
_5M_FETCH_COOLDOWN: Dict[int, float] = {}  # token → last fetch epoch; capped to 500 entries
_5M_FETCH_COOLDOWN_MAX = 500


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class CandleBar:
    time: int  # UTC timestamp in seconds (start of bar)
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "time": self.time,
            "open": round(self.open, 2),
            "high": round(self.high, 2),
            "low": round(self.low, 2),
            "close": round(self.close, 2),
            "volume": round(self.volume, 2),
        }


@dataclass(slots=True)
class ChartMarker:
    time: int
    position: str  # 'aboveBar' | 'belowBar' | 'inBar'
    color: str
    shape: str  # 'arrowUp' | 'arrowDown' | 'circle'
    text: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PriceLine:
    price: float
    label: str
    color: str
    line_style: str = "dashed"  # 'solid' | 'dashed' | 'dotted'
    line_width: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "price": round(self.price, 2),
            "label": self.label,
            "color": self.color,
            "lineStyle": self.line_style,
            "lineWidth": self.line_width,
        }


@dataclass(slots=True)
class TechnicalCommentary:
    structure_base: str
    multi_tf_trend: str
    indicator_interaction: str
    volatility_state: str
    actionable_thesis: str
    bias: str  # BULLISH_BREAKOUT | 50EMA_BOUNCE | MONTHLY_VWAP_TEST | BEARISH_BREAKDOWN | SQUEEZE_COIL | NEUTRAL_CHOP
    potential_score: int  # 0 to 100
    quality_tier: str  # 'A+' | 'A' | 'B' | 'C'
    trigger_price: Optional[float] = None
    target_price_1: Optional[float] = None
    target_price_2: Optional[float] = None
    stop_loss: Optional[float] = None
    latest_news: str = "No news in past 24h"
    institutional_flow: str = "Neutral / Balanced Flow"
    institutional_flow_detail: Optional[Dict[str, Any]] = None
    sentiment_detail: Optional[Dict[str, Any]] = None
    narrative_alignment: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── SQLite Caching Layer ──────────────────────────────────────────────────────

class HistoricalCandleCache:
    """Manages fast DuckDB columnar storage of daily candles with SQLite fallback for news & persistence."""

    def __init__(self, db_path: Path = DB_PATH, duckdb_path: Optional[Path] = None):
        self.db_path = Path(db_path)
        if duckdb_path is None:
            if self.db_path != DB_PATH:
                self.duckdb_path = self.db_path.with_suffix(".duckdb")
            else:
                self.duckdb_path = DUCKDB_PATH
        else:
            self.duckdb_path = Path(duckdb_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._init_duckdb()
        self._mem_cache: cachetools.TTLCache = cachetools.TTLCache(maxsize=500, ttl=300.0)

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("""
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
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_daily_sym_time 
                ON daily_candles (symbol, timestamp)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_news_cache (
                    symbol TEXT PRIMARY KEY,
                    last_pulled_ts REAL NOT NULL,
                    display_summary TEXT NOT NULL,
                    articles_json TEXT NOT NULL
                )
            """)
            conn.commit()

    def _init_duckdb(self) -> None:
        if duckdb is None:
            self._duck_conn = None
            return
        try:
            self._duck_conn = duckdb.connect(str(self.duckdb_path))
            self._duck_conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_candles (
                    symbol VARCHAR NOT NULL,
                    timestamp BIGINT NOT NULL,
                    open DOUBLE NOT NULL,
                    high DOUBLE NOT NULL,
                    low DOUBLE NOT NULL,
                    close DOUBLE NOT NULL,
                    volume DOUBLE NOT NULL,
                    PRIMARY KEY (symbol, timestamp)
                )
            """)
        except Exception as exc:
            logger.warning("Failed initializing DuckDB cache: %s. Falling back to SQLite.", exc)
            self._duck_conn = None

    def get_news_cache(self, symbol: str) -> Optional[Tuple[float, str, List[Dict[str, Any]]]]:
        """Retrieve persisted news cache for symbol from SQLite."""
        upper = symbol.strip().upper()
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT last_pulled_ts, display_summary, articles_json FROM stock_news_cache WHERE symbol = ?",
                    (upper,)
                ).fetchone()
                if row:
                    last_pulled_ts, display_summary, articles_json = row
                    articles = json.loads(articles_json) if articles_json else []
                    return float(last_pulled_ts), str(display_summary), articles
        except Exception as exc:
            logger.debug("Failed reading news cache from SQLite for %s: %s", upper, exc)
        return None

    def save_news_cache(self, symbol: str, last_pulled_ts: float, display_summary: str, articles: List[Dict[str, Any]]) -> None:
        """Persist news cache and article timestamps for symbol into SQLite."""
        upper = symbol.strip().upper()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO stock_news_cache (symbol, last_pulled_ts, display_summary, articles_json)
                    VALUES (?, ?, ?, ?)
                """, (upper, float(last_pulled_ts), str(display_summary), json.dumps(articles)))
                conn.commit()
        except Exception as exc:
            logger.debug("Failed saving news cache to SQLite for %s: %s", upper, exc)

    def get_candles(self, symbol: str) -> List[CandleBar]:
        upper = symbol.strip().upper()
        if hasattr(self, "_mem_cache") and upper in self._mem_cache:
            return self._mem_cache[upper]

        rows = None
        if getattr(self, "_duck_conn", None) is not None:
            try:
                cur = self._duck_conn.cursor()
                rows = cur.execute("""
                    SELECT timestamp, open, high, low, close, volume
                    FROM daily_candles
                    WHERE symbol = ?
                    ORDER BY timestamp ASC
                """, [upper]).fetchall()
            except Exception as exc:
                logger.debug("DuckDB get_candles query failed for %s: %s, falling back to SQLite", upper, exc)
                rows = None

        if rows is None:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT timestamp, open, high, low, close, volume
                    FROM daily_candles
                    WHERE symbol = ?
                    ORDER BY timestamp ASC
                """, (upper,))
                rows = cursor.fetchall()

        # Deduplicate by calendar date (keeps latest bar for any single day, preventing ghost duplicates)
        seen_dates = set()
        clean_bars = []
        for r in reversed(rows):
            dt = datetime.fromtimestamp(r[0], tz=_IST).date()
            if dt not in seen_dates:
                seen_dates.add(dt)
                clean_bars.append(CandleBar(r[0], r[1], r[2], r[3], r[4], r[5]))
        clean_bars.reverse()
        if getattr(self, "_mem_cache", None) is None:
            self._mem_cache = cachetools.TTLCache(maxsize=500, ttl=300.0)
        self._mem_cache[upper] = clean_bars
        return clean_bars

    def save_candles(self, symbol: str, candles: List[CandleBar], overwrite: bool = False) -> None:
        if not candles:
            return
        upper = symbol.strip().upper()
        if getattr(self, "_mem_cache", None) is not None:
            self._mem_cache.pop(upper, None)

        rows = [(upper, c.time, c.open, c.high, c.low, c.close, c.volume) for c in candles]

        # Save to DuckDB if available
        if getattr(self, "_duck_conn", None) is not None:
            try:
                cur = self._duck_conn.cursor()
                if overwrite:
                    cur.execute("DELETE FROM daily_candles WHERE symbol = ?", [upper])
                cur.executemany("""
                    INSERT OR REPLACE INTO daily_candles 
                    (symbol, timestamp, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, rows)
            except Exception as exc:
                logger.warning("DuckDB save_candles failed for %s: %s", upper, exc)

        with sqlite3.connect(self.db_path) as conn:
            if overwrite:
                conn.execute("DELETE FROM daily_candles WHERE symbol = ?", (upper,))
            conn.executemany("""
                INSERT OR REPLACE INTO daily_candles 
                (symbol, timestamp, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()


# ── Multi-Timeframe Resampling Engine ─────────────────────────────────────────

def resample_weekly(daily_bars: List[CandleBar]) -> List[CandleBar]:
    """Resample daily bars into ISO calendar weekly bars (Monday to Friday)."""
    if not daily_bars:
        return []

    weeks: Dict[Tuple[int, int], List[CandleBar]] = {}
    for bar in daily_bars:
        dt = datetime.fromtimestamp(bar.time, tz=_IST)
        iso_year, iso_week, _ = dt.isocalendar()
        key = (iso_year, iso_week)
        if key not in weeks:
            weeks[key] = []
        weeks[key].append(bar)

    weekly_bars: List[CandleBar] = []
    for (iso_year, iso_week), bars in sorted(weeks.items()):
        bars.sort(key=lambda b: b.time)
        # Anchor bar time to the Monday 09:15 IST of this ISO week
        dt0 = datetime.fromtimestamp(bars[0].time, tz=_IST)
        monday_dt = dt0 - timedelta(days=dt0.weekday())
        monday_open = datetime(monday_dt.year, monday_dt.month, monday_dt.day, 9, 15, tzinfo=_IST)

        w_open = bars[0].open
        w_high = max(b.high for b in bars)
        w_low = min(b.low for b in bars)
        w_close = bars[-1].close
        w_vol = sum(b.volume for b in bars)
        weekly_bars.append(CandleBar(
            time=int(monday_open.timestamp()),
            open=round(w_open, 2),
            high=round(w_high, 2),
            low=round(w_low, 2),
            close=round(w_close, 2),
            volume=round(w_vol, 2),
        ))
    return weekly_bars


def resample_monthly(daily_bars: List[CandleBar]) -> List[CandleBar]:
    """Resample daily bars into calendar monthly bars."""
    if not daily_bars:
        return []

    months: Dict[Tuple[int, int], List[CandleBar]] = {}
    for bar in daily_bars:
        dt = datetime.fromtimestamp(bar.time, tz=_IST)
        key = (dt.year, dt.month)
        if key not in months:
            months[key] = []
        months[key].append(bar)

    monthly_bars: List[CandleBar] = []
    for (year, month), bars in sorted(months.items()):
        bars.sort(key=lambda b: b.time)
        month_open_dt = datetime(year, month, 1, 9, 15, tzinfo=_IST)
        m_open = bars[0].open
        m_high = max(b.high for b in bars)
        m_low = min(b.low for b in bars)
        m_close = bars[-1].close
        m_vol = sum(b.volume for b in bars)
        monthly_bars.append(CandleBar(
            time=int(month_open_dt.timestamp()),
            open=round(m_open, 2),
            high=round(m_high, 2),
            low=round(m_low, 2),
            close=round(m_close, 2),
            volume=round(m_vol, 2),
        ))
    return monthly_bars


# ── Vectorized Indicator Mathematics ──────────────────────────────────────────

def compute_sma(closes: List[float], period: int) -> List[Optional[float]]:
    """Compute Simple Moving Average."""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) < period:
        return out

    rolling_sum = sum(closes[:period])
    out[period - 1] = round(rolling_sum / period, 2)
    for i in range(period, len(closes)):
        rolling_sum += closes[i] - closes[i - period]
        out[i] = round(rolling_sum / period, 2)
    return out


def compute_ema(closes: List[float], period: int) -> List[Optional[float]]:
    """Compute Exponential Moving Average seeded by SMA."""
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) < period:
        return out

    alpha = 2.0 / (period + 1.0)
    sma_seed = sum(closes[:period]) / period
    out[period - 1] = round(sma_seed, 2)
    current_ema = sma_seed

    for i in range(period, len(closes)):
        current_ema = alpha * closes[i] + (1.0 - alpha) * current_ema
        out[i] = round(current_ema, 2)
    return out


def compute_anchored_vwap(bars: List[CandleBar], anchor_mode: str = "monthly") -> List[Optional[float]]:
    """
    Compute Periodic Anchored VWAP.
    anchor_mode: 'monthly' (resets 1st of month), 'yearly' (resets Jan 1),
    'multi_year' (2-year cycle), or 'weekly' (resets Monday).
    """
    out: List[Optional[float]] = []
    cum_pv = 0.0
    cum_vol = 0.0
    last_anchor_key: Any = None

    for bar in bars:
        dt = datetime.fromtimestamp(bar.time, tz=_IST)
        if anchor_mode == "monthly":
            curr_anchor_key = (dt.year, dt.month)
        elif anchor_mode == "yearly":
            curr_anchor_key = dt.year
        elif anchor_mode == "multi_year":
            curr_anchor_key = dt.year // 2
        else:  # weekly
            iso = dt.isocalendar()
            curr_anchor_key = (iso[0], iso[1])

        if curr_anchor_key != last_anchor_key:
            cum_pv = 0.0
            cum_vol = 0.0
            last_anchor_key = curr_anchor_key

        typical_price = (bar.high + bar.low + bar.close) / 3.0
        if bar.volume > 0:
            vol = bar.volume
            cum_pv += typical_price * vol
            cum_vol += vol

        vwap_val = round(cum_pv / cum_vol, 2) if cum_vol > 0 else round(typical_price, 2)
        out.append(vwap_val)
    return out


def compute_bollinger_squeeze(bars: List[CandleBar], period: int = 20, lookback_rank: int = 120) -> List[Optional[float]]:
    """
    Compute Bollinger BandWidth Squeeze Percentile (0 to 100).
    Values <= 20 indicate severe consolidation squeeze.
    """
    closes = [b.close for b in bars]
    n = len(closes)
    bandwidths: List[Optional[float]] = [None] * n
    squeeze_percentiles: List[Optional[float]] = [None] * n

    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        mean = sum(window) / period
        # Reverted to use population variance (period) to align with standard charting tools
        variance = sum((x - mean) ** 2 for x in window) / period if period > 0 else 0.0
        std = math.sqrt(variance)
        bw = (4.0 * std) / mean if mean > 0 else 0.0
        bandwidths[i] = bw

    for i in range(period - 1, n):
        hist_start = max(period - 1, i - lookback_rank + 1)
        hist = [bandwidths[j] for j in range(hist_start, i + 1) if bandwidths[j] is not None]
        if hist and bandwidths[i] is not None:
            curr = bandwidths[i]
            count_lower = sum(1 for x in hist if x <= curr)
            pct = round((count_lower / len(hist)) * 100.0, 1)
            squeeze_percentiles[i] = pct

    return squeeze_percentiles


def compute_atr(bars: List[CandleBar], period: int = 14) -> List[Optional[float]]:
    """Compute Wilder's Average True Range."""
    n = len(bars)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    trs: List[float] = [bars[0].high - bars[0].low]
    for i in range(1, n):
        h = bars[i].high
        l = bars[i].low
        prev_c = bars[i - 1].close
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)

    atr = sum(trs[:period]) / period
    out[period - 1] = round(atr, 2)
    for i in range(period, n):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = round(atr, 2)
    return out


# ── Pattern Recognition & Technical Logic ────────────────────────────────────

@dataclass(slots=True)
class ConsolidationPattern:
    is_consolidating: bool
    resistance: float
    support: float
    range_span_pct: float
    duration_bars: int
    start_time: int
    end_time: int
    containment_ratio: float


def detect_consolidation(bars: List[CandleBar], max_span_pct: float = 8.5) -> ConsolidationPattern:
    """
    Evaluates recent price action for horizontal consolidation.
    Checks windows from 15 to 45 bars to find the tightest sustained base.
    """
    if len(bars) < 15:
        return ConsolidationPattern(False, 0.0, 0.0, 0.0, 0, 0, 0, 0.0)

    best_match: Optional[ConsolidationPattern] = None

    for window_len in (15, 20, 30, 45):
        if len(bars) < window_len:
            continue
        window = bars[-window_len:]
        highs = [b.high for b in window]
        lows = [b.low for b in window]
        closes = [b.close for b in window]

        resistance = max(highs)
        support = min(lows)
        midpoint = (resistance + support) / 2.0
        if midpoint <= 0:
            continue

        span_pct = round(((resistance - support) / midpoint) * 100.0, 2)
        # FLAW-01: Test genuine tight base compression by evaluating containment
        # against the central 80% inner corridor (10% buffer off support and resistance).
        corridor_margin = (resistance - support) * 0.10
        inner_sup = support + corridor_margin
        inner_res = resistance - corridor_margin
        inside_count = sum(1 for c in closes if inner_sup <= c <= inner_res)
        containment = inside_count / window_len

        if span_pct <= max_span_pct and containment >= 0.65:
            pattern = ConsolidationPattern(
                is_consolidating=True,
                resistance=round(resistance, 2),
                support=round(support, 2),
                range_span_pct=span_pct,
                duration_bars=window_len,
                start_time=window[0].time,
                end_time=window[-1].time,
                containment_ratio=round(containment, 2),
            )
            if best_match is None or pattern.duration_bars > best_match.duration_bars:
                best_match = pattern

    if best_match:
        return best_match

    # Fallback to last 18 bars stats even if span is wider
    recent = bars[-18:]
    res = max(b.high for b in recent)
    sup = min(b.low for b in recent)
    mid = (res + sup) / 2.0
    span = round(((res - sup) / mid) * 100.0, 2) if mid > 0 else 0.0
    return ConsolidationPattern(
        is_consolidating=False,
        resistance=round(res, 2),
        support=round(sup, 2),
        range_span_pct=span,
        duration_bars=len(recent),
        start_time=recent[0].time,
        end_time=recent[-1].time,
        containment_ratio=0.75,
    )


def evaluate_ma_interaction(
    current_price: float,
    current_low: float,
    current_high: float,
    current_open: float,
    ema50: Optional[float],
    sma50: Optional[float],
    avwap: Optional[float],
    atr: Optional[float] = None,
) -> Dict[str, Any]:
    """Check if price is touching, bouncing, or extended from key moving averages."""
    status = {
        "touching_ema50": False,
        "bouncing_ema50": False,
        "dist_ema50_pct": 0.0,
        "touching_avwap": False,
        "bouncing_avwap": False,
        "dist_avwap_pct": 0.0,
        "confluence_shelf": False,
        "confluence_level": None,
    }

    atr_val = atr if (atr and atr > 0) else max(current_price * 0.015, QUANT_CONFIG["doji_abs_span_floor"])
    touch_limit = QUANT_CONFIG.get("ma_touch_atr_mult", 0.25) * atr_val

    if ema50 and ema50 > 0:
        dist_ema = ((current_price - ema50) / ema50) * 100.0
        status["dist_ema50_pct"] = round(dist_ema, 2)
        # Volatility-normalized touch condition (within ATR window or low tested EMA)
        if abs(current_price - ema50) <= touch_limit or (current_low <= ema50 + (0.10 * atr_val) and current_price >= ema50):
            status["touching_ema50"] = True
            # Bounce condition (rejection wick off low)
            min_span = max(QUANT_CONFIG["doji_abs_span_floor"], QUANT_CONFIG["doji_body_bps"] * current_price)
            total_range = max(current_high - current_low, min_span)
            wick_ratio = (current_price - current_low) / total_range
            if (current_price - current_open) >= min_span and wick_ratio >= 0.55:
                status["bouncing_ema50"] = True

    if avwap and avwap > 0:
        dist_vwap = ((current_price - avwap) / avwap) * 100.0
        status["dist_avwap_pct"] = round(dist_vwap, 2)
        if abs(current_price - avwap) <= touch_limit or (current_low <= avwap + (0.10 * atr_val) and current_price >= avwap):
            status["touching_avwap"] = True
            min_span = max(QUANT_CONFIG["doji_abs_span_floor"], QUANT_CONFIG["doji_body_bps"] * current_price)
            total_range = max(current_high - current_low, min_span)
            wick_ratio = (current_price - current_low) / total_range
            if (current_price - current_open) >= min_span and wick_ratio >= 0.55:
                status["bouncing_avwap"] = True

    # Confluence shelf: EMA50 and Monthly AVWAP aligned within 0.75% or 0.25 ATR
    if ema50 and avwap and ema50 > 0 and avwap > 0:
        diff_pts = abs(ema50 - avwap)
        if diff_pts <= touch_limit or (diff_pts / ema50 * 100.0 <= 0.75):
            status["confluence_shelf"] = True
            status["confluence_level"] = round((ema50 + avwap) / 2.0, 2)

    return status


def detect_candle_pattern(bar: CandleBar) -> Optional[str]:
    """Detect single candle formations (Hammer, Rejection, Engulfing)."""
    body = abs(bar.close - bar.open)
    min_body = max(QUANT_CONFIG["doji_abs_span_floor"], QUANT_CONFIG["doji_body_bps"] * bar.close)
    effective_body = max(body, min_body)
    total_range = max(bar.high - bar.low, QUANT_CONFIG["doji_abs_span_floor"])
    lower_wick = min(bar.open, bar.close) - bar.low
    upper_wick = bar.high - max(bar.open, bar.close)

    # Bullish Hammer / Pin Bar
    if lower_wick >= 2.0 * effective_body and upper_wick <= 0.35 * total_range:
        return "Bullish Hammer Rejection"
    # Bearish Shooting Star
    if upper_wick >= 2.0 * effective_body and lower_wick <= 0.35 * total_range:
        return "Bearish Shooting Star"
    # Doji / Indecision
    if body <= 0.10 * total_range:
        return "Doji Compression"
    return None


# ── Stock News Caching Layer & Adaptive Trading-Hours Fetcher ─────────────────

_NEWS_CACHE: Dict[str, Tuple[float, str, List[Dict[str, Any]]]] = {}
_NEWS_CACHE_TTL = 900  # 15 minutes


def _is_trading_hours_ist(now_dt: Optional[datetime] = None) -> bool:
    """Returns True if current time is within Indian market trading hours (09:00 - 16:00 IST, Mon-Fri)."""
    if now_dt is None:
        now_dt = datetime.now(_IST)
    if now_dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now_dt.time()
    return dt_time(9, 0) <= t < dt_time(16, 0)


def _normalize_title_for_dedup(title: str) -> str:
    """Normalize headline for robust cross-fetch deduplication."""
    return re.sub(r"[^\w\s]", "", title.lower()).strip()


# Conglomerate & Sister-Company Disambiguation Map
# Prevents news from other entities in the same group or generic market roundups from bleeding into the stock
CONGLOMERATE_DISAMBIGUATION_MAP: Dict[str, Dict[str, List[str]]] = {
    "RELIANCE": {
        "exclude_terms": [
            "jio financial", "jiofin", "jio platform", "rpower", "reliance power",
            "reliance infra", "reliance capital", "reliance communications", "rcom"
        ],
        "must_have_if_excluded": ["reliance industries", "ril"],
        "reject_standalone": ["jio"],  # reject standalone 'Jio' if 'reliance' is not in title
    },
    "JIOFIN": {
        "exclude_terms": ["reliance industries", "ril", "reliance retail"],
        "must_have_if_excluded": ["jio financial", "jiofin"],
    },
    "TATACHEM": {
        "exclude_terms": ["tata motors", "tata steel", "tata power", "tata consumer", "tata elxsi", "tata tech", "tcs"],
        "must_have_if_excluded": ["tata chemicals", "tata chem"],
    },
    "TATAMOTORS": {
        "exclude_terms": ["tata steel", "tata chemicals", "tata power", "tata consumer", "tata elxsi", "tcs"],
        "must_have_if_excluded": ["tata motors", "tatamotors"],
    },
    "TATASTEEL": {
        "exclude_terms": ["tata motors", "tata chemicals", "tata power", "tata consumer", "tcs"],
        "must_have_if_excluded": ["tata steel", "tatasteel"],
    },
    "TATAPOWER": {
        "exclude_terms": ["tata motors", "tata steel", "tata chemicals", "tata consumer", "tcs"],
        "must_have_if_excluded": ["tata power", "tatapower"],
    },
    "TATACONSUM": {
        "exclude_terms": ["tata motors", "tata steel", "tata chemicals", "tata power", "tcs"],
        "must_have_if_excluded": ["tata consumer", "tataconsum"],
    },
    "HDFCBANK": {
        "exclude_terms": ["hdfc life", "hdfc amc", "hdfc ergo", "hdfc mutual fund"],
        "must_have_if_excluded": ["hdfc bank"],
    },
    "HDFCLIFE": {
        "exclude_terms": ["hdfc bank", "hdfc amc", "hdfc ergo"],
        "must_have_if_excluded": ["hdfc life"],
    },
    "BAJFINANCE": {
        "exclude_terms": ["bajaj auto", "bajaj finserv", "bajaj electricals", "bajaj consumer"],
        "must_have_if_excluded": ["bajaj finance"],
    },
    "BAJAJFINSV": {
        "exclude_terms": ["bajaj auto", "bajaj finance", "bajaj electricals"],
        "must_have_if_excluded": ["bajaj finserv"],
    },
    "BAJAJ-AUTO": {
        "exclude_terms": ["bajaj finance", "bajaj finserv"],
        "must_have_if_excluded": ["bajaj auto"],
    },
    "ADANIENT": {
        "exclude_terms": ["adani ports", "adani power", "adani green", "adani total", "adani wilmar", "adani energy"],
        "must_have_if_excluded": ["adani enterprises", "adanient"],
    },
    "ADANIPORTS": {
        "exclude_terms": ["adani enterprises", "adani power", "adani green", "adani total", "adani wilmar"],
        "must_have_if_excluded": ["adani ports", "adani port"],
    },
    "ADANIPOWER": {
        "exclude_terms": ["adani ports", "adani enterprises", "adani green", "adani total", "adani wilmar"],
        "must_have_if_excluded": ["adani power"],
    },
    "ADANIGREEN": {
        "exclude_terms": ["adani ports", "adani enterprises", "adani power", "adani total", "adani wilmar"],
        "must_have_if_excluded": ["adani green"],
    },
    "JSWSTEEL": {
        "exclude_terms": ["jsw energy", "jsw infrastructure", "jsw infra"],
        "must_have_if_excluded": ["jsw steel"],
    },
    "JSWENERGY": {
        "exclude_terms": ["jsw steel", "jsw infrastructure", "jsw infra"],
        "must_have_if_excluded": ["jsw energy"],
    },
}


def get_company_name_for_symbol(symbol: str) -> str:
    """Resolves official company name from Dhan instrument master or canonical name."""
    sym_upper = symbol.strip().upper()
    try:
        from services.dhan_instrument_resolver import get_instrument_resolver
        resolver = get_instrument_resolver()
        sec_id = resolver.get_security_id(sym_upper)
        if sec_id and hasattr(resolver, "_id_to_name") and sec_id in resolver._id_to_name:
            name = resolver._id_to_name[sec_id]
            if name and name.strip():
                return name.strip()
    except Exception:
        pass
    return sym_upper


def is_article_strictly_relevant(symbol: str, company_name: str, title: str) -> bool:
    """
    Ensures a news headline specifically and directly pertains to the target company.
    Rejects:
    1. News from sister/group companies (e.g. Jio Financial for Reliance; Tata Motors for Tata Chem).
    2. Generic market roundups/listicles without specific company focus.
    3. Unrelated articles matching keyword fragments.
    """
    t_lower = title.lower()
    sym_lower = symbol.lower()
    comp_lower = company_name.lower()

    # 1. Conglomerate sister-company disambiguation
    rule = CONGLOMERATE_DISAMBIGUATION_MAP.get(symbol.upper())
    if rule:
        for ex in rule.get("exclude_terms", []):
            if ex in t_lower:
                if not any(mh in t_lower for mh in rule.get("must_have_if_excluded", [])):
                    return False
        for rej in rule.get("reject_standalone", []):
            if re.search(rf"\b{rej}\b", t_lower) and sym_lower not in t_lower and comp_lower not in t_lower:
                return False

    # 2. Generic market noise / listicle filter
    GENERIC_NOISE = [
        "why is market rallying", "why is market falling", "sensex jumps", "sensex drops",
        "nifty closes", "nifty opens", "market wrap", "stocks to watch: 10", "stocks to watch on",
        "top gainers and losers", "buzzing stocks today"
    ]
    if any(noise in t_lower for noise in GENERIC_NOISE):
        if comp_lower not in t_lower and not re.search(rf"\b{sym_lower}\b", t_lower):
            return False

    # 3. Direct company presence check
    name_tokens = [
        tok for tok in re.split(r"\s+", comp_lower)
        if len(tok) >= 4 and tok not in ["limited", "ltd", "india", "corp", "corporation", "industries"]
    ]

    has_symbol = bool(re.search(rf"\b{sym_lower}\b", t_lower))
    has_full_name = comp_lower in t_lower
    has_name_token = any(re.search(rf"\b{tok}\b", t_lower) for tok in name_tokens) if name_tokens else False
    has_specific_alias = (
        symbol.upper() == "RELIANCE" and (bool(re.search(r"\bril\b", t_lower)) or "reliance" in t_lower)
    )

    if not (has_symbol or has_full_name or has_name_token or has_specific_alias):
        return False

    return True


def _fetch_stock_news_from_google(symbol: str, window_str: str) -> List[Dict[str, Any]]:
    """Fetch raw articles from Google News RSS for a given symbol and time window with strict relevance."""
    upper = symbol.strip().upper()
    company_name = get_company_name_for_symbol(upper)

    # Use exact company name and symbol query for high signal-to-noise ratio
    query = f'"{company_name}" OR "{upper}" stock share NSE when:{window_str}'
    url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
    )
    reg_keywords = (
        "announcement", "filing", "board", "result", "dividend", "sebi", 
        "order", "acquisition", "stake", "approval", "merger", "patent", "quarterly"
    )
    articles_raw: List[Dict[str, Any]] = []

    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            xml_data = response.read()
        root = ET.fromstring(xml_data)
        now_utc = datetime.now(timezone.utc)

        for item in root.findall("./channel/item"):
            title = item.findtext("title", "").strip()
            pub_date_str = item.findtext("pubDate", "").strip()
            source = item.findtext("source", "").strip()
            if not title or not pub_date_str:
                continue
            try:
                pub_dt = email.utils.parsedate_to_datetime(pub_date_str)
                age_seconds = (now_utc - pub_dt).total_seconds()
                # Accept articles within the 24h envelope
                if 0 <= age_seconds <= 86400:
                    pub_ts = pub_dt.timestamp()
                    hours_ago = max(1, int(age_seconds // 3600))
                    clean_title = html.unescape(title).replace("\u20b9", "Rs.").replace("₹", "Rs.")
                    if " - " in clean_title:
                        clean_title = clean_title.rsplit(" - ", 1)[0]

                    # Strictly enforce company and symbol relevance (filters out sister companies like Jio for Reliance)
                    if not is_article_strictly_relevant(upper, company_name, clean_title):
                        continue

                    is_reg = any(k in clean_title.lower() for k in reg_keywords)
                    articles_raw.append({
                        "title": clean_title,
                        "source": source or "News",
                        "hours_ago": hours_ago,
                        "pub_ts": pub_ts,
                        "is_regulatory": is_reg,
                    })
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"News fetch error for {symbol} ({window_str}): {e}")

    return articles_raw


def _fetch_stock_news_24h_sync(
    symbol: str,
    force_refresh: bool = False,
    cache_instance: Optional[HistoricalCandleCache] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    now_ts = time.time()
    now_ist = datetime.now(_IST)
    now_utc = datetime.now(timezone.utc)
    upper = symbol.strip().upper()

    # 1. Check in-memory cache and SQLite cache
    cached_entry = _NEWS_CACHE.get(upper)
    if cached_entry is None and cache_instance is not None:
        db_entry = cache_instance.get_news_cache(upper)
        if db_entry:
            cached_entry = db_entry
            _NEWS_CACHE[upper] = db_entry

    company_name = get_company_name_for_symbol(upper)

    if not force_refresh and cached_entry is not None:
        cached_ts, cached_news, cached_articles = cached_entry
        if (now_ts - cached_ts) < _NEWS_CACHE_TTL:
            clean_articles = [a for a in cached_articles if is_article_strictly_relevant(upper, company_name, a.get("title", ""))]
            return cached_news, clean_articles

    is_trading_time = _is_trading_hours_ist(now_ist)

    # ── BRANCH A: Outside Trading Hours (Weekends, before 09:00, or after 16:00 IST) ──
    # Outside trading time: pull fresh 24h news and FinBERT re-evaluates all stocks.
    if not is_trading_time:
        articles_raw = _fetch_stock_news_from_google(upper, "24h")
        # Prune strictly > 24 hours (86400s)
        surviving = [
            a for a in articles_raw
            if (now_utc.timestamp() - a.get("pub_ts", now_ts)) <= 86400
        ]
        # Sort newest first
        surviving.sort(key=lambda a: a.get("pub_ts", 0), reverse=True)
        for a in surviving:
            age_sec = max(0.0, now_utc.timestamp() - a.get("pub_ts", now_ts))
            a["hours_ago"] = max(1, int(age_sec // 3600))

        articles_display = [
            f"[{a.get('source')}] {a.get('title')} ({a.get('hours_ago')}h ago)" if a.get('source') else f"{a.get('title')} ({a.get('hours_ago')}h ago)"
            for a in surviving
        ]
        summary_str = " | ".join(articles_display[:2]) if articles_display else "No news in past 24h"

        _NEWS_CACHE[upper] = (now_ts, summary_str, surviving)
        if cache_instance:
            cache_instance.save_news_cache(upper, now_ts, summary_str, surviving)
        return summary_str, surviving

    # ── BRANCH B: Inside Trading Hours (09:00 - 16:00 IST, Mon-Fri) ─────────────
    # Inside trading time: see when last news was pulled (e.g. 1hr, 2hr, 3hr ago),
    # pull within that period, append with prev news already there, and remove news older than 24h.
    prev_articles: List[Dict[str, Any]] = []
    last_pulled_ts = 0.0
    if cached_entry:
        last_pulled_ts, _, prev_articles = cached_entry

    elapsed_sec = max(0.0, now_ts - last_pulled_ts) if last_pulled_ts > 0 else 86400.0

    # If first time pulling or last pull was > 24 hours ago, pull full 24h
    if last_pulled_ts <= 0 or elapsed_sec >= 86400.0 or not prev_articles:
        window_str = "24h"
    else:
        # Determine elapsed hours rounded up, bounded between 1h and 24h
        elapsed_hours = max(1, min(24, math.ceil(round(elapsed_sec / 3600.0, 2))))
        window_str = f"{elapsed_hours}h"

    new_articles = _fetch_stock_news_from_google(upper, window_str)

    # Merge newly pulled articles with previous articles, deduplicating by normalized title
    merged: Dict[str, Dict[str, Any]] = {}

    # 1. Existing articles (prune anything older than 24h)
    for art in prev_articles:
        art_ts = art.get("pub_ts")
        if art_ts is None:
            art_ts = now_ts - (art.get("hours_ago", 1) * 3600.0)
            art["pub_ts"] = art_ts
        if (now_utc.timestamp() - art_ts) <= 86400:
            k = _normalize_title_for_dedup(art.get("title", ""))
            if k:
                merged[k] = art

    # 2. Append new articles
    for art in new_articles:
        art_ts = art.get("pub_ts", now_ts)
        if (now_utc.timestamp() - art_ts) <= 86400:
            k = _normalize_title_for_dedup(art.get("title", ""))
            if k:
                merged[k] = art

    surviving = list(merged.values())
    surviving.sort(key=lambda a: a.get("pub_ts", 0), reverse=True)
    for a in surviving:
        age_sec = max(0.0, now_utc.timestamp() - a.get("pub_ts", now_ts))
        a["hours_ago"] = max(1, int(age_sec // 3600))

    articles_display = [
        f"[{a.get('source')}] {a.get('title')} ({a.get('hours_ago')}h ago)" if a.get('source') else f"{a.get('title')} ({a.get('hours_ago')}h ago)"
        for a in surviving
    ]
    summary_str = " | ".join(articles_display[:2]) if articles_display else "No news in past 24h"

    _NEWS_CACHE[upper] = (now_ts, summary_str, surviving)
    if cache_instance:
        cache_instance.save_news_cache(upper, now_ts, summary_str, surviving)
    return summary_str, surviving


async def fetch_stock_news_24h(
    symbol: str,
    force_refresh: bool = False,
    cache_instance: Optional[HistoricalCandleCache] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Fetches stock news with adaptive trading-hours lookback, dedup, and 24h pruning."""
    return await asyncio.to_thread(_fetch_stock_news_24h_sync, symbol, force_refresh, cache_instance)


def synthesize_institutional_flow(
    symbol: str,
    daily_bars: List[CandleBar],
    current_avwap: Optional[float] = None,
    avwap_week: Optional[float] = None,
    ema50: Optional[float] = None,
    support: Optional[float] = None,
    resistance: Optional[float] = None,
    weekly_trend: str = "NEUTRAL",
    technical_bias: Optional[str] = None,
) -> InstitutionalFlowResult:
    """Invokes the Universal Institutional Flow Engine."""
    engine = get_institutional_flow_engine()

    current_bar = daily_bars[-1]
    closes_20 = [b.close for b in daily_bars[-20:]]
    opens_20 = [b.open for b in daily_bars[-20:]]
    highs_20 = [b.high for b in daily_bars[-20:]]
    lows_20 = [b.low for b in daily_bars[-20:]]
    vols_20 = [b.volume for b in daily_bars[-20:]]

    adv_20 = statistics.mean(vols_20) if vols_20 else current_bar.volume
    med_20 = statistics.median(vols_20) if vols_20 else current_bar.volume

    # Optional live CVD from cvd_engine if active
    live_cvd = None
    live_obi = None
    live_deep_obi = None
    live_is_absorbing = None
    if get_instrument_resolver is not None and get_cvd_engine is not None:
        try:
            resolver = get_instrument_resolver()
            sec_id = resolver.get_security_id(symbol)
            if sec_id:
                cvd_eng = get_cvd_engine()
                live_cvd = cvd_eng.get_cvd(sec_id)
                live_obi = cvd_eng.get_obi(sec_id)
                live_deep_obi = cvd_eng.get_deep_obi(sec_id)
                live_is_absorbing = cvd_eng.is_absorbing(sec_id)
        except Exception:
            pass

    bulk_net_cr = None
    if get_bulk_deals_service is not None:
        try:
            bulk_svc = get_bulk_deals_service()
            deal_summary = bulk_svc.get_symbol_deal_summary(symbol, lookback_days=7)
            if deal_summary.get("has_deals"):
                bulk_net_cr = deal_summary.get("net_cash_cr")
        except Exception:
            pass

    snapshot = MarketSnapshot(
        symbol=symbol,
        price=current_bar.close,
        volume=current_bar.volume,
        adv_20=adv_20,
        volume_median_20=med_20,
        volumes_20=vols_20,
        daily_closes_20=closes_20,
        daily_highs_20=highs_20,
        daily_lows_20=lows_20,
        daily_opens_20=opens_20,
        live_cvd=live_cvd if live_cvd != 0 else None,
        live_obi=live_obi,
        live_deep_obi=live_deep_obi,
        live_is_absorbing=live_is_absorbing,
        avwap_month=current_avwap,
        avwap_week=avwap_week,
        ema_50=ema50,
        support_level=support,
        resistance_level=resistance,
        weekly_trend=weekly_trend,
        technical_bias=technical_bias,
        bulk_deals_net_cr=bulk_net_cr,
    )

    return engine.compute_flow(snapshot)


# ── Structured Commentary & Scoring Engine ───────────────────────────────────

def synthesize_commentary_and_score(
    symbol: str,
    current_bar: CandleBar,
    consolidation: ConsolidationPattern,
    ma_status: Dict[str, Any],
    squeeze_pct: Optional[float],
    atr: Optional[float],
    ema50: Optional[float],
    sma50: Optional[float],
    sma200: Optional[float],
    avwap: Optional[float],
    weekly_trend: str,
    latest_news: str = "No news in past 24h",
    flow_result: Optional[InstitutionalFlowResult] = None,
    adv_20: Optional[float] = None,
    sentiment_detail: Optional[Dict[str, Any]] = None,
    narrative_alignment: Optional[Dict[str, Any]] = None,
    candle_pattern: Optional[str] = None,
) -> TechnicalCommentary:
    """Synthesize institutional-grade rule-based technical commentary and potential score."""
    p = current_bar.close
    res = consolidation.resistance
    sup = consolidation.support
    span = consolidation.range_span_pct
    duration = consolidation.duration_bars

    # Dynamic volatility unit: Wilder's ATR (defaulting to 1.5% of price if ATR is uncomputed)
    atr_val = atr if (atr and atr > 0) else max(p * 0.015, QUANT_CONFIG["doji_abs_span_floor"])
    breakdown_buffer = QUANT_CONFIG.get("breakdown_atr_mult", 0.20) * atr_val
    breakout_buffer = QUANT_CONFIG.get("breakout_atr_mult", 0.20) * atr_val
    proximity_limit = QUANT_CONFIG.get("proximity_atr_mult", 0.50) * atr_val

    # 1. Early Degenerate S/R Short-Circuit BEFORE any scoring or bonus computation
    has_valid_sr = (sup is not None and sup > 0) and (res is not None and res > 0)
    # Check if S/R is structurally degenerate (e.g. range is less than 0.1% of price)
    is_degenerate = has_valid_sr and (res - sup) / p < 0.001

    if is_degenerate:
        logger.warning(
            "Degenerate S/R detected for %s: support (%.2f) and resistance (%.2f) are too close.",
            symbol, sup, res
        )
        return TechnicalCommentary(
            structure_base=f"{symbol} exhibits an invalid structural configuration: calculated support (₹{sup:.2f}) meets or exceeds resistance (₹{res:.2f}).",
            multi_tf_trend=f"Primary Weekly Trend is {weekly_trend}.",
            indicator_interaction="Technical scoring suspended due to corrupted or degenerate S/R levels.",
            volatility_state="Volatility metrics ungrounded.",
            actionable_thesis="Bias: INVALID_STRUCTURE. Avoid/Pass — Inconclusive structure. Await clean support/resistance resolution.",
            bias="INVALID_STRUCTURE",
            potential_score=QUANT_CONFIG["degenerate_sr_sentinel_score"],
            quality_tier="C",
            trigger_price=None,
            target_price_1=None,
            target_price_2=None,
            stop_loss=None,
            latest_news=latest_news,
            institutional_flow="Flow vetoes suspended due to invalid structural levels.",
            institutional_flow_detail=flow_result.to_dict() if flow_result else None,
        )

    # Calculate potential score (0 to 100)
    score = 50

    # Consolidation tightness factor + Squeeze factor with Collinearity Cap
    base_bonus = 0
    if consolidation.is_consolidating:
        if span <= 5.0:
            base_bonus = 25
        elif span <= 8.5:
            base_bonus = 18
        else:
            base_bonus = 10

    squeeze_bonus = 0
    if squeeze_pct is not None and squeeze_pct <= 20.0:
        squeeze_bonus = 15

    # Cap combined compression bonus (tightness + squeeze) to prevent collinearity double-counting
    compression_bonus = min(QUANT_CONFIG.get("max_compression_bonus", 30), base_bonus + squeeze_bonus)
    score += compression_bonus

    # Trend alignment factor
    if weekly_trend == "BULLISH":
        score += 10
    if sma200 and p > sma200:
        score += 10

    # Moving Average & VWAP factor
    if ma_status["bouncing_ema50"] or ma_status["bouncing_avwap"]:
        score += 15
    elif ma_status["touching_ema50"] or ma_status["touching_avwap"]:
        score += 8

    # Dynamic Volatility-Normalized Proximity to breakout level (k * ATR)
    dist_to_res_pts = res - p if res > 0 else 999.0
    if 0.0 <= dist_to_res_pts <= proximity_limit:
        score += 12

    # Institutional Flow Confluence Boost / Penalty
    if flow_result is not None:
        if flow_result.score >= 50:
            score += 10  # Strong institutional accumulation confirmation
        elif flow_result.score <= -40:
            score -= 15  # Institutional distribution overhang
        if flow_result.stealth_phase == "STEALTH_ACCUMULATION":
            score += 8   # Early positioning catalyst

    # Dynamic ATR Volatility-Normalized Structural Break Bands
    is_breakdown = (sup is not None and sup > 0) and (p < (sup - breakdown_buffer))
    is_reclaim = (res is not None and res > 0) and (p > (res + breakout_buffer))
    has_distribution = flow_result is not None and flow_result.score <= -40
    has_accumulation = flow_result is not None and flow_result.score >= 50

    # Setup Bias Determination (Baseline)
    if ma_status["bouncing_ema50"] and weekly_trend == "BULLISH":
        bias = "50EMA_BOUNCE"
    elif ma_status["bouncing_avwap"]:
        bias = "MONTHLY_VWAP_TEST"
    elif is_breakdown:
        bias = "BEARISH_BREAKDOWN"
    elif is_reclaim:
        bias = "BULLISH_BREAKOUT"
    elif consolidation.is_consolidating and (0.0 <= dist_to_res_pts <= proximity_limit) and (sup is None or p >= (sup - breakdown_buffer)):
        bias = "BULLISH_BREAKOUT"
    elif squeeze_pct is not None and squeeze_pct <= 15.0 and consolidation.is_consolidating:
        bias = "SQUEEZE_COIL"
    else:
        bias = "NEUTRAL_CHOP"

    # Active Catalyst Priority Boost: reward active breakout momentum without double-counting MA bounce factors
    if bias == "BULLISH_BREAKOUT" or (flow_result and flow_result.stealth_phase == "STEALTH_MARKUP"):
        score += QUANT_CONFIG.get("active_catalyst_bonus", 10)

    # Candle Pattern Confluence: Reward hammer at MA/AVWAP support, temper conviction on shooting star at reclaim
    cp = candle_pattern or detect_candle_pattern(current_bar)
    if cp == "Bullish Hammer Rejection" and (ma_status.get("bouncing_ema50") or ma_status.get("touching_ema50") or ma_status.get("bouncing_avwap") or ma_status.get("touching_avwap")):
        score += 5  # Hammer at key support = high-quality bounce confirmation
    elif cp == "Bearish Shooting Star" and is_reclaim:
        score -= 3  # Rejection at resistance = temper breakout conviction

    # ── Explicit 8-State Specific-Before-General Conflict & Confluence Matrix ──
    # CASE 1: Conflict — Breakdown under Accumulation (Stop-Hunt / Dip-Buying)
    # Specific-before-general ordering: preserves conservative breakdown cap <= 35
    if is_breakdown and has_accumulation:
        score = min(score, QUANT_CONFIG["veto_breakdown_cap"])  # <= 35
        bias = "CONFLICTED_SIGNAL"

    # CASE 2: Conflict — Breakout under Distribution (Bull Trap)
    elif is_reclaim and has_distribution:
        score = min(score, QUANT_CONFIG["veto_distribution_cap"])  # <= 40
        bias = "DISTRIBUTION_TRAP"

    # CASE 3: Concordant Bearish — Breakdown confirmed by Distribution
    elif is_breakdown and has_distribution:
        score = min(score, QUANT_CONFIG["concordant_bearish_cap"])  # <= 20
        bias = "BEARISH_BREAKDOWN"

    # CASE 4: Concordant Bullish — Reclaim confirmed by Accumulation
    elif is_reclaim and has_accumulation:
        score = max(score, QUANT_CONFIG["concordant_bullish_floor"])  # >= 75
        bias = "BULLISH_BREAKOUT"

    # CASE 5: Isolated Structural Breakdown (Flow Neutral)
    elif is_breakdown:
        score = min(score, QUANT_CONFIG["veto_breakdown_cap"])  # <= 35
        bias = "BEARISH_BREAKDOWN"

    # CASE 6: Isolated Structural Reclaim (Flow Neutral)
    elif is_reclaim:
        score = max(score, QUANT_CONFIG["veto_reclaim_floor"])  # >= 65
        if bias in ("BEARISH_BREAKDOWN", "NEUTRAL_CHOP"):
            bias = "BULLISH_BREAKOUT"

    # CASE 7: Isolated Distribution (Inside Range)
    elif has_distribution:
        score = min(score, QUANT_CONFIG["veto_distribution_cap"])  # <= 40
        if bias == "BULLISH_BREAKOUT":
            bias = "DISTRIBUTION_TRAP"

    # CASE 8: Isolated Accumulation (Inside Range)
    elif has_accumulation:
        score = max(score, QUANT_CONFIG["veto_accumulation_floor"])  # >= 60

    # FinBERT Sentiment & Cross-Modal Narrative Confluence
    if sentiment_detail and sentiment_detail.get("sentiment_score") is not None:
        sent_score = sentiment_detail["sentiment_score"]
        if sent_score >= 40:
            score += 8  # Strong bullish catalyst confirmation
        elif sent_score <= -40:
            score -= 8  # Negative headline drag

        if narrative_alignment:
            state = narrative_alignment.get("state", "")
            if state == "CONCORDANT_BULLISH":
                score += 5  # News + Flow aligned bullish
            elif state == "MEDIA_HYPE_TRAP":
                score -= 5  # Distribution masked by hype
            elif state == "SILENT_ACCUMULATION":
                score += 5  # Smart money buying on quiet tape

    # FLAW-03 Fix: If stock is in structural breakdown or distribution, preserve the exact
    # structural veto caps (including concordant bearish cap <= 20) so positive headlines
    # or narrative hype cannot override confirmed bearish market structure.
    if is_breakdown and has_distribution:
        score = min(score, QUANT_CONFIG["concordant_bearish_cap"])
    elif is_breakdown:
        score = min(score, QUANT_CONFIG["veto_breakdown_cap"])
    elif has_distribution:
        score = min(score, QUANT_CONFIG["veto_distribution_cap"])

    # Liquidity & Execution Drag Defense: Deduct penalty if 20-day ADV turnover < ₹5 Crore
    is_illiquid = False
    if adv_20 is not None and adv_20 > 0:
        turnover_inr = adv_20 * p
        if turnover_inr < QUANT_CONFIG.get("min_turnover_liquid_inr", 50000000.0):
            is_illiquid = True
            score -= QUANT_CONFIG.get("illiquid_drag_penalty", 10)

    score = max(10, min(98, score))

    # Tier mapping
    if score >= 85:
        tier = "A+"
    elif score >= 75:
        tier = "A"
    elif score >= 60:
        tier = "B"
    else:
        tier = "C"

    # Hard tier cap for illiquid stocks to prevent spread slippage traps
    if is_illiquid and tier in ("A+", "A"):
        tier = QUANT_CONFIG.get("illiquid_tier_cap", "B")

    # Component 1: Structure & Base
    if consolidation.is_consolidating:
        struct_text = (
            f"{symbol} is coiling within a {duration}-session horizontal consolidation base "
            f"spanning {span}% (₹{sup:.2f} – ₹{res:.2f}). Containment is solid at "
            f"{int(consolidation.containment_ratio * 100)}%."
        )
    else:
        struct_text = (
            f"{symbol} is trading in a broader {span}% swing channel between "
            f"recent support at ₹{sup:.2f} and resistance at ₹{res:.2f}."
        )

    # Component 2: Multi-Timeframe Trend
    mtf_text = f"Primary Weekly Trend is {weekly_trend}."
    if sma200:
        if p > sma200:
            mtf_text += f" Price holds cleanly above the long-term 200-day SMA (₹{sma200:.2f}), confirming macro bullish market structure."
        else:
            mtf_text += f" Price is positioned below the 200-day SMA (₹{sma200:.2f}), indicating longer-term headwinds."

    # Component 3: Indicator Interaction
    ma_parts = []
    if ma_status["confluence_shelf"]:
        ma_parts.append(
            f"A prominent Institutional Confluence Shelf is established around ₹{ma_status['confluence_level']:.2f} "
            f"(50-EMA & Monthly AVWAP aligned within 0.75%)."
        )
    if ma_status["bouncing_ema50"]:
        ma_parts.append(f"Recent price action printed a decisive hammer bounce directly off the rising 50-day EMA (₹{ema50:.2f}).")
    elif ma_status["touching_ema50"]:
        ma_parts.append(f"Price is testing the 50-day EMA (₹{ema50:.2f}) within a tight {abs(ma_status['dist_ema50_pct']):.1f}% corridor.")
    elif ema50:
        ma_parts.append(f"50-day EMA stands at ₹{ema50:.2f} ({ma_status['dist_ema50_pct']:+.1f}% distance).")

    if ma_status["bouncing_avwap"]:
        ma_parts.append(f"Strong bid absorption observed at the Monthly Anchored VWAP (₹{avwap:.2f}).")

    indicator_text = " ".join(ma_parts) if ma_parts else "Moving averages and anchored VWAP are in neutral alignment."

    # Component 4: Volatility Squeeze State
    if squeeze_pct is not None:
        if squeeze_pct <= 15.0:
            vol_text = f"Bollinger BandWidth is in the {squeeze_pct:.0f}th percentile—an extreme multi-month volatility compression signaling an imminent directional expansion."
        elif squeeze_pct <= 35.0:
            vol_text = f"Volatility is contracting (Squeeze Percentile: {squeeze_pct:.0f}%), consistent with an orderly accumulation phase."
        else:
            vol_text = f"Volatility is in normal ranges (Squeeze Percentile: {squeeze_pct:.0f}%)."
    else:
        vol_text = "Volatility metrics are stabilizing within standard historical bands."

    # Component 5: Actionable Trade Thesis
    atr_val = atr if atr and atr > 0 else (p * 0.015)
    if bias in ("BULLISH_BREAKOUT", "SQUEEZE_COIL"):
        trigger = round(res * 1.002, 2)
        target1 = round(trigger + (atr_val * 2.0), 2)
        target2 = round(trigger + (atr_val * 3.8), 2)
        stop = round(sup * 0.995, 2)
        thesis_text = (
            f"Bias: Bullish Breakout Candidate. Look for a daily close above ₹{trigger:.2f} "
            f"with expanding volume to trigger stage-1 expansion. Initial objective at ₹{target1:.2f}, "
            f"secondary target at ₹{target2:.2f}. Invalidation stop placed below the base floor at ₹{stop:.2f}."
        )
    elif bias == "50EMA_BOUNCE":
        trigger = round(current_bar.high * 1.001, 2)
        target1 = round(res, 2)
        target2 = round(res + (atr_val * 2.0), 2)
        stop = round(current_bar.low * 0.996, 2)
        thesis_text = (
            f"Bias: 50-EMA Trend Continuation Bounce. Validated entry on break above ₹{trigger:.2f}. "
            f"Initial target retests resistance at ₹{target1:.2f}, with extension to ₹{target2:.2f}. "
            f"Protective stop anchored under the reaction low at ₹{stop:.2f}."
        )
    elif bias == "MONTHLY_VWAP_TEST":
        trigger = round(current_bar.high * 1.001, 2)
        target1 = round(res, 2)
        target2 = round(res + (atr_val * 1.5), 2)
        stop = round((avwap or p) * 0.992, 2)
        thesis_text = (
            f"Bias: Monthly VWAP Support Rebound. Price respecting institutional benchmark at ₹{avwap:.2f}. "
            f"Targeting recovery to ₹{target1:.2f}. Stop-loss positioned below monthly VWAP at ₹{stop:.2f}."
        )
    elif bias == "BEARISH_BREAKDOWN":
        trigger = round(sup * 0.998, 2)
        target1 = round(sup - (atr_val * 2.0), 2)
        target2 = round(sup - (atr_val * 3.5), 2)
        stop = round(res * 1.005, 2)
        thesis_text = (
            f"Bias: Breakdown Vulnerability. Price violating consolidation support at ₹{sup:.2f}. "
            f"Downside acceleration targets ₹{target1:.2f}. Reclaim above ₹{stop:.2f} invalidates bearish thesis."
        )
    elif bias == "CONFLICTED_SIGNAL":
        trigger = round(sup * 1.005, 2) if sup else round(p * 1.01, 2)
        target1 = round(res, 2) if res else round(p * 1.03, 2)
        target2 = round(target1 + (atr_val * 1.5), 2)
        stop = round(p * 0.98, 2)
        thesis_text = (
            f"Bias: Conflicted Signal (Accumulation Divergence / Stop-Hunt). Price trades below support at ₹{sup:.2f}, "
            f"yet institutional flow reflects aggressive accumulation. Await confirmed reclaim above ₹{trigger:.2f} "
            f"before initiating longs; maintain tight risk."
        )
    elif bias == "DISTRIBUTION_TRAP":
        trigger = round(res * 0.995, 2) if res else round(p * 0.99, 2)
        target1 = round(sup, 2) if sup else round(p * 0.97, 2)
        target2 = round(target1 - (atr_val * 2.0), 2)
        stop = round(res * 1.015, 2) if res else round(p * 1.02, 2)
        thesis_text = (
            f"Bias: Distribution Trap Overhang. Price attempts breakout above resistance at ₹{res:.2f}, "
            f"but institutional flow shows strong distribution. High risk of bull-trap exhaustion."
        )
    else:
        trigger = round(res, 2)
        target1 = round(res + (atr_val * 1.5), 2)
        target2 = round(res + (atr_val * 3.0), 2)
        stop = round(sup, 2)
        thesis_text = (
            f"Bias: Neutral Range Bound. Wait for decisive breakout outside the ₹{sup:.2f} – ₹{res:.2f} "
            f"containment box before committing capital."
        )

    return TechnicalCommentary(
        structure_base=struct_text,
        multi_tf_trend=mtf_text,
        indicator_interaction=indicator_text,
        volatility_state=vol_text,
        actionable_thesis=thesis_text,
        bias=bias,
        potential_score=score,
        quality_tier=tier,
        trigger_price=trigger,
        target_price_1=target1,
        target_price_2=target2,
        stop_loss=stop,
        latest_news=latest_news,
        institutional_flow=flow_result.summary if flow_result else "Neutral / Balanced Flow",
        institutional_flow_detail=flow_result.to_dict() if flow_result else None,
        sentiment_detail=sentiment_detail,
        narrative_alignment=narrative_alignment,
    )


# ── Main Service Controller ──────────────────────────────────────────────────

class MultiTFAnalysisService:
    """Core orchestrator for fetching, calculating, and presenting multi-timeframe analysis."""

    def __init__(self, cache: Optional[HistoricalCandleCache] = None):
        self.cache = cache or HistoricalCandleCache()
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def _fetch_today_intraday_bar(
        self,
        symbol: str,
        security_id: Optional[str] = None,
        today_date: Optional[date] = None,
    ) -> Optional[CandleBar]:
        """Fetches today's 1-minute intraday bars from Dhan and aggregates into a single daily candle."""
        sec_id = None
        t_date = today_date or datetime.now(_IST).date()
        # 1. High-speed, zero-network live feed check:
        # If live WebSocket quotes are streaming, use today's actual live price, open, high, low, volume immediately.
        try:
            from services.orchestrator import get_orchestrator
            orch = get_orchestrator()
            ticker = getattr(orch, "dhan_ticker", None)
            if ticker and hasattr(ticker, "latest_quotes"):
                q = ticker.latest_quotes.get(symbol)
                if q and float(q.get("ltp", 0)) > 0:
                    ltp = float(q["ltp"])
                    o = float(q.get("open", ltp))
                    h = max(float(q.get("high", ltp)), ltp, o)
                    l = min(float(q.get("low", ltp)), ltp, o) if float(q.get("low", 0)) > 0 else min(ltp, o)
                    c = ltp
                    v = float(q.get("volume", 0))
                    t = int(datetime(t_date.year, t_date.month, t_date.day, 9, 15, tzinfo=_IST).timestamp())
                    return CandleBar(time=t, open=o, high=h, low=l, close=c, volume=v)
        except Exception as exc_live:
            logger.debug("MultiTF: Live quote check failed for %s: %s", symbol, exc_live)

        try:
            from services.dhan_token_service import get_dhan_token
            from services.dhan_instrument_resolver import get_instrument_resolver

            resolver = get_instrument_resolver()
            resolver.load()
            sec_id = security_id or resolver.get_security_id(symbol)
            client_id = os.getenv("DHAN_CLIENT_ID", "")
            access_token = get_dhan_token() or os.getenv("DHAN_ACCESS_TOKEN", "")

            if sec_id and client_id and access_token:
                headers = {
                    "client-id": client_id,
                    "access-token": access_token,
                    "Content-Type": "application/json",
                }
                # BUG-1 FIX: Dhan v2/charts/intraday uses fromDate/toDate as YYYY-MM-DD strings
                payload = {
                    "securityId": str(sec_id),
                    "exchangeSegment": "NSE_EQ",
                    "instrument": "EQUITY",
                    "interval": "1",
                    "fromDate": t_date.isoformat(),
                    "toDate": t_date.isoformat(),
                }
                client = await self._get_http_client()
                resp = None
                for attempt in range(3):
                    resp = await client.post("https://api.dhan.co/v2/charts/intraday", json=payload, headers=headers)
                    if resp.status_code == 429:
                        await asyncio.sleep(0.35 * (attempt + 1))
                        continue
                    break

                if resp and resp.status_code == 200:
                    data = resp.json()
                    opens = data.get("open") or []
                    highs = data.get("high") or []
                    lows = data.get("low") or []
                    closes = data.get("close") or []
                    volumes = data.get("volume") or []

                    if opens and highs and lows and closes:
                        o = float(opens[0])
                        h = max(float(x) for x in highs)
                        l = min(float(x) for x in lows)
                        c = float(closes[-1])
                        v = sum(float(x) for x in volumes)
                        t = int(datetime(t_date.year, t_date.month, t_date.day, 9, 15, tzinfo=_IST).timestamp())
                        return CandleBar(time=t, open=o, high=h, low=l, close=c, volume=v)
                elif resp and resp.status_code == 400:
                    logger.warning(
                        "MultiTF: Dhan intraday 400 for %s on %s: %s",
                        symbol, t_date, resp.text[:300]
                    )
        except Exception as exc:
            logger.debug("MultiTF: Today intraday Dhan API failed for %s: %s", symbol, exc)

        # Fallback to CandleAggregator in-memory 5m/1m bars if available
        try:
            from services.candle_aggregator import get_candle_aggregator
            agg = get_candle_aggregator()
            token = int(sec_id) if sec_id and str(sec_id).isdigit() else 0
            if token > 0:
                bars_5m = list(agg.bars_5m.get(token, ()))
                curr_5m = agg.current_5m.get(token)
                all_bars = bars_5m + ([curr_5m] if curr_5m else [])
                if not all_bars:
                    bars_1m = list(agg.bars_1m.get(token, ()))
                    curr_1m = agg.current_1m.get(token)
                    all_bars = bars_1m + ([curr_1m] if curr_1m else [])
                if all_bars:
                    bar_sym = all_bars[0].get("symbol")
                    if bar_sym and bar_sym != symbol:
                        logger.warning("Rejecting CandleAggregator fallback due to symbol mismatch: expected=%s, got=%s", symbol, bar_sym)
                        return None
                    o = float(all_bars[0].get("open", 0))
                    h = max(float(b.get("high", 0)) for b in all_bars)
                    l = min(float(b.get("low", 9999999)) for b in all_bars)
                    c = float(all_bars[-1].get("close", 0))
                    v = sum(float(b.get("volume", 0)) for b in all_bars)
                    if o > 0 and c > 0:
                        t = int(datetime(t_date.year, t_date.month, t_date.day, 9, 15, tzinfo=_IST).timestamp())
                        return CandleBar(time=t, open=o, high=h, low=l, close=c, volume=v)
        except Exception as exc2:
            logger.debug("MultiTF: CandleAggregator fallback failed for %s: %s", symbol, exc2)
        return None

    async def fetch_or_generate_daily_candles(
        self,
        symbol: str,
        security_id: Optional[str] = None,
        days_back: int = 1500,
        force_refresh: bool = False,
    ) -> List[CandleBar]:
        """
        Retrieves daily candles from local cache.
        Checks for staleness against the latest completed trading session.
        If cache is missing, has < 50 bars, is stale, or force_refresh is True:
        fetches historical bars from Dhan API and updates the cache.
        Then, automatically stitches today's live/intraday session bar so
        analysis and charts ALWAYS reflect today's latest closing or live price.
        """
        from services.dhan_instrument_resolver import resolve_canonical_symbol
        upper = resolve_canonical_symbol(symbol)
        candles = await asyncio.to_thread(self.cache.get_candles, upper)

        now_ist = datetime.now(_IST)
        today = now_ist.date()
        weekday = now_ist.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
        market_started = now_ist.hour > 9 or (now_ist.hour == 9 and now_ist.minute >= 15)
        is_trading_day = weekday < 5

        # Determine the latest fully closed equity trading day
        if weekday == 0:  # Monday
            latest_closed_day = today - timedelta(days=3)  # Friday
        elif weekday == 5:  # Saturday
            latest_closed_day = today - timedelta(days=1)  # Friday
        elif weekday == 6:  # Sunday
            latest_closed_day = today - timedelta(days=2)  # Friday
        else:  # Tue-Fri
            latest_closed_day = today - timedelta(days=1)  # Yesterday

        # Determine the latest cached closed trading day:
        if candles:
            last_date = datetime.fromtimestamp(candles[-1].time, tz=_IST).date()
            if last_date == today and len(candles) >= 2:
                last_closed_cached_date = datetime.fromtimestamp(candles[-2].time, tz=_IST).date()
            elif last_date == today:
                last_closed_cached_date = None
            else:
                last_closed_cached_date = last_date
        else:
            last_closed_cached_date = None

        # A cache is considered stale if:
        # 1. Fewer than 50 bars
        # 2. force_refresh is True
        # 3. The last closed cached candle date is strictly older than the latest closed trading session (e.g. Sept 17 < Sept 18)
        # 4. Market is open today but cache has no today bar AND last_closed_cached_date < latest_closed_day
        #    OR: today is a weekday + market has started + cache is older than today (handles holidays)
        _cache_is_dated = (
            last_closed_cached_date is not None
            and last_closed_cached_date < latest_closed_day
        )
        # Holiday guard: if it's a trading day, market started, but cache hasn't been updated
        # with today's data, treat as stale even if the strict date comparison passes
        # (handles public holidays where yesterday was a holiday and the 'latest_closed_day' calc is wrong)
        _stale_by_age = (
            is_trading_day
            and market_started
            and candles
            and (datetime.now(_IST).date() > datetime.fromtimestamp(candles[-1].time, tz=_IST).date())
            and len(candles) >= 50  # Only apply if we have history, otherwise the < 50 rule handles it
        )
        needs_historical_sync = (
            len(candles) < 50
            or force_refresh
            or _cache_is_dated
            or _stale_by_age
        )

        if needs_historical_sync:
            try:
                from services.dhan_token_service import get_dhan_token
                from services.dhan_instrument_resolver import get_instrument_resolver

                resolver = get_instrument_resolver()
                resolver.load()
                sec_id = security_id or resolver.get_security_id(upper)
                client_id = os.getenv("DHAN_CLIENT_ID", "")
                access_token = get_dhan_token() or os.getenv("DHAN_ACCESS_TOKEN", "")

                if sec_id and client_id and access_token:
                    headers = {
                        "client-id": client_id,
                        "access-token": access_token,
                        "Content-Type": "application/json",
                    }
                    # If we already have >= 50 candles, fetching recent 60 days fills missing sessions quickly
                    lookbacks = (730, 365) if len(candles) < 50 else (60,)
                    # Reuse the shared async HTTP client to avoid spawning a new TCP connection per symbol
                    http_client = await self._get_http_client()
                    for lookback in lookbacks:
                        from_date = today - timedelta(days=lookback)
                        payload = {
                            "securityId": str(sec_id),
                            "exchangeSegment": "NSE_EQ",
                            "instrument": "EQUITY",
                            "expiryCode": 0,
                            "fromDate": from_date.isoformat(),
                            "toDate": today.isoformat(),
                        }
                        resp = await http_client.post("https://api.dhan.co/v2/charts/historical", json=payload, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            opens = data.get("open") or []
                            highs = data.get("high") or []
                            lows = data.get("low") or []
                            closes = data.get("close") or []
                            volumes = data.get("volume") or []
                            timestamps = data.get("timestamp") or []

                            fetched_candles: List[CandleBar] = []
                            for i in range(len(closes)):
                                fetched_candles.append(CandleBar(
                                    time=int(float(timestamps[i])),
                                    open=float(opens[i]),
                                    high=float(highs[i]),
                                    low=float(lows[i]),
                                    close=float(closes[i]),
                                    volume=float(volumes[i]),
                                ))
                            if fetched_candles:
                                fetched_candles.sort(key=lambda c: c.time)
                                if len(candles) < 50:
                                    await asyncio.to_thread(self.cache.save_candles, upper, fetched_candles, overwrite=True)
                                    candles = fetched_candles
                                else:
                                    await asyncio.to_thread(self.cache.save_candles, upper, fetched_candles, overwrite=False)
                                    candles = await asyncio.to_thread(self.cache.get_candles, upper)
                                logger.info(
                                    "MultiTF: Synced %d daily bars from Dhan for %s (latest=%s)",
                                    len(fetched_candles),
                                    upper,
                                    datetime.fromtimestamp(candles[-1].time, tz=_IST).date() if candles else "N/A",
                                )
                                break
                        elif resp.status_code == 400:
                            logger.warning(
                                "MultiTF: Dhan historical 400 for %s lookback=%dd: %s",
                                upper, lookback, resp.text[:300]
                            )
                            break
            except Exception as exc:
                logger.debug("MultiTF: Dhan historical fetch skipped for %s: %s", upper, exc)

        # ── 2. Stitch Today's Intraday Session into Daily Series ─────────────
        # Guarantees the latest closed or forming session (today) is present
        if is_trading_day and market_started and candles:
            last_date = datetime.fromtimestamp(candles[-1].time, tz=_IST).date()
            if last_date < today:
                today_bar = await self._fetch_today_intraday_bar(upper, security_id, today)
                if today_bar:
                    candles.append(today_bar)
                    await asyncio.to_thread(self.cache.save_candles, upper, [today_bar])
                    logger.info("MultiTF: Stitched today's session candle for %s: Close=₹%.2f Vol=%d", upper, today_bar.close, int(today_bar.volume))
            elif last_date == today and (now_ist.hour < 16 or (now_ist.hour == 16 and now_ist.minute <= 30)):
                # Market is open or in post-market wrap: update forming candle
                today_bar = await self._fetch_today_intraday_bar(upper, security_id, today)
                if today_bar:
                    candles[-1] = today_bar
                    await asyncio.to_thread(self.cache.save_candles, upper, [today_bar])

        return candles

    async def analyze_symbol(self, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        """Performs full multi-timeframe analysis for a single stock."""
        from services.dhan_instrument_resolver import resolve_canonical_symbol
        upper = resolve_canonical_symbol(symbol)
        daily_bars = await self.fetch_or_generate_daily_candles(upper, force_refresh=force_refresh)
        if not daily_bars:
            raise ValueError(f"No candle data available for {upper}")

        # Resample to Weekly and Monthly
        weekly_bars = resample_weekly(daily_bars)
        monthly_bars = resample_monthly(daily_bars)

        # ── 0. Guard against Insufficient History ─────────────────────────────
        # Multi-timeframe analysis requires establishing a prior consolidation base
        # from closed historical sessions. If <= 15 sessions are present, return an
        # explicit insufficient history sentinel rather than computing a self-referential base.
        if len(daily_bars) <= 15:
            current_bar = daily_bars[-1]
            return {
                "symbol": upper,
                "current_price": round(current_bar.close, 2),
                "price": round(current_bar.close, 2),
                "prev_close": round(daily_bars[-2].close, 2) if len(daily_bars) >= 2 else round(current_bar.open, 2),
                "change_pct": round(((current_bar.close - daily_bars[-2].close) / daily_bars[-2].close) * 100.0, 2) if len(daily_bars) >= 2 else 0.0,
                "timeframes": {
                    "1D": {"candles": [b.to_dict() for b in daily_bars], "ema50": [], "sma50": [], "sma200": [], "avwap_month": [], "avwap_week": []},
                    "1W": {"candles": [b.to_dict() for b in weekly_bars], "ema20": [], "ema50": [], "sma50": [], "sma100": [], "avwap": []},
                    "1M": {"candles": [b.to_dict() for b in monthly_bars], "ema10": [], "ema20": [], "sma20": [], "avwap": []},
                },
                "consolidation": asdict(ConsolidationPattern(False, 0.0, 0.0, 0.0, 0, 0, 0, 0.0)),
                "ma_status": {
                    "touching_ema50": False, "bouncing_ema50": False, "dist_ema50_pct": 0.0,
                    "touching_avwap": False, "bouncing_avwap": False, "dist_avwap_pct": 0.0,
                    "confluence_shelf": False, "confluence_level": None,
                },
                "metrics": {
                    "ema50": None, "sma50": None, "sma200": None, "avwap_month": None,
                    "squeeze_percentile": None, "atr14": None, "weekly_trend": "NEUTRAL",
                },
                "commentary": TechnicalCommentary(
                    structure_base=f"{upper} has only {len(daily_bars)} sessions of trading history. Minimum 16 sessions required to establish prior support/resistance base.",
                    multi_tf_trend="Primary trend undetermined (insufficient history).",
                    indicator_interaction="Technical scoring suspended due to insufficient session history.",
                    volatility_state="Volatility metrics ungrounded.",
                    actionable_thesis="Bias: INSUFFICIENT_HISTORY. Newly listed / insufficient data (<=15 bars). Await trading history accumulation before evaluating structural setups.",
                    bias="INSUFFICIENT_HISTORY",
                    potential_score=QUANT_CONFIG.get("degenerate_sr_sentinel_score", 20),
                    quality_tier="C",
                    trigger_price=None,
                    target_price_1=None,
                    target_price_2=None,
                    stop_loss=None,
                    latest_news="No news in past 24h",
                    institutional_flow="Institutional flow scoring suspended (insufficient history).",
                    institutional_flow_detail=None,
                ).to_dict(),
                "markers": [],
                "price_lines": [],
                "config_version": QUANT_CONFIG.get("config_version", "v1.0.0"),
                "config_hash": QUANT_CONFIG_HASH,
                "analyzed_at": datetime.now(_IST).isoformat(),
            }

        # ── 1. Calculate Daily Indicators ─────────────────────────────────────
        daily_closes = [b.close for b in daily_bars]
        d_ema50 = compute_ema(daily_closes, 50)
        d_sma50 = compute_sma(daily_closes, 50)
        d_sma200 = compute_sma(daily_closes, 200)
        d_avwap_month = compute_anchored_vwap(daily_bars, "monthly")
        d_avwap_week = compute_anchored_vwap(daily_bars, "weekly")
        d_squeeze = compute_bollinger_squeeze(daily_bars, 20, 120)
        d_atr = compute_atr(daily_bars, 14)

        # ── 2. Calculate Weekly Indicators ────────────────────────────────────
        weekly_closes = [b.close for b in weekly_bars]
        w_ema20 = compute_ema(weekly_closes, 20)
        w_ema50 = compute_ema(weekly_closes, 50)
        w_sma50 = compute_sma(weekly_closes, 50)
        w_sma100 = compute_sma(weekly_closes, 100)
        w_avwap = compute_anchored_vwap(weekly_bars, "yearly")
        if weekly_bars and w_ema20 and w_ema20[-1] is not None and w_ema50 and w_ema50[-1] is not None:
            w_close = weekly_bars[-1].close
            above_ema20 = w_close >= w_ema20[-1]
            above_ema50 = w_close >= w_ema50[-1]
            ema20_rising = len(w_ema20) >= 2 and w_ema20[-2] is not None and w_ema20[-1] > w_ema20[-2]

            if above_ema20 and above_ema50 and ema20_rising:
                w_trend = "BULLISH"
            elif not above_ema20 and not above_ema50:
                w_trend = "BEARISH"
            else:
                w_trend = "NEUTRAL"
        elif weekly_bars and w_ema20 and w_ema20[-1] is not None:
            w_trend = "BULLISH" if weekly_bars[-1].close >= w_ema20[-1] else "BEARISH"
        else:
            w_trend = "NEUTRAL"

        # ── 2B. Calculate Monthly Indicators ──────────────────────────────────
        monthly_closes = [b.close for b in monthly_bars]
        m_ema10 = compute_ema(monthly_closes, 10)
        m_ema20 = compute_ema(monthly_closes, 20)
        m_sma20 = compute_sma(monthly_closes, 20)
        m_avwap = compute_anchored_vwap(monthly_bars, "multi_year")

        # ── 3. Pattern Recognition (Evaluated on Prior Established Base) ──────
        # Base is established strictly from prior, already-closed bars (daily_bars[:-1])
        # so today's candle is evaluated against the established base without circular reference.
        prior_bars = daily_bars[:-1]
        consolidation = detect_consolidation(prior_bars)
        current_bar = daily_bars[-1]
        current_ema50 = d_ema50[-1]
        current_sma50 = d_sma50[-1]
        current_sma200 = d_sma200[-1]
        current_avwap = d_avwap_month[-1]
        current_squeeze = d_squeeze[-1]
        current_atr = d_atr[-1]

        ma_status = evaluate_ma_interaction(
            current_price=current_bar.close,
            current_low=current_bar.low,
            current_high=current_bar.high,
            current_open=current_bar.open,
            ema50=current_ema50,
            sma50=current_sma50,
            avwap=current_avwap,
            atr=current_atr,
        )

        candle_pattern = detect_candle_pattern(current_bar)

        # ── 3b. Fetch 24h Stock News & Institutional Flow ────────────────────
        latest_news, articles_list = await fetch_stock_news_24h(
            upper,
            force_refresh=force_refresh,
            cache_instance=self.cache,
        )
        flow_result = synthesize_institutional_flow(
            symbol=upper,
            daily_bars=daily_bars,
            current_avwap=current_avwap,
            avwap_week=d_avwap_week[-1] if d_avwap_week else None,
            ema50=current_ema50,
            support=consolidation.support,
            resistance=consolidation.resistance,
            weekly_trend=w_trend,
        )

        # ── 3c. FinBERT GPU Sentiment & Narrative Intelligence ───────────────
        sentiment_detail = None
        narrative_alignment = None
        try:
            from services.finbert_sentiment import get_finbert_sentiment_engine, _SENTIMENT_CACHE, _CACHE_LOCK
            finbert_eng = get_finbert_sentiment_engine()
            if force_refresh:
                with _CACHE_LOCK:
                    _SENTIMENT_CACHE.pop(upper, None)
            sentiment_detail = await asyncio.to_thread(
                finbert_eng.analyze_articles,
                symbol=upper,
                articles=articles_list,
                institutional_flow_score=flow_result.score if flow_result else None,
            )
            narrative_alignment = sentiment_detail.get("narrative_alignment")
        except Exception as sent_err:
            logger.warning("FinBERT sentiment evaluation failed for %s: %s", upper, sent_err)

        adv_20 = (
            sum(b.volume for b in daily_bars[-20:]) / 20.0
            if len(daily_bars) >= 20
            else sum(b.volume for b in daily_bars) / len(daily_bars)
        )

        # ── 4. Synthesize Commentary & Scoring ────────────────────────────────
        commentary = synthesize_commentary_and_score(
            symbol=upper,
            current_bar=current_bar,
            consolidation=consolidation,
            ma_status=ma_status,
            squeeze_pct=current_squeeze,
            atr=current_atr,
            ema50=current_ema50,
            sma50=current_sma50,
            sma200=current_sma200,
            avwap=current_avwap,
            weekly_trend=w_trend,
            latest_news=latest_news,
            flow_result=flow_result,
            adv_20=adv_20,
            sentiment_detail=sentiment_detail,
            narrative_alignment=narrative_alignment,
            candle_pattern=candle_pattern,
        )

        # ── 5. Generate Visual Markers & Price Lines ──────────────────────────
        markers: List[ChartMarker] = []
        price_lines: List[PriceLine] = []

        # Support & Resistance price lines
        if consolidation.resistance > 0:
            price_lines.append(PriceLine(
                price=consolidation.resistance,
                label=f"Resistance ₹{consolidation.resistance:.2f}",
                color="#f59e0b",  # amber
                line_style="dashed",
                line_width=2,
            ))
        if consolidation.support > 0:
            price_lines.append(PriceLine(
                price=consolidation.support,
                label=f"Support ₹{consolidation.support:.2f}",
                color="#06b6d4",  # cyan
                line_style="dashed",
                line_width=2,
            ))

        # Target & Stop Loss lines if actionable setup exists
        if commentary.trigger_price:
            price_lines.append(PriceLine(
                price=commentary.trigger_price,
                label=f"Trigger ₹{commentary.trigger_price:.2f}",
                color="#10b981",  # emerald
                line_style="solid",
                line_width=2,
            ))
        if commentary.target_price_1:
            price_lines.append(PriceLine(
                price=commentary.target_price_1,
                label=f"Target ₹{commentary.target_price_1:.2f}",
                color="#3b82f6",  # blue
                line_style="dotted",
                line_width=1,
            ))
        if commentary.stop_loss:
            price_lines.append(PriceLine(
                price=commentary.stop_loss,
                label=f"Stop Loss ₹{commentary.stop_loss:.2f}",
                color="#ef4444",  # red
                line_style="solid",
                line_width=1,
            ))

        # Directional Arrows & Callout Markers on chart
        if commentary.bias in ("BULLISH_BREAKOUT", "SQUEEZE_COIL"):
            markers.append(ChartMarker(
                time=current_bar.time,
                position="aboveBar",
                color="#10b981",
                shape="arrowUp",
                text="POTENTIAL BREAKOUT ⬆",
            ))
        elif commentary.bias == "50EMA_BOUNCE":
            markers.append(ChartMarker(
                time=current_bar.time,
                position="belowBar",
                color="#06b6d4",
                shape="arrowUp",
                text="50-EMA BOUNCE ⚡",
            ))
        elif commentary.bias == "MONTHLY_VWAP_TEST":
            markers.append(ChartMarker(
                time=current_bar.time,
                position="belowBar",
                color="#8b5cf6",
                shape="arrowUp",
                text="MONTHLY VWAP SUPPORT",
            ))
        elif commentary.bias == "BEARISH_BREAKDOWN":
            markers.append(ChartMarker(
                time=current_bar.time,
                position="belowBar",
                color="#ef4444",
                shape="arrowDown",
                text="BREAKDOWN RISK ⬇",
            ))
        elif commentary.bias == "CONFLICTED_SIGNAL":
            markers.append(ChartMarker(
                time=current_bar.time,
                position="belowBar",
                color="#f59e0b",
                shape="circle",
                text="CONFLICT: ACCUMULATION DIVERGENCE ⚠️",
            ))
        elif commentary.bias == "DISTRIBUTION_TRAP":
            markers.append(ChartMarker(
                time=current_bar.time,
                position="aboveBar",
                color="#f97316",
                shape="arrowDown",
                text="DISTRIBUTION TRAP OVERHANG ⚠️",
            ))

        if candle_pattern:
            markers.append(ChartMarker(
                time=current_bar.time,
                position="aboveBar" if "Hammer" not in candle_pattern else "belowBar",
                color="#fbbf24",
                shape="circle",
                text=candle_pattern,
            ))

        # ── 6. Assemble Final Payload ─────────────────────────────────────────
        # Package series for Lightweight Charts
        daily_series = [b.to_dict() for b in daily_bars[-350:]]
        weekly_series = [b.to_dict() for b in weekly_bars]
        monthly_series = [b.to_dict() for b in monthly_bars]

        def _format_line_series(bars_list: List[CandleBar], values: List[Optional[float]]) -> List[Dict[str, Any]]:
            out = []
            for b, v in zip(bars_list, values):
                if v is not None:
                    out.append({"time": b.time, "value": v})
            return out

        return {
            "symbol": upper,
            "current_price": round(current_bar.close, 2),
            "price": round(current_bar.close, 2),
            "prev_close": round(daily_bars[-2].close, 2) if len(daily_bars) >= 2 else round(current_bar.open, 2),
            "change_pct": round(((current_bar.close - daily_bars[-2].close) / daily_bars[-2].close) * 100.0, 2) if len(daily_bars) >= 2 else 0.0,
            "timeframes": {
                "1D": {
                    "candles": daily_series,
                    "ema50": _format_line_series(daily_bars, d_ema50)[-350:],
                    "sma50": _format_line_series(daily_bars, d_sma50)[-350:],
                    "sma200": _format_line_series(daily_bars, d_sma200)[-350:],
                    "avwap_month": _format_line_series(daily_bars, d_avwap_month)[-350:],
                    "avwap_week": _format_line_series(daily_bars, d_avwap_week)[-350:],
                },
                "1W": {
                    "candles": weekly_series,
                    "ema20": _format_line_series(weekly_bars, w_ema20),
                    "ema50": _format_line_series(weekly_bars, w_ema50),
                    "sma50": _format_line_series(weekly_bars, w_sma50),
                    "sma100": _format_line_series(weekly_bars, w_sma100),
                    "avwap": _format_line_series(weekly_bars, w_avwap),
                },
                "1M": {
                    "candles": monthly_series,
                    "ema10": _format_line_series(monthly_bars, m_ema10),
                    "ema20": _format_line_series(monthly_bars, m_ema20),
                    "sma20": _format_line_series(monthly_bars, m_sma20),
                    "avwap": _format_line_series(monthly_bars, m_avwap),
                },
            },
            "consolidation": asdict(consolidation),
            "ma_status": ma_status,
            "metrics": {
                "ema50": current_ema50,
                "sma50": current_sma50,
                "sma200": current_sma200,
                "avwap_month": current_avwap,
                "squeeze_percentile": current_squeeze,
                "atr14": current_atr,
                "weekly_trend": w_trend,
            },
            "commentary": commentary.to_dict(),
            "markers": [m.to_dict() for m in markers],
            "price_lines": [pl.to_dict() for pl in price_lines],
            "config_version": QUANT_CONFIG.get("config_version", "v1.0.0"),
            "config_hash": QUANT_CONFIG_HASH,
            "analyzed_at": datetime.now(_IST).isoformat(),
        }

    async def get_5m_bars(self, symbol: str, allow_network: bool = True) -> List[CandleBar]:
        from services.dhan_instrument_resolver import get_instrument_resolver, resolve_canonical_symbol
        from services.candle_aggregator import get_candle_aggregator

        upper = resolve_canonical_symbol(symbol)

        resolver = get_instrument_resolver()
        resolver.load()
        sec_id = resolver.get_security_id(upper)
        token = int(sec_id) if sec_id and str(sec_id).isdigit() else 0

        aggregator = get_candle_aggregator()
        raw_bars: List[Dict[str, Any]] = []

        if token > 0:
            raw_bars = list(aggregator.bars_5m.get(token, ()))

        if allow_network and len(raw_bars) < 10 and token > 0:
            now_ts = time.time()
            if now_ts - _5M_FETCH_COOLDOWN.get(token, 0.0) > 120.0:
                try:
                    import os
                    from services.dhan_token_service import get_dhan_token
                    from services.dhan_historical import DhanHistoricalClient
                    client_id = os.getenv("DHAN_CLIENT_ID") or os.getenv("DHAN_USER_ID", "")
                    access_token = get_dhan_token() or os.getenv("DHAN_ACCESS_TOKEN", "")
                    if client_id and access_token:
                        hist_client = DhanHistoricalClient(client_id=client_id, access_token=access_token)
                        fetched = await hist_client.fetch_candles(
                            security_id=str(token),
                            exchange_segment="NSE_EQ",
                            interval_minutes=5,
                            count=100,
                        )
                        if fetched:
                            raw_bars = fetched
                            aggregator.hydrate_history(token, upper, [], fetched)
                except Exception as e:
                    _5M_FETCH_COOLDOWN[token] = now_ts
                    logger.debug("Historical 5m fetch skipped for %s: %s", upper, e)
                # LRU eviction: keep cooldown dict from growing unbounded (runs regardless of success/failure)
                if len(_5M_FETCH_COOLDOWN) > _5M_FETCH_COOLDOWN_MAX:
                    oldest = min(_5M_FETCH_COOLDOWN, key=_5M_FETCH_COOLDOWN.get)  # type: ignore
                    _5M_FETCH_COOLDOWN.pop(oldest, None)

        if raw_bars:
            bars_out = []
            for b in raw_bars:
                t = int(float(b.get("start_time", b.get("time", 0.0))))
                bars_out.append(CandleBar(
                    time=t,
                    open=float(b["open"]),
                    high=float(b["high"]),
                    low=float(b["low"]),
                    close=float(b["close"]),
                    volume=float(b.get("volume", 1.0)),
                ))
            return bars_out

        # Offline / synthetic baseline for 5m intraday analysis
        from datetime import time as dt_time, timedelta
        import math
        now_ist = datetime.now(_IST)
        session_date = now_ist.date()
        if session_date.weekday() == 5:
            session_date -= timedelta(days=1)
        elif session_date.weekday() == 6:
            session_date -= timedelta(days=2)
        elif now_ist.time() < dt_time(9, 15):
            session_date -= timedelta(days=1)
            if session_date.weekday() == 6:
                session_date -= timedelta(days=2)

        session_open_dt = datetime.combine(session_date, dt_time(9, 15), tzinfo=_IST)
        start_ts = int(session_open_dt.timestamp())
        base_price = 1000.0 + (abs(hash(upper)) % 1500)
        curr_price = base_price
        syn_bars = []
        for i in range(75):
            t = start_ts + (i * 300)
            noise = math.sin(i / 6.0) * (base_price * 0.003)
            o = round(curr_price, 2)
            c = round(curr_price + noise + ((i % 3) - 1) * 0.5, 2)
            h = round(max(o, c) + abs(math.cos(i / 4.0)) * 2.0, 2)
            l = round(min(o, c) - abs(math.sin(i / 4.0)) * 2.0, 2)
            v = round(5000.0 + (abs(math.sin(i / 5.0)) * 15000.0), 2)
            syn_bars.append(CandleBar(time=t, open=o, high=h, low=l, close=c, volume=v))
            curr_price = c
        return syn_bars

    async def analyze_symbol_intraday(
        self,
        symbol: str,
    ) -> Dict[str, Any]:
        from services.dhan_instrument_resolver import resolve_canonical_symbol
        upper = resolve_canonical_symbol(symbol)
        bars_5m = await self.get_5m_bars(upper)
        if not bars_5m:
            return {"status": "UNAVAILABLE", "message": f"No 5m intraday data for {upper}"}

        # Calculate Intraday VWAP
        cum_pv = 0.0
        cum_v = 0.0
        vwap_arr = []
        for b in bars_5m:
            typ = (b.high + b.low + b.close) / 3.0
            cum_pv += typ * b.volume
            cum_v += b.volume
            vwap_arr.append(cum_pv / max(cum_v, 1.0))

        offset = max(0, len(bars_5m) - 75)
        return {
            "symbol": upper,
            "mode": "intraday",
            "timeframe": "5m",
            "current_price": round(bars_5m[-1].close, 2),
            "price": round(bars_5m[-1].close, 2),
            "vwap": round(vwap_arr[-1], 2),
            "bars_count": len(bars_5m),
            "candles_5m": [b.to_dict() for b in bars_5m[-75:]],
            "vwap_series": [{"time": b.time, "value": round(vwap_arr[offset + idx], 2)} for idx, b in enumerate(bars_5m[-75:])],
            "analyzed_at": datetime.now(_IST).isoformat(),
        }


# Singleton accessor
_ANALYSIS_SERVICE: Optional[MultiTFAnalysisService] = None

def get_multi_tf_analysis_service() -> MultiTFAnalysisService:
    global _ANALYSIS_SERVICE
    if _ANALYSIS_SERVICE is None:
        _ANALYSIS_SERVICE = MultiTFAnalysisService()
    return _ANALYSIS_SERVICE
