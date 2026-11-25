"""
Centralized Indicator & Telemetry Cache (VIA-Agent7)
====================================================
Thread-safe, module-level TTL caches for RSI (Wilder 14-period), RVOL,
Daily Baselines, and REST Quotes shared across all routers and services:
- routers.analysis
- routers.shortlist_shorts
- services.short_screener_engine
- services.ai_shortlist_service_shorts
"""

from __future__ import annotations

import time
import numpy as np
import cachetools
from typing import Any, Dict, Optional

# In-memory capacity-bounded TTL caches (LRU + TTL eviction)
_rsi_cache: cachetools.TTLCache = cachetools.TTLCache(maxsize=1024, ttl=300.0)
_rvol_cache: cachetools.TTLCache = cachetools.TTLCache(maxsize=1024, ttl=300.0)
_symbol_baseline_cache: cachetools.TTLCache = cachetools.TTLCache(maxsize=1024, ttl=3600.0)
_dhan_rest_quotes_cache: cachetools.TTLCache = cachetools.TTLCache(maxsize=1024, ttl=300.0)

_RSI_CACHE_TTL = 60.0       # Refresh RSI every 60 seconds
_RVOL_CACHE_TTL = 30.0      # Refresh RVOL every 30 seconds
_BASELINE_CACHE_TTL = 3600.0  # Daily baselines valid for 1 hour


def get_cached_rsi(symbol: str, max_age: float = _RSI_CACHE_TTL) -> Optional[float]:
    """Retrieve cached RSI if present and within max_age TTL."""
    sym = symbol.strip().upper()
    entry = _rsi_cache.get(sym)
    if entry and (time.time() - entry.get("ts", 0.0)) < max_age:
        val = entry.get("value")
        if val is not None:
            return float(val)
    return None


def get_cached_real_rsi(symbol: str, max_age: float = _RSI_CACHE_TTL) -> Optional[float]:
    """Retrieve verified real Wilder RSI (from 5m candles, not synthetic fallback)."""
    sym = symbol.strip().upper()
    entry = _rsi_cache.get(sym)
    if entry and entry.get("is_real", False) and (time.time() - entry.get("ts", 0.0)) < max_age:
        val = entry.get("value")
        if val is not None:
            return float(val)
    return None


def set_cached_rsi(symbol: str, value: float, is_real: bool = True) -> None:
    """Store or update cached RSI value."""
    sym = symbol.strip().upper()
    _rsi_cache[sym] = {
        "value": round(float(value), 1),
        "ts": time.time(),
        "is_real": bool(is_real),
    }


def get_cached_rvol(symbol: str, max_age: float = _RVOL_CACHE_TTL) -> Optional[float]:
    """Retrieve cached RVOL if present and within max_age TTL."""
    sym = symbol.strip().upper()
    entry = _rvol_cache.get(sym)
    if entry and (time.time() - entry.get("ts", 0.0)) < max_age:
        val = entry.get("value")
        if val is not None:
            return float(val)
    return None


def set_cached_rvol(symbol: str, value: float, avg_vol: float = 0.0) -> None:
    """Store or update cached RVOL value."""
    sym = symbol.strip().upper()
    _rvol_cache[sym] = {
        "value": round(float(value), 2),
        "avg_vol": float(avg_vol),
        "ts": time.time(),
    }


def clear_indicator_cache() -> None:
    """Clear all cached indicator readings."""
    _rsi_cache.clear()
    _rvol_cache.clear()
    _symbol_baseline_cache.clear()
    _dhan_rest_quotes_cache.clear()


try:
    from numba import njit

    @njit(cache=True, fastmath=True)
    def _wilder_smooth_jit(gains: np.ndarray, losses: np.ndarray, avg_gain: float, avg_loss: float, period: int):
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        return avg_gain, avg_loss
except ImportError:
    def _wilder_smooth_jit(gains: np.ndarray, losses: np.ndarray, avg_gain: float, avg_loss: float, period: int):
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        return avg_gain, avg_loss


def _compute_rsi_wilder(closes: List[float], period: int = 14) -> Optional[float]:
    """Compute RSI using Wilder's smoothing method (exponential moving average of gains/losses)."""
    n = len(closes)
    if n < period + 1:
        return None
    arr = np.asarray(closes, dtype=np.float64)
    deltas = np.diff(arr)
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    avg_gain, avg_loss = _wilder_smooth_jit(gains, losses, avg_gain, avg_loss, period)
    if avg_loss == 0.0:
        if avg_gain == 0.0:
            return 50.0  # Flat series is neutral
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)

