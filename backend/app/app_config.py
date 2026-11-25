# backend/app/app_config.py
"""
Application-wide configuration constants.

All ``os.getenv`` calls for config are consolidated here so every router
and service imports from one place rather than re-reading the environment
directly.  This also makes test monkeypatching easier.

Dhan broker API configuration. Execution layer in screener-only mode.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_allowed_origins(frontend_url: str, production_mode: bool) -> List[str]:
    raw = str(os.getenv("ALLOWED_ORIGINS") or "").strip()
    candidates: List[str] = []

    if raw:
        candidates.extend([item.strip() for item in raw.split(",") if item.strip()])

    if frontend_url:
        candidates.append(frontend_url.strip())

    if not production_mode:
        candidates.extend(["http://localhost:3000", "http://localhost:5173"])
    
    deduped: List[str] = []
    seen: set = set()
    for origin in candidates:
        if origin in seen:
            continue
        seen.add(origin)
        deduped.append(origin)
    return deduped


# ── Runtime flags ─────────────────────────────────────────────────────────────

# Dhan broker configuration
USE_DHAN_MOCK: bool = _env_bool("USE_DHAN_MOCK", False)
DHAN_ENABLED: bool = _env_bool("DHAN_ENABLED", True)
DHAN_DEPTH_TIER: int = int(os.getenv("DHAN_DEPTH_TIER", "20"))  # 20 or 200
DHAN_ENV: str = os.getenv("DHAN_ENV", "sandbox").strip().lower()
DHAN_BASE_URL: str = os.getenv(
    "DHAN_BASE_URL",
    "https://sandbox.dhan.co/v2" if DHAN_ENV == "sandbox" else "https://api.dhan.co/v2",
)
DHAN_CLIENT_ID: str = (
    os.getenv("DHAN_USER_ID")
    or os.getenv("DHAN_CLIENT_ID")
    or os.getenv("DHAN_API_KEY")
    or ""
)
DHAN_ACCESS_TOKEN: str = os.getenv("DHAN_ACCESS_TOKEN") or os.getenv("DHAN_API_SECRET") or ""
DHAN_API_KEY: str = os.getenv("DHAN_API_KEY") or DHAN_CLIENT_ID
DHAN_API_SECRET: str = os.getenv("DHAN_API_SECRET") or DHAN_ACCESS_TOKEN

PRODUCTION_MODE: bool = _env_bool("PRODUCTION_MODE", False)
FAIL_CLOSED_ON_LIVE_ERROR: bool = _env_bool("FAIL_CLOSED_ON_LIVE_ERROR", PRODUCTION_MODE)
ALLOW_STREAM_TOKEN_QUERY: bool = _env_bool("ALLOW_STREAM_TOKEN_QUERY", True)


# ── Execution gate ────────────────────────────────────────────────────────────
# Master execution switch is dynamically governed by CAPITAL_CONFIG.execution_enabled
# in config/trading_config.py (persisted in watchdog_settings.json).
ENABLE_LIVE_ORDERS: bool = False

# ── Network / URL ─────────────────────────────────────────────────────────────

FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")
ALLOWED_ORIGINS: List[str] = _parse_allowed_origins(FRONTEND_URL, PRODUCTION_MODE)

# ── Cookie names ──────────────────────────────────────────────────────────────

SESSION_COOKIE_NAME: str = (
    str(os.getenv("SESSION_COOKIE_NAME") or "app_session").strip() or "app_session"
)
STREAM_COOKIE_NAME: str = (
    str(os.getenv("STREAM_COOKIE_NAME") or "stream_session").strip() or "stream_session"
)
_raw_samesite = str(os.getenv("AUTH_COOKIE_SAMESITE") or "lax").strip().lower()
AUTH_COOKIE_SAMESITE: str = _raw_samesite if _raw_samesite in {"lax", "strict", "none"} else "lax"

# ── Token TTLs ────────────────────────────────────────────────────────────────

SESSION_BOOTSTRAP_TTL_SECONDS: int = 300
STREAM_TOKEN_TTL_SECONDS: int = 300

# ── User defaults ─────────────────────────────────────────────────────────────

DEFAULT_DEV_USER_ID: str = (
    str(os.getenv("DEFAULT_APP_USER_ID") or "local_user").strip() or "local_user"
)


# ── Quantitative Parameter Centralization & Telemetry Hooks ──────────────────
# All quantitative thresholds are centralized here. Every analysis evaluation
# and trade journal event stamps `config_version` and `config_hash` to cleanly
# isolate parameter tuning from broader market regime effects.

QUANT_CONFIG: Dict[str, Any] = {
    # Config Metadata
    "config_version": "v1.1.0",

    # Degenerate S/R Sentinel
    "degenerate_sr_sentinel_score": 20,   # Low defensive score when support >= resistance

    # Reversal & Liquidity Gate
    "reversal_cvd_ratio_max": 0.08,       # Max aggressive delta fraction of bar volume for exhaustion
    "reversal_min_bar_vol": 500,          # Minimum shares per bar to evaluate CVD ratio
    "reversal_min_turnover_inr": 25000.0, # Minimum bar turnover (₹25,000) to avoid illiquid noise

    # Dynamic Volatility-Normalized Units (k * ATR_14)
    "breakout_atr_mult": 0.20,            # Breakout buffer: Price > Resistance + (0.20 * ATR)
    "breakdown_atr_mult": 0.20,           # Breakdown buffer: Price < Support - (0.20 * ATR)
    "proximity_atr_mult": 0.50,           # Pre-breakout proximity: (Resistance - Price) <= (0.50 * ATR)
    "ma_touch_atr_mult": 0.25,            # MA / AVWAP touch window: |Price - MA| <= (0.25 * ATR)

    # Collinearity & Catalyst Controls
    "max_compression_bonus": 30,          # Cap combined tightness (+25) + squeeze (+15) at 30 pts to prevent flat-base distortion
    "active_catalyst_bonus": 10,          # Priority bonus for active breakout/bounce/flow triggers over passive flat chop

    # Liquidity & Execution Drag Defense
    "min_turnover_liquid_inr": 50000000.0,# ₹5 Crore minimum 20-day ADV turnover floor
    "illiquid_drag_penalty": 10,          # Points deducted when turnover < ₹5 Crore to price in spread/slippage
    "illiquid_tier_cap": "B",             # Hard tier ceiling for illiquid equities

    # Legacy Percentage Fallback Bands (Preserved for compatibility)
    "structural_breakdown_band": 0.99,    # Price < Support * 0.99
    "structural_reclaim_band": 1.01,      # Price > Resistance * 1.01

    # Veto Caps & Floors
    "veto_breakdown_cap": 35,             # Hard score ceiling on broken support
    "veto_distribution_cap": 40,          # Hard score ceiling on institutional distribution
    "veto_reclaim_floor": 65,             # Hard score floor on resistance reclaim
    "veto_accumulation_floor": 60,        # Hard score floor on institutional accumulation

    # Confluence & Conflict Extremes
    "concordant_bullish_floor": 75,       # Score floor when breakout is confirmed by accumulation
    "concordant_bearish_cap": 20,         # Score cap when breakdown is confirmed by distribution

    # Doji & Span Protections
    "doji_body_bps": 0.0008,              # 8 bps price floor for effective candle body
    "doji_abs_span_floor": 0.05,          # 5 paise minimum span

    # Breakout RVOL
    "breakout_rvol_tod_min": 1.4,         # Minimum ToD RVOL for breakout ignition
}


def _compute_config_hash() -> str:
    """Compute deterministic 8-char SHA-256 fingerprint of current QUANT_CONFIG."""
    serialized = json.dumps(QUANT_CONFIG, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:8]


# Cached once at process startup for zero runtime overhead
QUANT_CONFIG_HASH: str = _compute_config_hash()
