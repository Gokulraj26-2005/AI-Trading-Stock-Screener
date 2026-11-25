"""
Re-Breakout Compression Detection — Two-Stage Signal Engine
------------------------------------------------------------
Implements a two-stage architecture for detecting compression breakouts:

Stage 1 — Setup (compression): Watchlist a stock when compression conditions form
Stage 2 — Trigger (breakout): Enter a trade when breakout conditions are met

This enables earlier entries at structurally superior prices.

Compression Factor Calculation:
    R_short = HHV10 − LLV10   (10-bar high-low range)
    R_med   = HHV40 − LLV40   (40-bar high-low range)
    C       = R_short / R_med

    C < 0.4 with decaying sigma_t signals strong contraction.
    Require simultaneously: falling realised volatility + falling volume + tightening range.

Trigger Conditions:
    - Resumption of volume expansion (CVD positive spike)
    - Range expansion (C increasing from compressed levels)
    - VWAP microstructure transitions from Neutral to Accepted Long

Per-Symbol State Machine:
    State           Entry Condition                           Action
    ──────────────  ──────────────────────────────────────    ──────────────────────────
    Flat            Default state                             No action
    Setup Forming   C < 0.4 + decaying vol + regime not Chop  Alert trader; begin monitoring
    Setup Ready     Setup Forming conditions sustained 3+ bars Priority queue elevation; pre-arm
    Triggered       Breakout bar: CVD spike + VWAP accepted   AI agents invoked; position sizing
    Managed         Trade live position open                  Trailing stop active; MFE/MAE recorded
    Cooldown        Exit triggered (SL or TP hit)             10-bar cooldown before re-arming
"""
import logging
import numpy as np
import itertools
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple, Any
from enum import Enum

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
SHORT_RANGE_WINDOW = 10   # HHV10 - LLV10
MED_RANGE_WINDOW   = 40   # HHV40 - LLV40
COMPRESSION_THRESH = 0.4  # C < 0.4 indicates strong compression
SETUP_SUSTAIN_BARS = 3    # Bars to sustain setup before "Ready" state
COOLDOWN_BARS      = 10   # Bars to wait after exit before re-arming

# Volume expansion thresholds
VOLUME_EXPANSION_RATIO = 1.5   # Current volume / avg volume > 1.5
VOLUME_AVG_WINDOW      = 20    # Bars for volume average calculation

# CVD spike threshold
CVD_SPIKE_THRESHOLD    = 3000  # Absolute CVD change threshold for spike detection

# Range expansion threshold
RANGE_EXPANSION_RATIO  = 1.3   # Current range / avg range > 1.3
RANGE_AVG_WINDOW       = 20    # Bars for range average calculation

# Volatility decay threshold
VOL_DECAY_RATIO        = 0.8   # Current vol / prev vol < 0.8 indicates decay


class CompressionState(Enum):
    """Per-symbol compression detection state machine states."""
    FLAT = "Flat"                    # Default state - no action
    SETUP_FORMING = "Setup Forming"  # Compression detected - alert trader
    SETUP_READY = "Setup Ready"      # Setup sustained 2-3 bars - pre-arm triggers
    EARLY_IGNITION = "Early Ignition" # Predictive coil ceiling expansion (signed CVD + volume)
    TRIGGERED = "Triggered"          # Breakout confirmed - AI agents invoked
    MANAGED = "Managed"              # Trade live - trailing stop active
    COOLDOWN = "Cooldown"            # Post-exit - waiting before re-arm


# ── Result dataclasses ──────────────────────────────────────────────────────

@dataclass(slots=True)
class CompressionResult:
    """Output of compression detection for a single bar."""
    symbol:             str
    state:              CompressionState
    compression_factor: float   # C = R_short / R_med
    r_short:            float   # 10-bar range
    r_med:              float   # 40-bar range
    
    # Volatility metrics
    current_vol:        float
    prev_vol:           float
    vol_decaying:       bool
    
    # Volume metrics
    current_volume:     float
    avg_volume:         float
    volume_ratio:       float
    volume_falling:     bool
    
    # Range metrics
    current_range:      float
    avg_range:          float
    range_ratio:        float
    range_tightening:   bool
    
    # Trigger signals
    cvd_spike:          bool
    cvd_value:          float
    range_expanding:    bool
    vwap_accepted_long: bool
    
    # State tracking
    setup_bars_count:   int     # Consecutive bars in setup forming
    cooldown_remaining: int     # Bars remaining in cooldown

    # Predictive & Relative Breakout additions
    is_early_ignition:  bool = False
    cvd_delta:          float = 0.0
    cvd_zscore:         float = 0.0
    coil_rel_pos:       float = 0.0


