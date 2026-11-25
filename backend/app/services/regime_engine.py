"""
Deterministic regime engine backed by static priors.

This module preserves the legacy public API used across the backend while
removing runtime HMM/GARCH dependencies.
"""

import logging
import time
from typing import Any, Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

REGIME_ORDER = [
    "Calm Uptrend",
    "Calm Downtrend",
    "High-Vol Trend",
    "High-Vol Chop",
]

STATE_INDEX = {name: idx for idx, name in enumerate(REGIME_ORDER)}


def _normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
    cleaned = {k: max(float(v), 0.0) for k, v in weights.items()}
    total = sum(cleaned.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in cleaned.items()}


def classify_volatility_percentile(percentile: float) -> str:
    if percentile >= 95.0:
        return "extreme"
    if percentile >= 80.0:
        return "high"
    if percentile >= 20.0:
        return "normal"
    return "low"


def interpolate_probability(setup_type: str, vol_state: str) -> float:
    base_probs = {
        "vwap_continuation": {"low": 0.60, "normal": 0.68, "high": 0.62, "extreme": 0.55},
        "compression_breakout": {"low": 0.58, "normal": 0.72, "high": 0.65, "extreme": 0.52},
        "cvd_divergence": {"low": 0.55, "normal": 0.65, "high": 0.60, "extreme": 0.50},
    }
    return base_probs.get(setup_type, {}).get(vol_state, 0.60)


_uniform_factors = {
    "F1_Volume": 0.1, "F2_Breakout": 0.1, "F3_RSI": 0.1, "F4_RR": 0.1,
    "F5_Trend": 0.1, "F6_Momentum": 0.1, "F7_VCP": 0.1, "F8_Liquidity": 0.1,
    "F9_VWAP": 0.1, "F10_OrderFlow": 0.1,
}
DEFAULT_WEIGHTS = {k: v / sum(_uniform_factors.values()) for k, v in _uniform_factors.items()}

# Regime-specific overlays on top of static default factor priors.
REGIME_WEIGHTS = {
    "Calm Uptrend": _normalize_weights(
        {
            **DEFAULT_WEIGHTS,
            "F6_Momentum": DEFAULT_WEIGHTS.get("F6_Momentum", 0.0) + 0.05,
            "F9_VWAP": DEFAULT_WEIGHTS.get("F9_VWAP", 0.0) + 0.03,
        }
    ),
    "Calm Downtrend": _normalize_weights(
        {
            **DEFAULT_WEIGHTS,
            "F4_RR": DEFAULT_WEIGHTS.get("F4_RR", 0.0) + 0.05,
            "F10_OrderFlow": DEFAULT_WEIGHTS.get("F10_OrderFlow", 0.0) + 0.03,
        }
    ),
    "High-Vol Trend": _normalize_weights(
        {
            **DEFAULT_WEIGHTS,
            "F2_Breakout": DEFAULT_WEIGHTS.get("F2_Breakout", 0.0) + 0.07,
            "F10_OrderFlow": DEFAULT_WEIGHTS.get("F10_OrderFlow", 0.0) + 0.05,
        }
    ),
    "High-Vol Chop": _normalize_weights(
        {
            **DEFAULT_WEIGHTS,
            "F9_VWAP": DEFAULT_WEIGHTS.get("F9_VWAP", 0.0) + 0.06,
            "F4_RR": DEFAULT_WEIGHTS.get("F4_RR", 0.0) + 0.04,
        }
    ),
}


