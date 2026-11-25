"""
Advanced VWAP Engine — Bands + Microstructure States  (§2.5 — Resolves FLAW 1.2.5)
-------------------------------------------------------------------------------------
Implements the full specification:

VWAP Bollinger Bands
    Three standard-deviation bands computed from square-root of rolling
    VWAP variance:
        VWAP_σ  = sqrt( Σ(volume × (typicalPrice − VWAP)²) / Σvolume )
        Upper_k = VWAP + k × VWAP_σ   k = 1, 2, 3
        Lower_k = VWAP − k × VWAP_σ

Normalised ATR distance
    d_t = (P_t − VWAP_t) / ATR_t (regime-scaled by band_mult)

Anchored VWAPs
    Intraday VWAP  — resets at 09:15 IST (session open)
    Weekly VWAP    — resets on Monday 09:15 IST
    Monthly VWAP   — resets on 1st trading-day 09:15 IST

6-State Microstructure Machine
    Accepted Long  : price above VWAP + 0.5 ATR, CVD trending positive
    Accepted Short : price below VWAP − 0.5 ATR, CVD trending negative
    Fade Long      : price extended above +1.5 ATR from VWAP
    Fade Short     : price extended below −1.5 ATR from VWAP
    Chop           : |d_t| ≤ 0.5 AND VWAP_σ compressed (< σ_quiet threshold)
    Neutral        : all other conditions

3σ Mean-Reversion Flag
    Fires when: |d_t| > 3 AND NOT (CVD expanding in same direction)
    Signals institutional exhaustion / trapped counterpart.

Chop Suppression
    When microstructure == "Chop", trend signal is blocked upstream.
    Scoring engine reads micro_state directly and zeroes trigger_score.
"""
import logging
import numpy as np
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple

try:
    from app.app_config import QUANT_CONFIG
except ImportError:
    from app_config import QUANT_CONFIG

from .volatility_engine import get_volatility_engine

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
ATR_WINDOW           = 14     # bars for rolling ATR
BAND_SIGMA_WINDOW    = 20     # bars for rolling VWAP σ (Bollinger window)
ACCEPT_THRESH_ATR    = 0.5    # d_t > this → Accepted (per vol regime)
EXHAUST_THRESH_ATR   = 1.5    # d_t > this → Fade     (per vol regime)
MEAN_REV_THRESH_ATR  = 3.0    # d_t > this AND no CVD → Mean-Reversion Flag
CHOP_MAX_DT          = 0.5    # |d_t| must be below this for Chop state

# Vol-regime threshold tables (applied to accept/exhaust thresholds)
_ACCEPT_THRESH  = {"quiet": 0.3, "normal": 0.5,  "high": 0.7}
_EXHAUST_THRESH = {"quiet": 1.2, "normal": 1.5,  "high": 1.8}


# ── Per-bar band result ────────────────────────────────────────────────────

@dataclass(slots=True)
class VWAPBands:
    """VWAP ± kσ output for a single symbol at a single bar."""
    vwap:     float
    vwap_std: float           # rolling VWAP σ (Bollinger)
    upper1:   float           # VWAP + 1σ
    upper2:   float           # VWAP + 2σ
    upper3:   float           # VWAP + 3σ
    lower1:   float           # VWAP − 1σ
    lower2:   float           # VWAP − 2σ
    lower3:   float           # VWAP − 3σ

    # Anchored VWAPs
    weekly_vwap:  float
    monthly_vwap: float


@dataclass(slots=True)
class MicroResult:
    """Full microstructure result including bands and state."""
    # VWAP bands
    bands:          VWAPBands

    # Distance metric
    dt:             float   # (close − VWAP) / (ATR × band_mult)
    atr:            float

    # Vol regime
    vol_state:      str     # "quiet" | "normal" | "high"
    band_mult:      float   # 0.8 / 1.0 / 1.5
    ewma_vol:       float

    # Microstructure state
    micro_state:    str     # 6-state label
    micro_score:    float   # signed bias score (−100 to +100)

    # Flags
    mean_rev_flag:  bool    # 3σ mean-reversion setup detected
    chop_active:    bool    # True → suppress trend entries