@dataclass(slots=True)
class TriggerSignal:
    """High-priority trigger signal for AI orchestrator."""
    symbol:             str
    timestamp:          float
    entry_price:        float
    compression_factor: float
    state:              CompressionState
    confidence:         float   # 0.0 to 1.0 confidence score
    
    # Signal components
    cvd_spike:          bool
    vwap_accepted:      bool
    range_expansion:    bool
    
    # Metadata for logging
    setup_duration:     int     # Bars in setup before trigger
    is_early_ignition:  bool = False


# ── Per-symbol state ──────────────────────────────────────────────────────

class _SymbolCompressionState:
    """
    Holds all rolling accumulators and state for one symbol.
    Tracks compression patterns and state machine transitions.
    """
    __slots__ = (
        # State machine
        "state", "setup_bars_count", "cooldown_remaining", "setup_fade_grace",
        
        # Price range tracking
        "_high_buffer", "_low_buffer",
        
        # Volatility tracking
        "_vol_history",
        
        # Volume tracking
        "_volume_history",
        
        # Range tracking
        "_range_history",
        
        # CVD tracking
        "_cvd_history", "_cvd_delta_history",
        
        # Trade tracking
        "entry_price", "entry_bar",
        
        # Last signal tracking
        "last_triggered_bar",
        "last_result",
    )

    def __init__(self):
        self.state: CompressionState = CompressionState.FLAT
        self.setup_bars_count: int = 0
        self.cooldown_remaining: int = 0
        self.setup_fade_grace: int = 0
        self.last_result: Optional[CompressionResult] = None
        
        # Price range buffers (for HHV/LLV calculation)
        self._high_buffer: deque = deque(maxlen=MED_RANGE_WINDOW)
        self._low_buffer: deque = deque(maxlen=MED_RANGE_WINDOW)
        
        # Volatility history
        self._vol_history: deque = deque(maxlen=20)
        
        # Volume history
        self._volume_history: deque = deque(maxlen=VOLUME_AVG_WINDOW + 1)
        
        # Range history
        self._range_history: deque = deque(maxlen=RANGE_AVG_WINDOW + 1)
        
        # CVD history for spike detection
        self._cvd_history: deque = deque(maxlen=10)
        self._cvd_delta_history: deque = deque(maxlen=30)
        
        # Trade tracking
        self.entry_price: float = 0.0
        self.entry_bar: int = 0
        self.last_triggered_bar: int = 0

    def reset_session(self):
        """Reset state at session start (optional - preserves compression context)."""
        self.state = CompressionState.FLAT
        self.setup_bars_count = 0
        self.cooldown_remaining = 0
        self.setup_fade_grace = 0
        self._high_buffer.clear()
        self._low_buffer.clear()
        self._vol_history.clear()
        self._volume_history.clear()
        self._range_history.clear()
        self._cvd_history.clear()
        self._cvd_delta_history.clear()
        self.last_result = None

    def add_bar(self, high: float, low: float, volume: float):
        """Add a new bar's data to the rolling buffers."""
        self._high_buffer.append(high)
        self._low_buffer.append(low)
        self._volume_history.append(volume)
        self._range_history.append(high - low)

    def add_volatility(self, vol: float):
        """Add volatility sample."""
        self._vol_history.append(vol)

    def add_cvd(self, cvd: float):
        """Add CVD sample for spike detection and record signed delta."""
        if self._cvd_history:
            prev = self._cvd_history[-1]
            delta = float(cvd - prev)
            self._cvd_delta_history.append(delta)
        self._cvd_history.append(cvd)

    @property
    def r_short(self) -> float:
        """Calculate short-term range: HHV10 - LLV10."""
        n = len(self._high_buffer)
        if n < SHORT_RANGE_WINDOW:
            return float('inf')  # Not enough data
        start_idx = n - SHORT_RANGE_WINDOW
        return max(itertools.islice(self._high_buffer, start_idx, None)) - min(itertools.islice(self._low_buffer, start_idx, None))

    @property
    def r_med(self) -> float:
        """Calculate medium-term range: HHV40 - LLV40."""
        if len(self._high_buffer) < MED_RANGE_WINDOW:
            return float('inf')  # Not enough data
        return max(self._high_buffer) - min(self._low_buffer)

    @property
    def compression_factor(self) -> float:
        """Calculate compression factor C = R_short / R_med."""
        r_s = self.r_short
        r_m = self.r_med
        if r_m == 0 or r_m == float('inf'):
            return float('inf')
        return r_s / r_m

    @property
    def avg_volume(self) -> float:
        """Calculate average volume over the lookback window."""
        n = len(self._volume_history) - 1
        if n < VOLUME_AVG_WINDOW:
            return 0.0
        return sum(itertools.islice(self._volume_history, 0, n)) / n

    @property
    def avg_range(self) -> float:
        """Calculate average range over the lookback window."""
        n = len(self._range_history) - 1
        if n < RANGE_AVG_WINDOW:
            return 0.0
        return sum(itertools.islice(self._range_history, 0, n)) / n

    @property
    def current_volume(self) -> float:
        """Get current bar volume."""
        if not self._volume_history:
            return 0.0
        return self._volume_history[-1]

    @property
    def current_range(self) -> float:
        """Get current bar range."""
        if not self._range_history:
            return 0.0
        return self._range_history[-1]

    @property
    def current_volatility(self) -> float:
        """Get current volatility."""
        if not self._vol_history:
            return 0.0
        return self._vol_history[-1]

    @property
    def prev_volatility(self) -> float:
        """Get previous volatility."""
        if len(self._vol_history) < 2:
            return self.current_volatility
        return self._vol_history[-2]

    @property
    def current_cvd(self) -> float:
        """Get current CVD value."""
        if not self._cvd_history:
            return 0.0
        return self._cvd_history[-1]

    @property
    def prev_cvd(self) -> float:
        """Get previous CVD value."""
        if len(self._cvd_history) < 2:
            return self.current_cvd
        return self._cvd_history[-2]

    @property
    def cvd_delta_stats(self) -> Tuple[float, float]:
        """Calculate (mean, std) of rolling signed CVD deltas."""
        if len(self._cvd_delta_history) < 3:
            return 0.0, 0.0
        deltas = list(self._cvd_delta_history)
        return float(np.mean(deltas)), float(np.std(deltas))

    @property
    def coil_bounds(self) -> Tuple[float, float, float]:
        """
        Calculate (coil_high, coil_low, coil_threshold_top25) for R_short window.
        Top 25% threshold = coil_mid + 0.5 * (coil_high - coil_mid)
                          = coil_low + 0.75 * (coil_high - coil_low).
        """
        n = len(self._high_buffer)
        if n < SHORT_RANGE_WINDOW:
            return 0.0, 0.0, 0.0
        start_idx = n - SHORT_RANGE_WINDOW
        c_high = max(itertools.islice(self._high_buffer, start_idx, None))
        c_low = min(itertools.islice(self._low_buffer, start_idx, None))
        c_mid = (c_high + c_low) / 2.0
        top_25_thresh = c_mid + 0.5 * (c_high - c_mid)
        return float(c_high), float(c_low), float(top_25_thresh)