class HMMRegimeDetector:
    """
    Backward-compatible deterministic replacement for the old HMM detector.
    """

    def __init__(self, lookback: int = 140, cooldown_sec: int = 1800):
        self.lookback = int(lookback)
        self.cooldown_sec = int(cooldown_sec)
        self.is_fitted = False
        self.state_map = dict(enumerate(REGIME_ORDER))
        self._last_fit_ts = 0.0
        self._india_vix: Optional[float] = None
        self._baseline_abs_vol = 0.0
        self.running = False

    async def start(self):
        self.running = True
        logger.info("Deterministic regime detector started")

    async def stop(self):
        self.running = False
        logger.info("Deterministic regime detector stopped")

    async def health_check(self) -> bool:
        return True

    def get_status(self) -> Dict[str, Any]:
        return {
            "status": "running" if self.running else "stopped",
            "is_fitted": self.is_fitted,
            "last_fit_ts": self._last_fit_ts,
            "india_vix": self._india_vix,
        }

    def set_india_vix(self, value: Optional[float]):
        self._india_vix = None if value is None else float(value)

    def fit_bootstrap(self, historical_returns: np.ndarray):
        arr = np.asarray(historical_returns, dtype=np.float64)
        if arr.size < 10:
            logger.warning("Insufficient data for regime bootstrap (%d samples)", arr.size)
            return
        self._baseline_abs_vol = float(np.median(np.abs(arr)))
        self._last_fit_ts = time.time()
        self.is_fitted = True

    def needs_refit(self) -> bool:
        return (time.time() - self._last_fit_ts) > self.cooldown_sec

    def _classify_regime(self, returns: np.ndarray) -> Tuple[str, str, float, float]:
        recent = np.asarray(returns, dtype=np.float64)
        if recent.size > self.lookback:
            recent = recent[-self.lookback :]

        abs_ret = np.abs(recent)
        latest_abs = float(abs_ret[-1]) if abs_ret.size else 0.0
        percentile = float((abs_ret <= latest_abs).mean() * 100.0) if abs_ret.size else 50.0
        vol_state = classify_volatility_percentile(percentile)

        drift = float(np.mean(recent[-min(20, recent.size) :])) if recent.size else 0.0
        abs_drift = abs(drift)

        high_vix = 20.0

        high_vol_context = vol_state in {"high", "extreme"}
        if self._india_vix is not None and self._india_vix >= high_vix:
            high_vol_context = True

        trend_threshold = max(5e-5, (self._baseline_abs_vol or 5e-5) * 0.6)

        if high_vol_context:
            if abs_drift >= trend_threshold:
                return "High-Vol Trend", vol_state, percentile, drift
            return "High-Vol Chop", vol_state, percentile, drift

        if drift >= 0:
            return "Calm Uptrend", vol_state, percentile, drift
        return "Calm Downtrend", vol_state, percentile, drift

    def predict_macro_regime(self, recent_returns: np.ndarray):
        arr = np.asarray(recent_returns, dtype=np.float64)
        if arr.size < 3:
            probs = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float64)
            return "Calm Uptrend", probs, dict(DEFAULT_WEIGHTS)

        regime_name, vol_state, _percentile, drift = self._classify_regime(arr)

        if regime_name in {"Calm Uptrend", "Calm Downtrend"}:
            base_conf = interpolate_probability("vwap_continuation", vol_state)
        elif regime_name == "High-Vol Trend":
            base_conf = interpolate_probability("compression_breakout", vol_state)
        else:
            base_conf = interpolate_probability("cvd_divergence", vol_state)

        drift_boost = min(abs(float(drift)) * 3000.0, 0.20)
        dominant_p = float(np.clip(base_conf + drift_boost, 0.52, 0.92))

        probs = np.full(len(REGIME_ORDER), (1.0 - dominant_p) / (len(REGIME_ORDER) - 1), dtype=np.float64)
        probs[STATE_INDEX[regime_name]] = dominant_p

        self.is_fitted = True
        weights = dict(REGIME_WEIGHTS.get(regime_name, DEFAULT_WEIGHTS))
        return regime_name, probs, weights

    def predict_micro_regime(self, symbol_returns: np.ndarray, macro_weights: Dict[str, float]):
        arr = np.asarray(symbol_returns, dtype=np.float64)
        if arr.size < 3:
            return _normalize_weights(dict(macro_weights))

        local_vol = float(np.std(arr[-min(20, arr.size) :]))
        local_drift = float(np.mean(arr[-min(10, arr.size) :]))

        adjusted = dict(macro_weights)

        # Compression profile: slightly prioritize VCP and participation.
        if local_vol < 0.001:
            adjusted["F7_VCP"] = adjusted.get("F7_VCP", 0.0) + 0.06
            adjusted["F1_Volume"] = adjusted.get("F1_Volume", 0.0) + 0.04
            adjusted["F2_Breakout"] = max(adjusted.get("F2_Breakout", 0.0) - 0.04, 0.0)

        # High-noise profile: de-emphasize breakout, emphasize risk/mean management.
        if local_vol > 0.003:
            adjusted["F2_Breakout"] = max(adjusted.get("F2_Breakout", 0.0) - 0.08, 0.0)
            adjusted["F4_RR"] = adjusted.get("F4_RR", 0.0) + 0.05
            adjusted["F9_VWAP"] = adjusted.get("F9_VWAP", 0.0) + 0.03

        # Directional drift nudges momentum/trend factors.
        if local_drift > 0:
            adjusted["F6_Momentum"] = adjusted.get("F6_Momentum", 0.0) + 0.03
            adjusted["F5_Trend"] = adjusted.get("F5_Trend", 0.0) + 0.02
        elif local_drift < 0:
            adjusted["F4_RR"] = adjusted.get("F4_RR", 0.0) + 0.02

        return _normalize_weights(adjusted)


_regime_engine: Optional[HMMRegimeDetector] = None


def get_regime_engine() -> HMMRegimeDetector:
    global _regime_engine
    if _regime_engine is None:
        _regime_engine = HMMRegimeDetector()
    return _regime_engine
