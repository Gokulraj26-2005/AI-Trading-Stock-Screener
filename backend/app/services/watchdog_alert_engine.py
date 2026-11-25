"""
Watchdog Alert Engine — Phase 3
=================================
Tiered, deduplicated, price-confirmed alert system for both Long and Short watchdogs.

Tiers
-----
INFO  (1 signal)   — a single weak signal; logged to recorder only (silent).
WATCH (2 signals)  — two independent signals agree; low-priority timeline event.
ACT   (3+ signals) — three or more agree AND price confirms direction;
                     high-priority timeline event that the user must see.

Design rules
------------
- A "signal" is a named boolean assertion (wall_present, obi_flip, cvd_surge, …).
- Each position carries its own SignalAccumulator so state is isolated.
- Cooldowns are per-event-type, per-position (60s default, ACT clears faster).
- Dedup: same event type doesn't re-fire while conditions remain identical.
- Price confirmation: CVD/OBI directional signals require price to have moved
  at least 0.1% in the expected direction within the last 30 seconds to reach ACT.
- MIS square-off warning fires at 15:10 IST (5 min before 15:15 auto-SQ).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from logger import logger

# ── Signal definitions ───────────────────────────────────────────────────────

# Signal names — keep these stable; they're stored in the DB
S_CVD_DUMP       = "large_persistent_sell_flow"
S_CVD_SURGE      = "large_persistent_buy_flow"
S_OBI_BEARISH    = "obi_bearish_dominant"
S_OBI_BULLISH    = "obi_bullish_dominant"
S_SELL_WALL_NEAR = "sell_wall_near_price"
S_BUY_WALL_NEAR  = "buy_wall_near_price"
S_BOOK_FLIP_SELL = "orderbook_flipped_to_sellers"
S_BOOK_FLIP_BUY  = "orderbook_flipped_to_buyers"
S_PRICE_BREAK_VW = "price_breaks_vwap_against_position"
S_RSI_EXHAUST    = "rsi_exhaustion_zone"

# Tier thresholds
_INFO_THRESHOLD  = 1
_WATCH_THRESHOLD = 2
_ACT_THRESHOLD   = 3

# Cooldowns per tier (seconds)
_COOLDOWN_INFO   = 300    # INFO: silent, 5 min
_COOLDOWN_WATCH  = 120    # WATCH: 2 min
_COOLDOWN_ACT    = 60     # ACT: 1 min (serious alerts need repetition)


@dataclass
class AlertSignalBundle:
    """Snapshot of all active signals for a single evaluate_tick call."""
    cvd_dump: bool = False
    cvd_surge: bool = False
    obi_bearish: bool = False        # deep OBI < -0.30
    obi_bullish: bool = False        # deep OBI > +0.30
    sell_wall_near: bool = False
    buy_wall_near: bool = False
    book_flip_sell: bool = False     # OBI crossed from BUYERS to SELLERS
    book_flip_buy: bool = False
    price_breaks_vwap_against: bool = False
    rsi_exhaustion: bool = False

    # Context values (for recording)
    cvd_delta: int = 0
    cvd_total: int = 0
    deep_obi: Optional[float] = None
    wall_price: Optional[float] = None
    wall_side: Optional[str] = None
    wall_strength: Optional[float] = None
    rsi_value: Optional[float] = None
    ltp: float = 0.0
    vwap_5m: Optional[float] = None


@dataclass
class _AlertState:
    """Per-position alert state tracking."""
    cooldowns: Dict[str, float] = field(default_factory=dict)
    last_price_at_signal: Dict[str, float] = field(default_factory=dict)  # for dedup
    price_history: List[Tuple[float, float]] = field(default_factory=list)  # [(ts, ltp), ...]
    obi_smooth_buf: List[float] = field(default_factory=list)   # rolling 10-tick OBI
    obi_zone: str = "NEUTRAL"         # last confirmed OBI zone (for hysteresis)
    mis_warned: bool = False



def get_liquidity_tier(symbol: str) -> int:
    """
    Phase 3: Stock liquidity tiering.
    Tier 1: Large-cap (NIFTY 50) — tight spread, thick book.
    Tier 2: Mid-cap (NIFTY NEXT 50 & MIDCAP 100) — moderate depth.
    Tier 3: Small-cap / other — thinner book, wider jumps.
    """
    sym = (symbol or "").strip().upper()
    try:
        from config.symbols import NIFTY_50_SYMBOLS, NIFTY_NEXT_50_SYMBOLS, NIFTY_MIDCAP_100_SYMBOLS
        if sym in NIFTY_50_SYMBOLS:
            return 1
        elif sym in NIFTY_NEXT_50_SYMBOLS or sym in NIFTY_MIDCAP_100_SYMBOLS:
            return 2
        else:
            return 3
    except Exception:
        return 2


def get_tier_obi_thresholds(tier: int) -> Tuple[float, float]:
    """
    Returns (fire_threshold, rearm_threshold) for OBI based on liquidity tier:
    - Tier 1: fire at ±0.15, re-arm at ±0.05 (dense book requires lower threshold)
    - Tier 2: fire at ±0.25, re-arm at ±0.10
    - Tier 3: fire at ±0.35, re-arm at ±0.15 (thin book requires higher threshold)
    """
    if tier == 1:
        return 0.15, 0.05
    elif tier == 3:
        return 0.35, 0.15
    return 0.25, 0.10


class WatchdogAlertEngine:
    """
    Stateless evaluator — each call to evaluate() receives the full signal
    bundle and the current position state dict; returns a list of alerts to fire.
    State is stored in _AlertState objects keyed by symbol.
    """

    def __init__(self) -> None:
        self._states: Dict[str, _AlertState] = {}

    def get_state(self, symbol: str) -> _AlertState:
        if symbol not in self._states:
            self._states[symbol] = _AlertState()
        return self._states[symbol]

    def remove_state(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    def smooth_obi(self, symbol: str, raw_obi: Optional[float], window: int = 10) -> Optional[float]:
        """
        Maintain a rolling average OBI buffer (Phase 1 #3).
        Returns the smoothed value once the buffer has enough samples.
        """
        if raw_obi is None or not isinstance(raw_obi, (int, float)) or isinstance(raw_obi, bool):
            return None
        st = self.get_state(symbol)
        st.obi_smooth_buf.append(float(raw_obi))
        if len(st.obi_smooth_buf) > window:
            st.obi_smooth_buf.pop(0)
        if len(st.obi_smooth_buf) < 5:  # need at least 5 ticks to smooth
            return float(raw_obi)
        return sum(st.obi_smooth_buf) / len(st.obi_smooth_buf)

    def update_price_history(self, symbol: str, ltp: float) -> None:
        if not isinstance(ltp, (int, float)) or isinstance(ltp, bool):
            return
        st = self.get_state(symbol)
        now = time.time()
        st.price_history.append((now, float(ltp)))
        # Keep last 60 seconds of price history
        cutoff = now - 60.0
        st.price_history = [(ts, px) for ts, px in st.price_history if ts >= cutoff]

    def price_change_last_n_sec(self, symbol: str, seconds: float) -> float:
        """Return % price change from N seconds ago to now. +ve = rising."""
        st = self.get_state(symbol)
        if not st.price_history:
            return 0.0
        now = time.time()
        cutoff = now - seconds
        old_entries = [(ts, px) for ts, px in st.price_history if ts <= cutoff]
        if not old_entries or not st.price_history:
            return 0.0
        old_price = old_entries[-1][1]
        now_price = st.price_history[-1][1]
        if old_price <= 0:
            return 0.0
        return (now_price - old_price) / old_price * 100.0

    def obi_zone_with_hysteresis(
        self, symbol: str, smoothed_obi: Optional[float]
    ) -> str:
        """
        OBI zone state machine with tier-calibrated hysteresis (Phase 1 #3 + Phase 3):
        - Fire SELLERS when smoothed OBI <= -fire_thresh
        - Fire BUYERS when smoothed OBI >= +fire_thresh
        - Re-arm (go NEUTRAL) only when smoothed OBI crosses back through ±rearm_thresh
        Returns the new zone name.
        """
        if smoothed_obi is None or not isinstance(smoothed_obi, (int, float)) or isinstance(smoothed_obi, bool):
            return "NEUTRAL"
        st = self.get_state(symbol)
        zone = st.obi_zone

        tier = get_liquidity_tier(symbol)
        fire_thresh, rearm_thresh = get_tier_obi_thresholds(tier)

        if zone == "SELLERS":
            if smoothed_obi >= -rearm_thresh:  # recovery above re-arm threshold
                zone = "NEUTRAL"
        elif zone == "BUYERS":
            if smoothed_obi <= rearm_thresh:
                zone = "NEUTRAL"
        else:  # NEUTRAL
            if smoothed_obi <= -fire_thresh:
                zone = "SELLERS"
            elif smoothed_obi >= fire_thresh:
                zone = "BUYERS"

        st.obi_zone = zone
        return zone

    def _in_cooldown(self, st: _AlertState, event_key: str, cooldown: float) -> bool:
        last = st.cooldowns.get(event_key, 0.0)
        return (time.time() - last) < cooldown

    def _arm_cooldown(self, st: _AlertState, event_key: str) -> None:
        st.cooldowns[event_key] = time.time()

    def evaluate(
        self,
        symbol: str,
        direction: str,   # "LONG" | "SHORT"
        bundle: AlertSignalBundle,
        entry_price: float,
        add_timeline_event_fn,   # callable(symbol, event_type, message, price, severity)
        record_alert_fn,         # callable from watchdog_data_recorder
    ) -> List[Dict[str, Any]]:
        """
        Core tiered alert evaluation.
        Returns list of fired alert dicts (for testing / logging).
        """
        st = self.get_state(symbol)
        now = time.time()
        fired: List[Dict[str, Any]] = []
        ltp = bundle.ltp

        # ── Update price history ──────────────────────────────────────────────
        if ltp > 0:
            self.update_price_history(symbol, ltp)

        # ── MIS square-off time warning (15:10 IST) ───────────────────────────
        from datetime import datetime
        from zoneinfo import ZoneInfo
        _IST = ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(_IST)
        if not st.mis_warned and now_ist.hour == 15 and now_ist.minute >= 10:
            st.mis_warned = True
            add_timeline_event_fn(
                symbol,
                "MIS_SQUAREOFF_WARNING",
                "⏰ 15:10 — Only 5 minutes to MIS auto square-off at 15:15! "
                "Close your position manually on Dhan now if you haven't already.",
                ltp,
                "danger",
            )
            fired.append({"tier": "ACT", "type": "MIS_SQUAREOFF_WARNING"})

        # ─────────────────────────────────────────────────────────────────────
        # Collect active signals this tick
        # ─────────────────────────────────────────────────────────────────────

        active_signals: List[str] = []

        # 1. CVD dump/surge
        if bundle.cvd_dump:
            active_signals.append(S_CVD_DUMP)
        if bundle.cvd_surge:
            active_signals.append(S_CVD_SURGE)

        # 2. OBI (hysteresis-gated, already smoothed by caller)
        if bundle.obi_bearish:
            active_signals.append(S_OBI_BEARISH)
        if bundle.obi_bullish:
            active_signals.append(S_OBI_BULLISH)

        # 3. Walls
        if bundle.sell_wall_near:
            active_signals.append(S_SELL_WALL_NEAR)
        if bundle.buy_wall_near:
            active_signals.append(S_BUY_WALL_NEAR)

        # 4. Book flip (only once per zone transition, tracked by caller)
        if bundle.book_flip_sell:
            active_signals.append(S_BOOK_FLIP_SELL)
        if bundle.book_flip_buy:
            active_signals.append(S_BOOK_FLIP_BUY)

        # 5. VWAP break against position
        if bundle.price_breaks_vwap_against:
            active_signals.append(S_PRICE_BREAK_VW)

        # 6. RSI exhaustion zone
        if bundle.rsi_exhaustion:
            active_signals.append(S_RSI_EXHAUST)

        # ─────────────────────────────────────────────────────────────────────
        # Determine which composite events fire, per direction
        # ─────────────────────────────────────────────────────────────────────

        def _price_confirmed_bearish() -> bool:
            """Price dropped ≥0.1% in last 30s (confirms sell/dump)."""
            return self.price_change_last_n_sec(symbol, 30.0) <= -0.10

        def _price_confirmed_bullish() -> bool:
            """Price rose ≥0.1% in last 30s (confirms buy/surge)."""
            return self.price_change_last_n_sec(symbol, 30.0) >= 0.10

        # ── Event: DUMP/SELL-PRESSURE ─────────────────────────────────────────
        # Signals that indicate heavy selling: CVD dump, OBI bearish, book flip sell
        sell_pressure_sigs = [s for s in active_signals
                              if s in (S_CVD_DUMP, S_OBI_BEARISH, S_BOOK_FLIP_SELL, S_SELL_WALL_NEAR)]
        n_sell = len(sell_pressure_sigs)

        if n_sell >= _INFO_THRESHOLD:
            event_key = "SELL_PRESSURE"
            tier, cooldown = self._compute_tier(n_sell, _price_confirmed_bearish())

            if not self._in_cooldown(st, event_key + "_" + tier, cooldown):
                self._arm_cooldown(st, event_key + "_" + tier)

                if direction == "LONG":
                    msgs = {
                        "INFO":  None,  # Silent for LONG — too noisy
                        "WATCH": f"⚡ Sell pressure building on {symbol} — CVD and order book turning negative. Watch your stop closely.",
                        "ACT":   f"🔴 Strong sell pressure confirmed on {symbol}! Heavy selling with price dropping. Be ready to exit or tighten your stop.",
                    }
                    sevs = {"WATCH": "warning", "ACT": "danger"}
                else:  # SHORT
                    msgs = {
                        "INFO":  None,
                        "WATCH": f"📉 Sell momentum building on {symbol} — good for your short. Flow and orderbook aligning.",
                        "ACT":   f"🚀 Heavy sell pressure confirmed on {symbol}! Strong breakdown flow. Hold your short tight.",
                    }
                    sevs = {"WATCH": "info", "ACT": "success"}

                fired += self._maybe_fire(
                    symbol, direction, event_key, tier,
                    sell_pressure_sigs, bundle,
                    msgs, sevs,
                    add_timeline_event_fn, record_alert_fn,
                )

        # ── Event: BUY-SURGE/INFLOW ───────────────────────────────────────────
        buy_surge_sigs = [s for s in active_signals
                         if s in (S_CVD_SURGE, S_OBI_BULLISH, S_BOOK_FLIP_BUY, S_BUY_WALL_NEAR)]
        n_buy = len(buy_surge_sigs)

        if n_buy >= _INFO_THRESHOLD:
            event_key = "BUY_SURGE"
            tier, cooldown = self._compute_tier(n_buy, _price_confirmed_bullish())

            if not self._in_cooldown(st, event_key + "_" + tier, cooldown):
                self._arm_cooldown(st, event_key + "_" + tier)

                if direction == "LONG":
                    msgs = {
                        "INFO":  None,
                        "WATCH": f"📈 Buy flow strengthening on {symbol} — orderbook and CVD positive. Position looks good.",
                        "ACT":   f"🚀 Strong buy pressure confirmed on {symbol}! Heavy inflow with price rising. Hold your long — momentum is with you.",
                    }
                    sevs = {"WATCH": "info", "ACT": "success"}
                else:  # SHORT
                    msgs = {
                        "INFO":  None,
                        "WATCH": f"⚡ Buy pressure increasing on {symbol} — could be bouncing against your short. Watch your stop.",
                        "ACT":   f"🔴 Strong buying confirmed on {symbol}! Heavy inflow with price rising. Consider covering your short.",
                    }
                    sevs = {"WATCH": "warning", "ACT": "danger"}

                fired += self._maybe_fire(
                    symbol, direction, event_key, tier,
                    buy_surge_sigs, bundle,
                    msgs, sevs,
                    add_timeline_event_fn, record_alert_fn,
                )

        # ── Event: RESISTANCE WALL (standalone) ───────────────────────────────
        if S_SELL_WALL_NEAR in active_signals and n_buy < _ACT_THRESHOLD:
            event_key = "SELL_WALL"
            if not self._in_cooldown(st, event_key, _COOLDOWN_WATCH):
                wp = bundle.wall_price
                ws = bundle.wall_strength
                self._arm_cooldown(st, event_key)
                if direction == "LONG":
                    msg = (f"⚠️ Sell wall at ₹{wp:.2f} (strength {ws:.1f}x) — "
                           f"heavy resistance ahead. Consider partial exit or tighten stop.")
                    sev = "warning"
                else:
                    msg = (f"🧱 Sell wall ceiling at ₹{wp:.2f} (strength {ws:.1f}x) — "
                           f"strong resistance protecting your short. Hold tight.")
                    sev = "info"
                add_timeline_event_fn(symbol, "SELL_WALL_STANDALONE", msg, ltp, sev)
                record_alert_fn(symbol, direction, "SELL_WALL_STANDALONE", "WATCH",
                                [S_SELL_WALL_NEAR],
                                {"wall_price": wp or 0, "strength": ws or 0, "ltp": ltp},
                                ltp)
                fired.append({"tier": "WATCH", "type": "SELL_WALL_STANDALONE"})

        # ── Event: SUPPORT WALL (standalone) ──────────────────────────────────
        if S_BUY_WALL_NEAR in active_signals and n_sell < _ACT_THRESHOLD:
            event_key = "BUY_WALL"
            if not self._in_cooldown(st, event_key, _COOLDOWN_WATCH):
                wp = bundle.wall_price
                ws = bundle.wall_strength
                self._arm_cooldown(st, event_key)
                if direction == "LONG":
                    msg = (f"🧱 Buy wall at ₹{wp:.2f} (strength {ws:.1f}x) — "
                           f"strong floor below price. Great cushion — hold your trade.")
                    sev = "info"
                else:
                    msg = (f"⚠️ Buy wall at ₹{wp:.2f} (strength {ws:.1f}x) — "
                           f"big floor blocking the drop. Be ready to take profit on your short.")
                    sev = "warning"
                add_timeline_event_fn(symbol, "BUY_WALL_STANDALONE", msg, ltp, sev)
                record_alert_fn(symbol, direction, "BUY_WALL_STANDALONE", "WATCH",
                                [S_BUY_WALL_NEAR],
                                {"wall_price": wp or 0, "strength": ws or 0, "ltp": ltp},
                                ltp)
                fired.append({"tier": "WATCH", "type": "BUY_WALL_STANDALONE"})

        # ── Event: VWAP BREAK (standalone, position-adverse) ──────────────────
        if S_PRICE_BREAK_VW in active_signals:
            event_key = "VWAP_BREAK"
            if not self._in_cooldown(st, event_key, 180.0):  # 3-min cooldown
                self._arm_cooldown(st, event_key)
                vw = bundle.vwap_5m
                if direction == "LONG":
                    msg = (f"⚠️ Price just fell below VWAP (₹{vw:.2f}) — "
                           f"structural support lost. Watch closely and tighten your stop.")
                    sev = "warning"
                else:
                    msg = (f"⚠️ Price just broke above VWAP (₹{vw:.2f}) — "
                           f"structural resistance pierced. Be alert on your short position.")
                    sev = "warning"
                add_timeline_event_fn(symbol, "VWAP_BREAK", msg, ltp, sev)
                record_alert_fn(symbol, direction, "VWAP_BREAK", "WATCH",
                                [S_PRICE_BREAK_VW],
                                {"vwap": vw or 0, "ltp": ltp},
                                ltp)
                fired.append({"tier": "WATCH", "type": "VWAP_BREAK"})

        return fired

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _compute_tier(self, n_signals: int, price_confirmed: bool) -> Tuple[str, float]:
        """Compute the alert tier and its cooldown given signal count and price confirmation."""
        if n_signals >= _ACT_THRESHOLD and price_confirmed:
            return "ACT", _COOLDOWN_ACT
        elif n_signals >= _WATCH_THRESHOLD:
            return "WATCH", _COOLDOWN_WATCH
        else:
            return "INFO", _COOLDOWN_INFO

    def _maybe_fire(
        self,
        symbol: str,
        direction: str,
        event_key: str,
        tier: str,
        signals: List[str],
        bundle: AlertSignalBundle,
        msgs: Dict[str, Optional[str]],
        sevs: Dict[str, str],
        add_timeline_event_fn,
        record_alert_fn,
    ) -> List[Dict[str, Any]]:
        """Fire a tiered alert if the tier has a non-None message."""
        msg = msgs.get(tier)
        if msg is None:
            return []  # INFO tier = silent (recorder-only)

        sev = sevs.get(tier, "info")
        ltp = bundle.ltp

        add_timeline_event_fn(symbol, f"{event_key}_{tier}", msg, ltp, sev)
        record_alert_fn(
            symbol, direction, f"{event_key}_{tier}", tier,
            signals,
            {
                "cvd_delta": bundle.cvd_delta,
                "cvd_total": bundle.cvd_total,
                "deep_obi": bundle.deep_obi or 0,
                "wall_price": bundle.wall_price or 0,
                "rsi": bundle.rsi_value or 0,
                "ltp": ltp,
            },
            ltp,
        )
        logger.info(
            "AlertEngine [%s/%s] tier=%s event=%s signals=%s",
            symbol, direction, tier, event_key, signals,
        )
        return [{"tier": tier, "type": event_key, "signals": signals}]


# ── Singleton ──────────────────────────────────────────────────────────────────
_alert_engine: Optional[WatchdogAlertEngine] = None


def get_alert_engine() -> WatchdogAlertEngine:
    global _alert_engine
    if _alert_engine is None:
        _alert_engine = WatchdogAlertEngine()
    return _alert_engine
