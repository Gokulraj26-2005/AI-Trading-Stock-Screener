"""
Volatility Modelling — EWMA Real-Time + GARCH Nightly  (§2.3)
--------------------------------------------------------------
EWMA: per-tick / per-bar real-time variance estimator.
GARCH(1,1): nightly offline fit → recalibrates EWMA lambda for next session.

Public surface
--------------
  VolatilityEngine.update_bar(symbol, log_return)  → EWMAResult (per bar)
  VolatilityEngine.classify_vol_state(symbol)       → "quiet" | "normal" | "high"
  VolatilityEngine.recalibrate_lambda(returns)      → float   (new λ for next session)
  VolatilityEngine.reset_session(symbol)            → None    (call at 09:15 IST)
  VolatilityEngine.get_band_multiplier(symbol)      → float   (for stop / R:R sizing)
"""
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, Deque

import numpy as np

logger = logging.getLogger(__name__)

try:
    from arch import arch_model
    HAS_ARCH = True
except ImportError:
    HAS_ARCH = False
    logger.warning("arch package not available - GARCH model disabled")

# ── Constants ──────────────────────────────────────────────────────────────
DEFAULT_LAMBDA      = 0.94        # RiskMetrics canonical value for 5-min bars
MIN_LAMBDA          = 0.90
MAX_LAMBDA          = 0.99
VOL_WINDOW          = 30          # bars for rolling quantile classification
QUIET_QUANTILE      = 0.33        # bottom third  → quiet / compression
HIGH_QUANTILE       = 0.67        # top third     → elevated risk

# Stop / R:R band multipliers per vol state
BAND_MULTIPLIERS: Dict[str, float] = {
    "quiet":  0.8,    # tight entries, tighter stops
    "normal": 1.0,
    "high":   1.5,    # wider stops, staged exits
}


# ── Data classes ──────────────────────────────────────────────────────────

@dataclass
class EWMAResult:
    """Output of a single bar update."""
    symbol:     str
    ewma_var:   float          # σ²_t  (variance)
    ewma_vol:   float          # σ_t   (annualised? no — in log-return units)
    log_return: float
    vol_state:  str            # "quiet" | "normal" | "high"
    band_mult:  float          # stop / R:R multiplier
    timestamp:  float = field(default_factory=time.time)


@dataclass
class GARCHResult:
    """Output of nightly GARCH(1,1) fit."""
    omega:           float
    alpha:           float
    beta:            float
    long_run_var:    float       # ω / (1 − α − β)
    calibrated_lambda: float     # β + α/2  (heuristic)


# ── Per-symbol state ──────────────────────────────────────────────────────

class _SymbolVolState:
    """
    All mutable per-symbol tracking data.
    Stored inside VolatilityEngine._states[symbol].
    """
    __slots__ = ("ewma_var", "lam", "vol_window", "long_run_var", "session_open_ts")

    def __init__(self, lam: float = DEFAULT_LAMBDA, window: int = VOL_WINDOW):
        self.ewma_var:      float           = 0.0
        self.lam:           float           = lam
        # Rolling window of ewma_vol values for quantile classification
        self.vol_window:    Deque[float]    = deque(maxlen=window)
        # Long-run variance from last nightly GARCH (used for anomaly reference)
        self.long_run_var:  float           = 0.0
        self.session_open_ts: float         = 0.0


# ── Main Engine ───────────────────────────────────────────────────────────