# ── Per-symbol rolling state ───────────────────────────────────────────────

class _SymbolVWAPState:
    """
    Holds all rolling accumulators for one symbol.
    Intraday VWAP resets at session open.
    Weekly / monthly VWAPs reset at the appropriate boundary.
    """
    __slots__ = (
        # Intraday VWAP
        "cum_vol", "cum_pv", "cum_pv2",
        # Weekly VWAP
        "w_cum_vol", "w_cum_pv",
        # Monthly VWAP
        "m_cum_vol", "m_cum_pv",
        # Rolling ATR / TP buffers
        "_tr_buf", "_tp_history",
        "last_result",
    )

    def __init__(self):
        self.cum_vol   = 0.0
        self.cum_pv    = 0.0
        self.cum_pv2   = 0.0     # Σ(vol × tp²) for volume-weighted variance
        self.w_cum_vol = 0.0
        self.w_cum_pv  = 0.0
        self.m_cum_vol = 0.0
        self.m_cum_pv  = 0.0
        self._tr_buf:     deque = deque(maxlen=ATR_WINDOW)
        self._tp_history: deque = deque(maxlen=BAND_SIGMA_WINDOW)
        self.last_result = None

    def reset_session(self):
        """Intraday VWAP reset — call at 09:15 IST."""
        self.cum_vol = 0.0
        self.cum_pv  = 0.0
        self.cum_pv2 = 0.0

    def reset_weekly(self):
        """Weekly VWAP reset — call Monday 09:15 IST."""
        self.w_cum_vol = 0.0
        self.w_cum_pv  = 0.0

    def reset_monthly(self):
        """Monthly VWAP reset — call 1st trading day 09:15 IST."""
        self.m_cum_vol = 0.0
        self.m_cum_pv  = 0.0


# ── Main Engine ───────────────────────────────────────────────────────────