# ── Main Engine ───────────────────────────────────────────────────────────

class CompressionEngine:
    """
    Re-Breakout Compression Detection Engine.
    
    Two-stage architecture:
    - Stage 1 (Setup): Detect compression patterns (C < 0.4 with decaying vol/volume/range)
    - Stage 2 (Trigger): Detect breakout when conditions reverse
    
    Usage pattern:
    --------------
    Pre-session:
        engine = get_compression_engine()
    
    Per bar (on every bar close):
        result = engine.update_bar(symbol, bar_data, cvd_value, vwap_result, regime)
        
        if result.state == CompressionState.SETUP_READY:
            # Add to priority watchlist
        elif result.state == CompressionState.TRIGGERED:
            # Invoke AI agents for trade execution
    """

    def __init__(self):
        self._states: Dict[str, _SymbolCompressionState] = {}
        self._bar_counter: int = 0  # Global bar counter for tracking
        self._trigger_signals: List[TriggerSignal] = []  # Recent trigger signals
        self.running = False

    async def start(self) -> None:
        """Start the compression engine service."""
        self.running = True
        logger.info("CompressionEngine started")

    async def stop(self) -> None:
        """Stop the compression engine service."""
        self.running = False
        logger.info("CompressionEngine stopped")

    async def health_check(self) -> bool:
        """Check if compression engine is healthy."""
        return self.running

    async def get_status(self) -> Dict[str, Any]:
        """Get compression engine status."""
        return {
            'status': 'running' if self.running else 'stopped',
            'active_symbols': len(self._states),
            'trigger_signals': len(self._trigger_signals)
        }

    def _get_state(self, symbol: str) -> _SymbolCompressionState:
        """Get or create state for a symbol."""
        if symbol not in self._states:
            self._states[symbol] = _SymbolCompressionState()
        return self._states[symbol]

    def reset_session(self, symbol: str):
        """Reset state for a symbol at session start."""
        if symbol in self._states:
            self._states[symbol].reset_session()
        logger.debug("Compression session reset for: %s", symbol)

    def reset_all(self):
        """Reset all symbol states."""
        self._states.clear()
        self._trigger_signals.clear()
        logger.info("Compression engine reset - all states cleared")

    def update_bar(
        self,
        symbol: str,
        bar: Dict[str, float],
        cvd_value: float = 0.0,
        vwap_micro_state: str = "Neutral",
        regime: str = "Calm Uptrend",
    ) -> CompressionResult:
        """
        Update compression state for a symbol on bar close.
        
        Parameters
        ----------
        symbol : str
            Stock symbol
        bar : dict
            OHLCV bar data with keys: 'open', 'high', 'low', 'close', 'volume'
        cvd_value : float
            Current CVD (Cumulative Volume Delta) value
        vwap_micro_state : str
            VWAP microstructure state: "Accepted Long", "Accepted Short", 
            "Neutral", "Chop", "Fade Long", "Fade Short"
        regime : str
            Current market regime from regime engine
            
        Returns
        -------
        CompressionResult
            Current compression state and metrics
        """
        self._bar_counter += 1
        st = self._get_state(symbol)
        
        # Extract bar data
        high = bar["high"]
        low = bar["low"]
        close = bar["close"]
        volume = bar.get("volume", 0.0)
        
        # Add bar data to rolling buffers
        st.add_bar(high, low, volume)
        st.add_cvd(cvd_value)

        warmup_ready = (
            len(st._high_buffer) >= MED_RANGE_WINDOW
            and len(st._volume_history) >= (VOLUME_AVG_WINDOW + 1)
            and len(st._range_history) >= (RANGE_AVG_WINDOW + 1)
            and len(st._cvd_history) >= 2
            and len(st._vol_history) >= 2
        )

        if st.state == CompressionState.COOLDOWN:
            st.cooldown_remaining -= 1
            if st.cooldown_remaining <= 0:
                st.state = CompressionState.FLAT
                st.setup_bars_count = 0
                logger.info("%s: Cooldown complete - back to Flat", symbol)

        if not warmup_ready:
            res = CompressionResult(
                symbol=symbol,
                state=st.state,
                compression_factor=-1.0,
                r_short=-1.0,
                r_med=-1.0,
                current_vol=0.0,
                prev_vol=0.0,
                vol_decaying=False,
                current_volume=0.0,
                avg_volume=0.0,
                volume_ratio=1.0,
                volume_falling=False,
                current_range=0.0,
                avg_range=0.0,
                range_ratio=1.0,
                range_tightening=False,
                cvd_spike=False,
                cvd_value=cvd_value,
                range_expanding=False,
                vwap_accepted_long=False,
                setup_bars_count=st.setup_bars_count,
                cooldown_remaining=st.cooldown_remaining,
            )
            st.last_result = res
            return res
        
        # Calculate compression metrics
        c = st.compression_factor
        r_short = st.r_short
        r_med = st.r_med
        
        # Calculate volume metrics
        current_vol = st.current_volume
        avg_vol = st.avg_volume
        # ARCHITECTURAL NOTE (RVOL Time-of-Day Interim Boundary):
        # Intraday 5-minute bars currently use the rolling 20-bar average (st.avg_volume)
        # as the immediate operational baseline for volume_ratio.
        # Tracked Milestone Phase 4: A full Time-of-Day (ToD) historical curve can use
        # accumulated 5-minute bucket history per symbol.
        # The rolling 20-bar window provides an active, safe interim design preventing NaNs.
        volume_ratio = current_vol / avg_vol if avg_vol > 0 else 1.0
        
        # Calculate range metrics
        current_range = st.current_range
        avg_range = st.avg_range
        range_ratio = current_range / avg_range if avg_range > 0 else 1.0
        
        # Determine if conditions are met
        is_compressed = c < COMPRESSION_THRESH and c != float('inf')
        volume_falling = volume_ratio < 1.0
        range_tightening = range_ratio < 1.0
        
        # ── Relative & Predictive Metric Calculations ─────────────────────
        # 1. Signed CVD delta and symbol-relative statistics
        cvd_delta = float(cvd_value - st.prev_cvd)  # SIGNED!
        mean_cvd_delta, std_cvd_delta = st.cvd_delta_stats
        cvd_z = (cvd_delta - mean_cvd_delta) / max(std_cvd_delta, 1.0) if std_cvd_delta > 0 else 0.0

        # 2. Consolidation coil Donchian ceiling
        coil_high, coil_low, coil_top_25 = st.coil_bounds
        coil_range = max(coil_high - coil_low, 0.001)
        coil_rel_pos = float((close - coil_low) / coil_range) if coil_high > coil_low else 0.5
        
        candle_range = max(high - low, 0.001)
        close_loc_in_bar = float((close - low) / candle_range)
        close_near_high = close_loc_in_bar >= 0.65
        close_in_upper_coil = (close >= coil_top_25) if coil_high > coil_low else True
        price_pressure_ok = close_near_high and close_in_upper_coil

        # 3. Dynamic signed CVD spikes
        # For Early Ignition: Delta CVD > 0 and (>= 1.5 sigma or relative to candle volume >= 12% or absolute >= 1500)
        has_early_cvd = cvd_delta > 0 and (
            (len(st._cvd_delta_history) >= 5 and cvd_z >= 1.5)
            or (avg_vol > 0 and (cvd_delta / avg_vol) >= 0.12)
            or cvd_delta >= 1500.0
        )
        # For Confirmed Breakout: Delta CVD > 0 and (>= 2.0 sigma or relative to candle volume >= 15% or absolute >= CVD_SPIKE_THRESHOLD)
        cvd_spike = cvd_delta > 0 and (
            (len(st._cvd_delta_history) >= 5 and cvd_z >= 2.0)
            or (avg_vol > 0 and (cvd_delta / avg_vol) >= 0.15)
            or cvd_delta >= CVD_SPIKE_THRESHOLD
        )

        # 4. Volume expansion checks (enforcing user specifications: 1.30x standalone with cvd_delta >= 0 OR 1.15x with signed CVD spike)
        early_volume_ok = (cvd_delta >= 0.0 and volume_ratio >= 1.30) or (volume_ratio >= 1.15 and has_early_cvd)

        # 5. Range expansion (relative to symbol's 20-bar average)
        range_expanding = (range_ratio > 1.25) or (range_ratio > 1.18 and volume_ratio >= 1.30)
        vwap_accepted_long = vwap_micro_state == "Accepted Long"

        # 6. Volatility decay & setup conditions
        vol_decaying = bool(
            st.current_volatility <= st.prev_volatility
            or (len(st._vol_history) >= 4 and st.current_volatility < float(np.mean(list(st._vol_history)[:3])) * 1.02)
        )
        setup_conditions_met = (
            is_compressed and 
            (volume_falling or range_tightening) and 
            vol_decaying and
            regime != "High-Vol Chop"
        )

        # Adaptive setup sustain: allow 2 bars if compression is tight and vol decaying sharply
        effective_sustain_bars = 2 if (c < 0.28 and st.current_volatility <= st.prev_volatility * 0.70) else SETUP_SUSTAIN_BARS
        
        # ── State Machine Transitions ──────────────────────────────────────
        old_state = st.state
        
        if st.state == CompressionState.FLAT:
            # Check for setup formation
            if setup_conditions_met:
                st.state = CompressionState.SETUP_FORMING
                st.setup_bars_count = 1
                st.setup_fade_grace = 2
                logger.info(
                    "%s: Setup Forming detected (C=%.3f, vol_decaying=%s)",
                    symbol, c, True
                )
                
        elif st.state == CompressionState.SETUP_FORMING:
            # Check for Early Ignition push first (if price is in upper coil with expanding volume)
            if price_pressure_ok and early_volume_ok and regime != "High-Vol Chop":
                st.state = CompressionState.EARLY_IGNITION
                confidence = self._calculate_confidence(c, cvd_value, volume_ratio)
                signal = TriggerSignal(
                    symbol=symbol,
                    timestamp=bar.get("timestamp", 0.0),
                    entry_price=close,
                    compression_factor=c,
                    state=st.state,
                    confidence=max(0.70, confidence),
                    cvd_spike=has_early_cvd,
                    vwap_accepted=vwap_accepted_long,
                    range_expansion=range_expanding,
                    setup_duration=st.setup_bars_count,
                    is_early_ignition=True,
                )
                self._trigger_signals.append(signal)
                if len(self._trigger_signals) > 100:
                    self._trigger_signals = self._trigger_signals[-50:]
                logger.warning(
                    "%s: EARLY IGNITION! Entry=%.2f, C=%.3f, coil_pos=%.2f, cvd_z=%.2f, vol_ratio=%.2f",
                    symbol, close, c, coil_rel_pos, cvd_z, volume_ratio,
                )
            elif setup_conditions_met:
                st.setup_bars_count += 1
                if st.setup_bars_count >= effective_sustain_bars:
                    st.state = CompressionState.SETUP_READY
                    st.setup_fade_grace = 2
                    logger.warning(
                        "%s: Setup READY after %d bars (C=%.3f) - PRIORITY QUEUE",
                        symbol, st.setup_bars_count, c
                    )
            else:
                # Setup failed - reset
                st.state = CompressionState.FLAT
                st.setup_bars_count = 0
                
        elif st.state == CompressionState.SETUP_READY:
            # ── Confirmed Breakout trigger check ──────────────────────────────
            trigger_signals = sum([
                cvd_spike,
                vwap_accepted_long,
                range_expanding,
            ])
            if trigger_signals >= 2 and (close >= coil_high * 0.998 if coil_high > 0 else True):
                st.state = CompressionState.TRIGGERED
                st.entry_price = close
                st.entry_bar = self._bar_counter
                st.last_triggered_bar = self._bar_counter

                confidence = self._calculate_confidence(c, cvd_value, volume_ratio)
                signal = TriggerSignal(
                    symbol=symbol,
                    timestamp=bar.get("timestamp", 0.0),
                    entry_price=close,
                    compression_factor=c,
                    state=st.state,
                    confidence=confidence,
                    cvd_spike=cvd_spike,
                    vwap_accepted=vwap_accepted_long,
                    range_expansion=range_expanding,
                    setup_duration=st.setup_bars_count,
                    is_early_ignition=False,
                )
                self._trigger_signals.append(signal)

                if len(self._trigger_signals) > 100:
                    self._trigger_signals = self._trigger_signals[-50:]

                logger.critical(
                    "%s: TRIGGERED! Entry=%.2f, C=%.3f, confidence=%.2f (cvd_spike=%s vwap_accepted=%s range_expanding=%s)",
                    symbol, close, c, confidence, cvd_spike, vwap_accepted_long, range_expanding,
                )
            # ── Early Ignition check if breakout not yet fully confirmed ──────
            elif price_pressure_ok and early_volume_ok and regime != "High-Vol Chop":
                st.state = CompressionState.EARLY_IGNITION
                confidence = self._calculate_confidence(c, cvd_value, volume_ratio)
                signal = TriggerSignal(
                    symbol=symbol,
                    timestamp=bar.get("timestamp", 0.0),
                    entry_price=close,
                    compression_factor=c,
                    state=st.state,
                    confidence=max(0.72, confidence),
                    cvd_spike=has_early_cvd,
                    vwap_accepted=vwap_accepted_long,
                    range_expansion=range_expanding,
                    setup_duration=st.setup_bars_count,
                    is_early_ignition=True,
                )
                self._trigger_signals.append(signal)
                if len(self._trigger_signals) > 100:
                    self._trigger_signals = self._trigger_signals[-50:]
                logger.warning(
                    "%s: EARLY IGNITION (from Ready)! Entry=%.2f, C=%.3f, coil_pos=%.2f, vol_ratio=%.2f",
                    symbol, close, c, coil_rel_pos, volume_ratio,
                )
            elif setup_conditions_met:
                # Still compressed, no breakout yet — stay in SETUP_READY and refresh grace
                st.setup_fade_grace = 2
            else:
                # Compression conditions briefly loosened — use grace period
                st.setup_fade_grace -= 1
                if st.setup_fade_grace <= 0:
                    st.state = CompressionState.FLAT
                    st.setup_bars_count = 0
                    logger.info("%s: Setup faded (no breakout) — reset to Flat", symbol)

        elif st.state == CompressionState.EARLY_IGNITION:
            # ── Check if early ignition confirms into full breakout ───────────
            trigger_signals = sum([
                cvd_spike,
                vwap_accepted_long,
                range_expanding,
            ])
            if trigger_signals >= 2 or (range_expanding and close >= coil_high * 0.998):
                st.state = CompressionState.TRIGGERED
                st.entry_price = close
                st.entry_bar = self._bar_counter
                st.last_triggered_bar = self._bar_counter

                confidence = self._calculate_confidence(c, cvd_value, volume_ratio)
                signal = TriggerSignal(
                    symbol=symbol,
                    timestamp=bar.get("timestamp", 0.0),
                    entry_price=close,
                    compression_factor=c,
                    state=st.state,
                    confidence=confidence,
                    cvd_spike=cvd_spike,
                    vwap_accepted=vwap_accepted_long,
                    range_expansion=range_expanding,
                    setup_duration=st.setup_bars_count,
                    is_early_ignition=False,
                )
                self._trigger_signals.append(signal)
                if len(self._trigger_signals) > 100:
                    self._trigger_signals = self._trigger_signals[-50:]
                logger.critical(
                    "%s: EARLY IGNITION CONFIRMED -> TRIGGERED! Entry=%.2f", symbol, close
                )
            elif not price_pressure_ok and volume_ratio < 1.0:
                # Push faded without breakout
                if setup_conditions_met:
                    st.state = CompressionState.SETUP_READY
                    st.setup_fade_grace = 2
                else:
                    st.state = CompressionState.FLAT
                    st.setup_bars_count = 0
                    logger.info("%s: Early ignition aborted — reset to Flat", symbol)
                    
        elif st.state == CompressionState.TRIGGERED:
            # Transition to managed (trade is live)
            st.state = CompressionState.MANAGED
            
        elif st.state == CompressionState.MANAGED:
            # Stay in managed until exit (handled externally via trade journal)
            pass
            
        elif st.state == CompressionState.COOLDOWN:
            st.cooldown_remaining -= 1
            if st.cooldown_remaining <= 0:
                st.state = CompressionState.FLAT
                st.setup_bars_count = 0
                logger.info("%s: Cooldown complete - back to Flat", symbol)
        
        # Return result
        result = CompressionResult(
            symbol=symbol,
            state=st.state,
            compression_factor=c if c != float('inf') else -1.0,
            r_short=r_short if r_short != float('inf') else -1.0,
            r_med=r_med if r_med != float('inf') else -1.0,
            current_vol=st.current_volatility,
            prev_vol=st.prev_volatility,
            vol_decaying=vol_decaying,
            current_volume=current_vol,
            avg_volume=avg_vol,
            volume_ratio=volume_ratio,
            volume_falling=volume_falling,
            current_range=current_range,
            avg_range=avg_range,
            range_ratio=range_ratio,
            range_tightening=range_tightening,
            cvd_spike=cvd_spike,
            cvd_value=cvd_value,
            range_expanding=range_expanding,
            vwap_accepted_long=vwap_accepted_long,
            setup_bars_count=st.setup_bars_count,
            cooldown_remaining=st.cooldown_remaining,
            is_early_ignition=(st.state == CompressionState.EARLY_IGNITION),
            cvd_delta=cvd_delta,
            cvd_zscore=cvd_z,
            coil_rel_pos=coil_rel_pos,
        )
        
        st.last_result = result
        return result

    def add_volatility(self, symbol: str, vol: float):
        """Add volatility sample for a symbol."""
        st = self._get_state(symbol)
        st.add_volatility(vol)

    def mark_trade_exit(self, symbol: str, exit_reason: str = "SL/TP"):
        """
        Mark a trade as exited and enter cooldown.
        Called by trade journal or risk engine when position is closed.
        
        Parameters
        ----------
        symbol : str
            Stock symbol
        exit_reason : str
            Reason for exit: "SL" (stop loss), "TP" (take profit), "Manual"
        """
        st = self._get_state(symbol)
        if st.state == CompressionState.MANAGED:
            st.state = CompressionState.COOLDOWN
            st.cooldown_remaining = COOLDOWN_BARS
            logger.info(
                "%s: Trade exited (%s) - entering %d-bar cooldown",
                symbol, exit_reason, COOLDOWN_BARS
            )

    def _calculate_confidence(
        self, 
        compression_factor: float, 
        cvd_value: float, 
        volume_ratio: float
    ) -> float:
        """
        Calculate confidence score for a trigger signal (0.0 to 1.0).
        
        Factors:
               - Lower compression factor = higher confidence
        - Higher CVD spike = higher confidence
        - Higher volume expansion = higher confidence
        """
        confidence = 0.0
        
        # Compression factor contribution (lower is better, max 0.33)
        if compression_factor > 0 and compression_factor != float('inf'):
            comp_score = max(0, (COMPRESSION_THRESH - compression_factor) / COMPRESSION_THRESH)
            confidence += comp_score * 0.33
        
        # CVD contribution (higher absolute value = better, max 0.33)
        cvd_score = min(1.0, abs(cvd_value) / (CVD_SPIKE_THRESHOLD * 2))
        confidence += cvd_score * 0.33
        
        # Volume contribution (higher ratio = better, max 0.34)
        vol_score = min(1.0, (volume_ratio - 1.0) / (VOLUME_EXPANSION_RATIO - 1.0))
        confidence += max(0, vol_score) * 0.34
        
        return min(1.0, max(0.0, confidence))

    def get_setup_watchlist(self) -> List[str]:
        """
        Get list of symbols in setup states (forming, ready, or early ignition).
        
        Returns
        -------
        List[str]
            Symbols in SETUP_FORMING, SETUP_READY, or EARLY_IGNITION state
        """
        watchlist = []
        for symbol, st in self._states.items():
            if st.state in (CompressionState.SETUP_FORMING, CompressionState.SETUP_READY, CompressionState.EARLY_IGNITION):
                watchlist.append(symbol)
        return watchlist

    def get_triggered_signals(self, last_n: int = 10) -> List[TriggerSignal]:
        """
        Get recent trigger signals.
        
        Parameters
        ----------
        last_n : int
            Number of most recent signals to return
            
        Returns
        -------
        List[TriggerSignal]
            Recent trigger signals
        """
        return self._trigger_signals[-last_n:]

    def get_state(self, symbol: str) -> CompressionState:
        """Get current state for a symbol."""
        st = self._states.get(symbol)
        return st.state if st else CompressionState.FLAT

    def get_snapshot(self, symbol: str) -> Dict:
        """
        Get a snapshot of compression metrics for a symbol.
        
        Returns a dict suitable for API response or logging.
        """
        st = self._get_state(symbol)
        c = st.compression_factor
        
        return {
            "symbol": symbol,
            "state": str(st.state.value),
            "compression_factor": float(c) if c != float('inf') else None,
            "r_short": float(st.r_short) if st.r_short != float('inf') else None,
            "r_med": float(st.r_med) if st.r_med != float('inf') else None,
            "setup_bars_count": int(st.setup_bars_count),
            "cooldown_remaining": int(st.cooldown_remaining),
            "is_compressed": bool(c < COMPRESSION_THRESH and c != float('inf')),
            "avg_volume": float(st.avg_volume),
            "current_volume": float(st.current_volume),
            "volume_ratio": float(st.current_volume / st.avg_volume if st.avg_volume > 0 else 1.0),
            "vcp_confirmed": bool(
                st.state in (CompressionState.SETUP_FORMING, CompressionState.SETUP_READY, CompressionState.EARLY_IGNITION, CompressionState.TRIGGERED)
                or (c != float('inf') and c < 0.45 and (getattr(st.last_result, "range_tightening", False) if st.last_result else False))
            ),
        }


# ── Singleton ──────────────────────────────────────────────────────────────
_compression_engine: Optional[CompressionEngine] = None

def get_compression_engine() -> CompressionEngine:
    """Get the singleton CompressionEngine instance."""
    global _compression_engine
    if _compression_engine is None:
        _compression_engine = CompressionEngine()
    return _compression_engine


def reset_compression_engine():
    """Reset the singleton instance (useful for testing)."""
    global _compression_engine
    _compression_engine = None