class VolatilityEngine:
    """
    Per-symbol EWMA real-time volatility tracker.

    Usage pattern
    -------------
    Pre-session (scheduler, ~09:00 IST):
        ve.reset_session("RELIANCE")        # clear VWAP / EWMA state
        ve.recalibrate_lambda(hist_returns) # optional; uses GARCH

    Per bar (on every 1m / 5m bar close):
        result = ve.update_bar("RELIANCE", log_return)
        # result.vol_state  →  "quiet" | "normal" | "high"
        # result.band_mult  →  multiplier for stop width and R:R bands
    """

    def __init__(self, default_lambda: float = DEFAULT_LAMBDA):
        self._default_lambda = default_lambda
        self._states: Dict[str, _SymbolVolState] = {}

    # ── Session management ─────────────────────────────────────────────────

    def reset_session(self, symbol: str):
        """
        Call at market open (09:15 IST) for each tracked symbol.
        Preserves calibrated lambda from the nightly GARCH run but
        clears the intraday rolling window and resets EWMA.
        """
        if symbol in self._states:
            prev_lam      = self._states[symbol].lam
            prev_long_run = self._states[symbol].long_run_var
        else:
            prev_lam      = self._default_lambda
            prev_long_run = 0.0

        self._states[symbol] = _SymbolVolState(lam=prev_lam)
        self._states[symbol].long_run_var   = prev_long_run
        self._states[symbol].session_open_ts = time.time()
        logger.debug("Vol session reset for %s (λ=%.4f)", symbol, prev_lam)

    def _get_or_init(self, symbol: str) -> _SymbolVolState:
        if symbol not in self._states:
            self._states[symbol] = _SymbolVolState(lam=self._default_lambda)
        return self._states[symbol]

    # ── Real-time EWMA update ──────────────────────────────────────────────

    def update_bar(self, symbol: str, log_return: float) -> EWMAResult:
        """
        Apply one EWMA variance step:

            σ²_t = λ σ²_{t-1} + (1−λ) r²_t

        Append σ_t to the rolling window and classify vol state.
        """
        st = self._get_or_init(symbol)
        lam = st.lam

        # EWMA variance update
        st.ewma_var = lam * st.ewma_var + (1.0 - lam) * (log_return ** 2)
        ewma_vol    = float(np.sqrt(max(st.ewma_var, 0.0)))

        # Append to rolling window for quantile classification
        st.vol_window.append(ewma_vol)

        vol_state = self._classify(st)
        band_mult = BAND_MULTIPLIERS[vol_state]

        return EWMAResult(
            symbol     = symbol,
            ewma_var   = st.ewma_var,
            ewma_vol   = ewma_vol,
            log_return = log_return,
            vol_state  = vol_state,
            band_mult  = band_mult,
        )

    # ── Vol-state classification ───────────────────────────────────────────

    def _classify(self, st: _SymbolVolState) -> str:
        """
        Classify the current bar into quiet / normal / high using quantiles
        of the rolling VOL_WINDOW observations.

        Requires the full rolling window; defaults to 'normal' while warming up.
        """
        window = list(st.vol_window)
        if len(window) < VOL_WINDOW:
            return "normal"

        arr     = np.array(window, dtype=np.float64)
        current = arr[-1]
        q_low   = float(np.quantile(arr, QUIET_QUANTILE))
        q_high  = float(np.quantile(arr, HIGH_QUANTILE))

        if current <= q_low:
            return "quiet"
        elif current >= q_high:
            return "high"
        else:
            return "normal"

    def classify_vol_state(self, symbol: str) -> str:
        """Public accessor for the latest vol state without updating EWMA."""
        if symbol not in self._states:
            return "normal"
        return self._classify(self._states[symbol])

    # ── Band multiplier accessor ───────────────────────────────────────────

    def get_band_multiplier(self, symbol: str) -> float:
        """
        Returns the stop / R:R width multiplier for the current vol state.
        0.8 for quiet compression, 1.0 for normal, 1.5 for high-vol.
        """
        state = self.classify_vol_state(symbol)
        return BAND_MULTIPLIERS.get(state, 1.0)

    def get_long_run_var(self, symbol: str) -> float:
        """
        Returns the long-run GARCH variance baseline (ω / (1−α−β)).
        0.0 until the first nightly GARCH run completes.
        """
        if symbol not in self._states:
            return 0.0
        return self._states[symbol].long_run_var

    # ── Nightly GARCH(1,1) recalibration ──────────────────────────────────

    def recalibrate_lambda(
        self, returns: np.ndarray, symbol: Optional[str] = None
    ) -> GARCHResult:
        """
        Nightly offline GARCH(1,1) fit via maximum likelihood estimation.

        Steps
        -----
        1. Fit GARCH(1,1) on `returns` (log-returns, full session or multi-day).
        2. Extract α, β, ω from fitted parameters.
        3. Compute long-run variance:  ω / (1 − α − β)
        4. Heuristic lambda recalibration:  λ_new = β + α/2
           (approximates the EWMA persistence implied by the GARCH structure)
        5. Clamp λ to [MIN_LAMBDA, MAX_LAMBDA].
        6. If `symbol` is provided, update its stored lambda immediately.

        Returns a GARCHResult with all intermediate values for audit logging.
        """
        returns_pct = returns * 100.0  # scale for ARCH optimizer numerical stability
        try:
            am  = arch_model(returns_pct, vol="Garch", p=1, q=1, rescale=False)
            res = am.fit(update_freq=0, disp="off")
            params = res.params

            omega = float(params.get("omega", 1e-6))
            alpha = float(params.get("alpha[1]", 0.05))
            beta  = float(params.get("beta[1]",  0.90))

            # Clamp to stationarity
            if alpha + beta >= 1.0:
                logger.warning("GARCH non-stationary (α+β=%.4f). Clamping.", alpha + beta)
                ratio  = 0.995 / (alpha + beta)
                alpha *= ratio
                beta  *= ratio

            denom        = max(1.0 - alpha - beta, 1e-8)
            long_run_var = (omega / 100.0 ** 2) / denom   # rescale back from pct²

            # Lambda heuristic: β captures persistence, α/2 adds responsiveness
            lam_new = float(np.clip(beta + alpha / 2.0, MIN_LAMBDA, MAX_LAMBDA))

            garch_result = GARCHResult(
                omega            = omega / 100.0 ** 2,
                alpha            = alpha,
                beta             = beta,
                long_run_var     = long_run_var,
                calibrated_lambda= lam_new,
            )

            logger.info(
                "GARCH fit: ω=%.6f α=%.4f β=%.4f  LR-var=%.6f  λ→%.4f",
                garch_result.omega, alpha, beta, long_run_var, lam_new
            )

            # Apply calibrated lambda (+ long-run var) to the given symbol
            if symbol is not None:
                st = self._get_or_init(symbol)
                st.lam          = lam_new
                st.long_run_var = long_run_var

            return garch_result

        except Exception as exc:
            logger.error("GARCH recalibration failed: %s. Keeping λ=%.4f", exc, self._default_lambda)
            return GARCHResult(
                omega=0.0, alpha=0.05, beta=0.90,
                long_run_var=0.0,
                calibrated_lambda=self._default_lambda,
            )

    def recalibrate_all_symbols(self, returns: np.ndarray) -> GARCHResult:
        """
        Convenience: run one GARCH fit on index-level returns and apply the
        calibrated lambda to every tracked symbol (macro vol baseline).
        """
        garch_result = self.recalibrate_lambda(returns, symbol=None)
        for sym in self._states:
            self._states[sym].lam          = garch_result.calibrated_lambda
            self._states[sym].long_run_var = garch_result.long_run_var
        logger.info(
            "Lambda updated for %d symbols → %.4f",
            len(self._states), garch_result.calibrated_lambda
        )
        return garch_result

    # ── Snapshot for ScoringEngine integration ────────────────────────────

    def get_snapshot(self, symbol: str) -> Dict[str, float]:
        """
        Returns a flat dict suitable for injecting into the scoring snapshot_df.

        Keys:  ewma_vol, vol_state_num, band_mult, long_run_var,
               current_vs_longrun_ratio
        """
        st = self._get_or_init(symbol)
        ewma_vol    = float(np.sqrt(max(st.ewma_var, 0.0)))
        vol_state   = self._classify(st)
        band_mult   = BAND_MULTIPLIERS[vol_state]

        # vol-state encoded as a numeric for ScoringEngine / pandas
        state_num = {"quiet": 0.0, "normal": 1.0, "high": 2.0}.get(vol_state, 1.0)

        # Ratio of current EWMA vol vs long-run GARCH baseline
        lr_std   = float(np.sqrt(max(st.long_run_var, 0.0)))
        ratio    = (ewma_vol / lr_std) if lr_std > 0 else 1.0

        return {
            "ewma_vol":                ewma_vol,
            "vol_state_num":           state_num,
            "vol_state":               vol_state,
            "band_mult":               band_mult,
            "long_run_var":            st.long_run_var,
            "current_vs_longrun_ratio": ratio,
        }


# ── Singleton ──────────────────────────────────────────────────────────────
_volatility_engine: Optional[VolatilityEngine] = None

def get_volatility_engine() -> VolatilityEngine:
    global _volatility_engine
    if _volatility_engine is None:
        _volatility_engine = VolatilityEngine()
    return _volatility_engine