class VolVWAPEngine:
    """
    Advanced VWAP Engine: bands, anchored VWAPs, 6-state microstructure.

    Per-bar call:
        result = engine.update_bar(symbol, bar, prev_bars, cvd_value)

    Session management:
        engine.reset_session(symbol)   # at 09:15 IST
        engine.reset_weekly(symbol)    # Monday 09:15 IST
        engine.reset_monthly(symbol)   # 1st of month 09:15 IST

    Chop suppression:
        if result.chop_active: skip trigger evaluation entirely

    Mean-reversion flag:
        if result.mean_rev_flag: flag high-probability mean-reversion setup
    """

    def __init__(self):
        self._states:     Dict[str, _SymbolVWAPState] = {}
        self._vol_engine  = get_volatility_engine()
        self.running = False

    async def start(self) -> None:
        """Start the vol VWAP engine service."""
        self.running = True
        logger.info("VolVWAPEngine started")

    async def stop(self) -> None:
        """Stop the vol VWAP engine service."""
        self.running = False
        logger.info("VolVWAPEngine stopped")

    async def health_check(self) -> bool:
        """Check if vol VWAP engine is healthy."""
        return self.running

    async def get_status(self) -> Dict[str, Any]:
        """Get vol VWAP engine status."""
        return {
            'status': 'running' if self.running else 'stopped',
            'active_symbols': len(self._states)
        }

    def _get_state(self, symbol: str) -> _SymbolVWAPState:
        if symbol not in self._states:
            self._states[symbol] = _SymbolVWAPState()
        return self._states[symbol]

    def get_last_result(self, symbol: str) -> Optional[MicroResult]:
        """Return the most recent MicroResult for a symbol, or None if not yet computed."""
        st = self._states.get(symbol)
        return st.last_result if st else None

    # ── Session management ─────────────────────────────────────────────────

    def reset_session(self, symbol: str):
        """Reset intraday VWAP + delegate EWMA reset to VolatilityEngine."""
        self._get_state(symbol).reset_session()
        self._vol_engine.reset_session(symbol)
        logger.debug("VWAP session reset: %s", symbol)

    def reset_weekly(self, symbol: str):
        self._get_state(symbol).reset_weekly()
        logger.debug("VWAP weekly reset: %s", symbol)

    def reset_monthly(self, symbol: str):
        self._get_state(symbol).reset_monthly()
        logger.debug("VWAP monthly reset: %s", symbol)

    # ── Band computation ───────────────────────────────────────────────────

    def _compute_bands(self, vwap: float, st: _SymbolVWAPState) -> Tuple[float, VWAPBands]:
        """
        Volume-weighted standard deviation of typical price around VWAP.
            VWAP_σ = sqrt( (Σ vol×tp²) / Σvol  −  vwap² )
        Returns (vwap_std, VWAPBands).
        """
        if st.cum_vol <= 0:
            return 0.0, VWAPBands(
                vwap=vwap, vwap_std=0.0,
                upper1=vwap, upper2=vwap, upper3=vwap,
                lower1=vwap, lower2=vwap, lower3=vwap,
                weekly_vwap=vwap, monthly_vwap=vwap,
            )

        vwap_variance = max(0.0, (st.cum_pv2 / st.cum_vol) - vwap ** 2)
        vwap_std      = float(np.sqrt(vwap_variance))

        weekly_vwap  = st.w_cum_pv / st.w_cum_vol  if st.w_cum_vol  > 0 else vwap
        monthly_vwap = st.m_cum_pv / st.m_cum_vol  if st.m_cum_vol  > 0 else vwap

        bands = VWAPBands(
            vwap         = vwap,
            vwap_std     = vwap_std,
            upper1       = vwap + 1 * vwap_std,
            upper2       = vwap + 2 * vwap_std,
            upper3       = vwap + 3 * vwap_std,
            lower1       = vwap - 1 * vwap_std,
            lower2       = vwap - 2 * vwap_std,
            lower3       = vwap - 3 * vwap_std,
            weekly_vwap  = weekly_vwap,
            monthly_vwap = monthly_vwap,
        )
        return vwap_std, bands

    # ── 6-State machine ───────────────────────────────────────────────────

    def _classify_state(
        self, dt: float, vwap_std: float, vol_state: str, cvd: float, close_price: float, bar_vol: float = 0.0
    ) -> Tuple[str, float, bool, bool]:
        """
        Returns (micro_state, micro_score, mean_rev_flag, chop_active).

        States:
          Accepted Long  : d_t > accept AND cvd trending +
          Accepted Short : d_t < −accept AND cvd trending −
          Fade Long      : d_t > exhaust (overextended above VWAP)
          Fade Short     : d_t < −exhaust (overextended below VWAP)
          Chop           : |d_t| ≤ CHOP_MAX_DT AND vwap_std compressed
          Neutral        : all others

        Mean-Rev Flag: d_t >= 3.0 AND CVD exhausted (relative CVD ratio < max on liquid bar)
        """
        accept  = _ACCEPT_THRESH.get(vol_state,  0.5)
        exhaust = _EXHAUST_THRESH.get(vol_state, 1.5)

        mean_rev_flag = False
        chop_active   = False

        # ── 3σ Mean-Reversion Check ──────────────────────────────────────
        # Price pushed into 3rd SD with CVD exhaustion (lack of counterpart absorption)
        at_3sigma = abs(dt) >= MEAN_REV_THRESH_ATR
        bar_vol_clamped = max(bar_vol, 0.0)
        bar_turnover = bar_vol_clamped * close_price

        # Compound Liquidity Gate: require BOTH share volume AND turnover floor
        is_liquid = (
            bar_vol_clamped >= QUANT_CONFIG["reversal_min_bar_vol"]
            and bar_turnover >= QUANT_CONFIG["reversal_min_turnover_inr"]
        )

        if is_liquid:
            cvd_ratio = abs(cvd) / bar_vol_clamped if bar_vol_clamped > 0 else 0.0
            cvd_exhausted = cvd_ratio < QUANT_CONFIG["reversal_cvd_ratio_max"]
        else:
            cvd_exhausted = False  # Suppress false exhaustion flags on thin illiquid bars

        if at_3sigma and cvd_exhausted:
            mean_rev_flag = True

        # ── Classify state ───────────────────────────────────────────────
        if dt > exhaust:
            return "Fade Long", -50.0, mean_rev_flag, False

        if dt < -exhaust:
            return "Fade Short", 50.0, mean_rev_flag, False

        if dt > accept and cvd > 0:
            return "Accepted Long", 100.0, False, False

        if dt < -accept and cvd < 0:
            return "Accepted Short", -100.0, False, False

        # Chop only when price is near VWAP and the band width is genuinely compressed.
        # The prior self-referential sigma check could never express compression.
        sigma_quiet = vwap_std <= max(0.25, close_price * 0.003)
        if abs(dt) <= CHOP_MAX_DT and sigma_quiet:
            chop_active = True
            return "Chop", 0.0, False, True

        return "Neutral", 0.0, False, False

    # ── Per-bar update ─────────────────────────────────────────────────────

    def update_bar(
        self,
        symbol:    str,
        bar:       Dict[str, float],
        prev_bars: List[Dict[str, float]],
        cvd:       float = 0.0,       # current running CVD from CVDEngine
    ) -> MicroResult:
        """
        Update VWAP state and return full microstructure result.

        bar dict requires: 'close', 'high', 'low', 'volume'
        prev_bars: list of prior bars (only last entry used for log-return / TR)
        cvd: running CVD from CVDEngine.process_tick() (positive = net buying)

        Returns
        -------
        MicroResult with bands, dt, micro_state, micro_score, flags
        """
        st = self._get_state(symbol)

        # ── 1. Typical Price & VWAP accumulation ─────────────────────────
        tp  = (bar["high"] + bar["low"] + bar["close"]) / 3.0
        vol = max(bar["volume"], 0.0)
        st._tp_history.append(tp)

        # Intraday VWAP
        st.cum_vol  += vol
        st.cum_pv   += tp * vol
        st.cum_pv2  += tp * tp * vol   # Σ(vol × tp²) for Bollinger variance

        # Weekly + Monthly anchored VWAPs
        st.w_cum_vol += vol
        st.w_cum_pv  += tp * vol
        st.m_cum_vol += vol
        st.m_cum_pv  += tp * vol

        vwap = st.cum_pv / st.cum_vol if st.cum_vol > 0 else tp

        # ── 2. VWAP Bollinger Bands ───────────────────────────────────────
        vwap_std, bands = self._compute_bands(vwap, st)

        # ── 3. Log-return → VolatilityEngine EWMA ─────────────────────────
        if prev_bars:
            prev_close = prev_bars[-1]["close"]
            log_ret    = float(np.log(bar["close"] / prev_close)) if prev_close > 0 else 0.0
        else:
            log_ret = 0.0

        ewma_result = self._vol_engine.update_bar(symbol, log_ret)
        ewma_vol    = ewma_result.ewma_vol
        vol_state   = ewma_result.vol_state
        band_mult   = ewma_result.band_mult

        # ── 4. True Range + ATR(14) ───────────────────────────────────────
        if prev_bars:
            pc = prev_bars[-1]["close"]
            tr = max(
                bar["high"] - bar["low"],
                abs(bar["high"] - pc),
                abs(bar["low"]  - pc),
            )
        else:
            tr = max(bar["high"] - bar["low"], 0.01)

        st._tr_buf.append(tr)

        warmup_ready = (
            len(st._tr_buf) >= ATR_WINDOW
            and len(st._tp_history) >= BAND_SIGMA_WINDOW
        )

        if not warmup_ready:
            ewma_result = self._vol_engine.update_bar(symbol, log_ret)
            bands = VWAPBands(
                vwap=tp,
                vwap_std=0.0,
                upper1=tp,
                upper2=tp,
                upper3=tp,
                lower1=tp,
                lower2=tp,
                lower3=tp,
                weekly_vwap=tp,
                monthly_vwap=tp,
            )

            result = MicroResult(
                bands=bands,
                dt=0.0,
                atr=0.0,
                vol_state=ewma_result.vol_state,
                band_mult=ewma_result.band_mult,
                ewma_vol=ewma_result.ewma_vol,
                micro_state="Neutral",
                micro_score=0.0,
                mean_rev_flag=False,
                chop_active=False,
            )
            st.last_result = result
            return result

        atr = float(np.mean(st._tr_buf))
        atr = max(atr, 0.01)

        # ── 5. Normalised VWAP distance (vol-regime scaled) ───────────────
        effective_atr = atr * band_mult
        dt = (bar["close"] - vwap) / effective_atr if effective_atr > 0 else 0.0

        # ── 6. Microstructure state machine ──────────────────────────────
        micro_state, micro_score, mean_rev_flag, chop_active = \
            self._classify_state(dt, vwap_std, vol_state, cvd, float(bar["close"]), float(bar.get("volume", 0.0)))

        if chop_active:
            logger.debug(
                "%s CHOP detected (|dt|=%.2f). Trend entries suppressed.", symbol, abs(dt)
            )
        if mean_rev_flag:
            logger.info(
                "%s MEAN-REVERSION FLAG at 3σ (dt=%.2f, cvd=%.0f). "
                "High-probability reversal setup.", symbol, dt, cvd
            )

        result = MicroResult(
            bands        = bands,
            dt           = dt,
            atr          = atr,
            vol_state    = vol_state,
            band_mult    = band_mult,
            ewma_vol     = ewma_vol,
            micro_state  = micro_state,
            micro_score  = micro_score,
            mean_rev_flag = mean_rev_flag,
            chop_active  = chop_active,
        )
        st.last_result = result   # 4.5 fix: was unreachable dead code after return
        return result

    # ── Snapshot dict (for phase23_vector / ScoringEngine) ────────────────

    def snapshot(self, symbol: str, result: MicroResult) -> Dict[str, Any]:
        """
        Flat dict compatible with the scoring engine's snapshot_df format.
        Callers merge this into the row dict before calling evaluate_universe().
        """
        b = result.bands
        return {
            "vwap":           b.vwap,
            "vwap_std":       b.vwap_std,
            "vwap_upper1":    b.upper1,
            "vwap_upper2":    b.upper2,
            "vwap_upper3":    b.upper3,
            "vwap_lower1":    b.lower1,
            "vwap_lower2":    b.lower2,
            "vwap_lower3":    b.lower3,
            "weekly_vwap":    b.weekly_vwap,
            "monthly_vwap":   b.monthly_vwap,
            "dt":             result.dt,
            "atr":            result.atr,
            "vol_state":      result.vol_state,
            "band_mult":      result.band_mult,
            "ewma_vol":       result.ewma_vol,
            "micro_state":    result.micro_state,
            "micro_score":    result.micro_score,
            "mean_rev_flag":  result.mean_rev_flag,
            "chop_active":    result.chop_active,
        }


# ── Singleton ──────────────────────────────────────────────────────────────
_vol_vwap_engine: Optional[VolVWAPEngine] = None

def get_vol_vwap_engine() -> VolVWAPEngine:
    global _vol_vwap_engine
    if _vol_vwap_engine is None:
        _vol_vwap_engine = VolVWAPEngine()
    return _vol_vwap_engine


