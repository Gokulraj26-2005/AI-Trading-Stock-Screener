"""
Cumulative Volume Delta (CVD) — Order Flow Engine  (§F10)
----------------------------------------------------------
Phase 1 + 2 upgrades:
  - Banded OBI: only depth levels within ±0.5% of LTP (more signal, less noise)
  - Normalized CVD: rolling z-score over a 20-bar window for cross-stock comparison
  - Wall persistence tracking: a level must persist for ≥5s before alerting
  - Iceberg/refill detection: tracks levels that refill after trading through them
  - Notional size filter: walls must exceed a rupee value threshold
  - Absorption detection: heavy volume at a level with small price movement
  - Banded OBI exposed via get_banded_obi(token, ltp)
  - Normalized CVD exposed via get_normalized_cvd(token)

Tick-level CVD
    Volume_tick = CumulativeVolume_t − CumulativeVolume_{t-1}
    Price >= Ask  →  delta = +Volume_tick   (buyer-initiated)
    Price <= Bid  →  delta = −Volume_tick   (seller-initiated)
    CVD_t = CVD_{t-1} + delta

CVD Divergence (primary signal)
    Scan the last N bar-snapshots for:
      • Price Lower Low  (price fell to a new low)
      • CVD Higher Low   (buying absorption: sellers active but CVD rising)
    This is the institutional absorption signal — highest-probability
    mean-reversion confirmation superior to RSI or aggregate volume.

Order Book Imbalance (OBI) — mandatory execution filter
    Imbalance = (Q_bid − Q_ask) / (Q_bid + Q_ask) ∈ [−1, +1]
    High positive OBI before a long confirms passive institutional bids.
    Used as a hard gate: Bayesian engine will NOT deploy capital unless
    obi_gate passes (obi >= OBI_FILTER_THRESHOLD when going long).

Zero-GC hot path
    All state stored in pre-allocated deques.  No dict/list creation
    per tick — only scalar arithmetic and in-place deque appends.
"""
import asyncio
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
DIVERGENCE_WINDOW    = 20    # bars to look back for CVD/price divergence
OBI_FILTER_THRESHOLD = 0.40  # minimum OBI for long trade authorization


# ── Deep Depth Dataclasses ────────────────────────────────────────────────

@dataclass(slots=True)
class WallLevel:
    """An institutional order wall detected in the deep order book."""
    price: float
    quantity: int
    depth_index: int          # 0-based position in the book (0 = best)
    side: str                 # "bid" or "ask"
    strength_ratio: float     # qty / median_level_qty — higher = stronger wall
    num_orders: Optional[int] = None
    is_absorption: bool = False


@dataclass(slots=True)
class ImpactCostResult:
    """Result of slippage/impact cost estimation for a hypothetical order."""
    order_qty: int
    side: str                 # "buy" or "sell"
    vwap_fill_price: float    # Weighted average fill price across book levels
    market_price: float       # Current best bid/ask
    slippage_bps: float       # (vwap_fill - market_price) / market_price * 10000
    levels_consumed: int      # How many depth levels the order would sweep
    fully_fillable: bool      # True if the book has enough liquidity


# ── Result dataclass ──────────────────────────────────────────────────────

@dataclass(slots=True)
class CVDResult:
    """Typed output from process_tick() — avoid caller-side dict key misses."""
    tick_volume:       int     # Volume_t − Volume_{t-1}
    delta:             int     # signed tick delta (+buy / −sell)
    cvd:               int     # running cumulative volume delta
    obi:               float   # Order Book Imbalance ∈ [−1, +1] (5-level)

    # Divergence flags (updated once per bar close via snapshot_bar())
    bullish_divergence: bool   # price lower-low + CVD higher-low (absorption)
    bearish_divergence: bool   # price higher-high + CVD lower-high (distribution)

    # Execution gate — checked by Bayesian engine before capital deployment
    obi_gate_long:     bool    # OBI >= OBI_FILTER_THRESHOLD  (long safe)
    obi_gate_short:    bool    # OBI <= −OBI_FILTER_THRESHOLD (short safe)

    # ── Deep Depth extensions (populated when 20/200-level data available) ──
    deep_obi:               Optional[float] = None    # OBI across all available depth levels
    deep_obi_gate_long:     Optional[bool]  = None
    deep_obi_gate_short:    Optional[bool]  = None
    institutional_walls:    Optional[List[WallLevel]] = None
    estimated_impact_cost_bps: Optional[float] = None  # For a standard lot


# ── Wall persistence / iceberg tracking ─────────────────────────────────────

@dataclass(slots=True)
class _WallSnapshot:
    """Tracks a known wall over time for persistence and refill detection."""
    price: float
    side: str
    first_seen_ts: float
    last_seen_ts: float
    qty_history: List[int] = field(default_factory=list)   # qty at each observation
    refill_count: int = 0   # how many times qty recovered after dropping
    last_qty: int = 0


# ── Per-Symbol State ──────────────────────────────────────────────────────

class _SymbolCVDState:
    """
    All mutable state for a single symbol.  Stored as plain scalars and
    deques — no dict lookup per tick inside process_tick().
    """
    __slots__ = (
        "prev_volume", "running_cvd", "last_obi",
        "last_bullish_div", "last_bearish_div",
        "_price_history", "_cvd_history",
        "_cvd_bar_history",
    )

    def __init__(self):
        self.prev_volume:      int   = 0
        self.running_cvd:      int   = 0
        self.last_obi:         float = 0.0
        self.last_bullish_div: bool  = False
        self.last_bearish_div: bool  = False
        # Rolling bar snapshots for divergence detection
        self._price_history: deque = deque(maxlen=DIVERGENCE_WINDOW)
        self._cvd_history:   deque = deque(maxlen=DIVERGENCE_WINDOW)
        # Rolling CVD bar history for z-score normalization (20 bars)
        self._cvd_bar_history: deque = deque(maxlen=20)

    def snapshot_bar(self, close_price: float):
        """
        Call once per completed bar (e.g. 5-min close) to push price/CVD
        into the rolling window and recompute divergence flags.
        Also records running_cvd into the normalization history for z-score.
        """
        self._price_history.append(close_price)
        self._cvd_history.append(self.running_cvd)
        self._cvd_bar_history.append(self.running_cvd)  # for normalized CVD z-score
        self._detect_divergence()

    def _detect_divergence(self):
        """
        Scans the rolling window for absorption/distribution divergence.
        Uses swing comparison: compares the recent leg (last 2 bars) against
        the prior lookback window (earlier bars) to avoid single-bar noise.
        """
        self.last_bullish_div = False
        self.last_bearish_div = False

        n = len(self._price_history)
        if n < 4:
            return

        # OPT-01 & DIV-01: Index deques directly without copying to lists.
        # Recent leg (last 2 bars) vs prior leg (0 .. n-3)
        recent_min_price = min(self._price_history[n - 2], self._price_history[n - 1])
        prior_min_price = min(self._price_history[i] for i in range(n - 2))

        recent_min_cvd = min(self._cvd_history[n - 2], self._cvd_history[n - 1])
        prior_min_cvd = min(self._cvd_history[i] for i in range(n - 2))

        # Bullish divergence (absorption): recent price made a lower low,
        # but recent cumulative volume delta remained higher than the prior low.
        if recent_min_price < prior_min_price and recent_min_cvd > prior_min_cvd:
            self.last_bullish_div = True

        recent_max_price = max(self._price_history[n - 2], self._price_history[n - 1])
        prior_max_price = max(self._price_history[i] for i in range(n - 2))

        recent_max_cvd = max(self._cvd_history[n - 2], self._cvd_history[n - 1])
        prior_max_cvd = max(self._cvd_history[i] for i in range(n - 2))

        # Bearish divergence (distribution): recent price made a higher high,
        # but recent cumulative volume delta was lower than the prior high.
        if recent_max_price > prior_max_price and recent_max_cvd < prior_max_cvd:
            self.last_bearish_div = True


# ── Main Engine ───────────────────────────────────────────────────────────

class CVDEngine:
    """
    Cumulative Volume Delta Engine — F10 Order Flow Signal.

    Hot-path call (every tick):
        result = engine.process_tick(token, price, volume, bid, ask, bid_qty, ask_qty)

    Bar-close call (every completed 5-min bar):
        engine.snapshot_bar(token, close_price)

    Execution filter:
        result.obi_gate_long   → True if OBI confirms institutional bid support
        result.bullish_divergence → True if absorption signal is active
    """

    def __init__(self):
        self._states: Dict[int, _SymbolCVDState] = {}
        self.running = False
        # All CVD processing is done inline by DhanWebSocket binary parser.
        # No background ZMQ subscriber is needed here.
        self.task: Optional[asyncio.Task] = None

        # Deep depth state per symbol
        self._deep_depth: Dict[int, Dict[str, Any]] = {}  # token -> latest deep book

    async def start(self) -> None:
        """Start the CVD engine service."""
        self.running = True
        self.task = asyncio.create_task(self.run())
        logger.info("CVDEngine started")

    async def stop(self) -> None:
        """Stop the CVD engine service."""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("CVDEngine stopped")

    async def run(self) -> None:
        logger.info("CVDEngine background task disabled (in-line processing active)")
        while self.running:
            await asyncio.sleep(1.0)

    async def health_check(self) -> bool:
        """Check if CVD engine is healthy."""
        return self.running and self.task is not None and not self.task.done()

    async def get_status(self) -> Dict[str, Any]:
        """Get CVD engine status."""
        return {
            'status': 'running' if self.running else 'stopped',
            'active_symbols': len(self._states),
            'task_active': self.task is not None and not self.task.done()
        }

    def _get_state(self, token: int) -> _SymbolCVDState:
        if token not in self._states:
            self._states[token] = _SymbolCVDState()
        return self._states[token]


    # ── Tick-level hot path ────────────────────────────────────────────────

    def process_tick(
        self,
        instrument_token: int,
        last_price:       float,
        current_volume:   int,
        best_bid:         float,
        best_ask:         float,
        total_bid_qty:    int,
        total_ask_qty:    int,
    ) -> CVDResult:
        """
        Process a new WebSocket tick (called at ~1–10 Hz per symbol in full-mode).

        All arithmetic is on scalars — no list/dict allocation per call.
        Divergence flags reflect the most recent bar-close snapshot.
        """
        st = self._get_state(instrument_token)

        # ── Tick volume isolation ─────────────────────────────────────────
        if st.prev_volume == 0:
            # First tick of the session — cannot compute delta yet
            st.prev_volume = current_volume
            tick_volume = 0
            delta       = 0
        else:
            # Day-rollover guard: exchange resets cumulative volume to 0
            if current_volume < st.prev_volume:
                logger.debug(
                    "Volume rollover for token %d (%d < %d). Resetting CVD.",
                    instrument_token, current_volume, st.prev_volume
                )
                st.prev_volume = current_volume
                st.running_cvd = 0
                tick_volume = 0
                delta = 0
                return CVDResult(
                    tick_volume       = tick_volume,
                    delta             = delta,
                    cvd               = st.running_cvd,
                    obi               = 0.0,
                    bullish_divergence = st.last_bullish_div,
                    bearish_divergence = st.last_bearish_div,
                    obi_gate_long     = False,
                    obi_gate_short    = False,
                )

            tick_volume = max(0, current_volume - st.prev_volume)

            # ── Aggressor side classification  ────────────────────────────
            # Spec: Price >= Ask  → buyer. Price <= Bid → seller.
            # Guard: skip classification entirely when quotes are stale/zero
            # (a zero bid/ask silently biases CVD if the chained comparison is used).
            if best_bid <= 0 or best_ask <= 0:
                delta = 0                  # stale / unavailable quote — stay neutral
            elif last_price >= best_ask:
                delta = tick_volume        # buyer-initiated
            elif last_price <= best_bid:
                delta = -tick_volume       # seller-initiated
            else:
                delta = 0                  # inside spread — mid-market, neutral

            st.running_cvd += delta
            st.prev_volume  = current_volume

        # ── Order Book Imbalance ──────────────────────────────────────────
        total_depth = total_bid_qty + total_ask_qty
        obi = (total_bid_qty - total_ask_qty) / total_depth if total_depth > 0 else 0.0
        st.last_obi = obi

        # ── OBI execution gate ────────────────────────────────────────────
        obi_gate_long  = obi >= OBI_FILTER_THRESHOLD
        obi_gate_short = obi <= -OBI_FILTER_THRESHOLD

        return CVDResult(
            tick_volume       = tick_volume,
            delta             = delta,
            cvd               = st.running_cvd,
            obi               = obi,
            bullish_divergence = st.last_bullish_div,
            bearish_divergence = st.last_bearish_div,
            obi_gate_long     = obi_gate_long,
            obi_gate_short    = obi_gate_short,
        )

    # ── Bar-close snapshot ─────────────────────────────────────────────────

    def snapshot_bar(self, instrument_token: int, close_price: float):
        """
        Push a completed bar close into the divergence detection window.
        Call once per bar close (every 5 minutes from the bar engine).
        Updates bullish/bearish divergence flags for the next tick cycle.
        """
        st = self._get_state(instrument_token)
        st.snapshot_bar(close_price)

        if st.last_bullish_div:
            logger.info(
                "CVD BULLISH DIVERGENCE detected on token %d  "
                "(price lower-low + CVD higher-low = absorption)",
                instrument_token
            )
        if st.last_bearish_div:
            logger.info(
                "CVD BEARISH DIVERGENCE detected on token %d  "
                "(price higher-high + CVD lower-high = distribution)",
                instrument_token
            )

    # ── Deep Depth Processing (20/200-level) ──────────────────────────────

    def process_deep_tick(
        self,
        instrument_token: int,
        bid_levels: List[Tuple[float, int]],  # [(price, qty), ...] best→worst
        ask_levels: List[Tuple[float, int]],  # [(price, qty), ...] best→worst
    ) -> Tuple[float, List[WallLevel]]:
        """
        Process deep order book data (20 or 200 levels).

        Computes:
          - deep_obi: OBI aggregated over all available depth levels
          - institutional walls: levels with anomalously high quantity

        Returns:
            (deep_obi, walls)
        """
        # Store for later queries
        self._deep_depth[instrument_token] = {
            "bids": bid_levels,
            "asks": ask_levels,
        }

        # ── Deep OBI ──────────────────────────────────────────────────────
        total_bid = sum(qty for _, qty in bid_levels)
        total_ask = sum(qty for _, qty in ask_levels)
        denom = total_bid + total_ask
        deep_obi = (total_bid - total_ask) / denom if denom > 0 else 0.0

        # ── Institutional Wall Detection ──────────────────────────────────
        walls = self.institutional_wall_detector(instrument_token, bid_levels, ask_levels)

        return deep_obi, walls

    # Alias for dhan_adapter compatibility
    process_deep_depth = process_deep_tick

    def reset_session(self, token: Optional[int] = None) -> None:
        """
        Reset CVD state at market session open (09:15 IST).
        If token is provided, resets only that token.
        If token is None, resets all active symbols.
        Clears both the running CVD/volume accumulators AND the divergence
        detection deques so prior-day signals cannot bleed into the new session.
        """
        if token is not None:
            if token in self._states:
                st = self._states[token]
                st.running_cvd = 0
                st.prev_volume = 0
                st.last_bullish_div = False
                st.last_bearish_div = False
                st._price_history.clear()  # prevent prior-day divergence bleed
                st._cvd_history.clear()    # prevent prior-day divergence bleed
        else:
            for st in self._states.values():
                st.running_cvd = 0
                st.prev_volume = 0
                st.last_bullish_div = False
                st.last_bearish_div = False
                st._price_history.clear()  # prevent prior-day divergence bleed
                st._cvd_history.clear()    # prevent prior-day divergence bleed
            logger.info("CVDEngine: Full session reset complete (%d tokens cleared).", len(self._states))

    def institutional_wall_detector(
        self,
        instrument_token: int,
        bid_levels: List[Any],
        ask_levels: List[Any],
        wall_threshold_multiplier: float = 5.0,
        max_order_count: int = 5,
        notional_threshold_rupees: float = 2_500_000.0,  # ₹25L default
        ltp: float = 0.0,
    ) -> List[WallLevel]:
        """
        Detect institutional order walls in the deep order book.

        Phase 2 improvements:
        - Notional size filter: qty*price must exceed notional_threshold_rupees
        - Average order size filter: prefers few large orders over many small ones
        - Persistence tracking: walls are validated against prior snapshots
        - Iceberg/refill detection: levels that refill after trading through

        A "wall" is a single level with:
          - quantity >= wall_threshold_multiplier × median level quantity
          - notional value (qty × price) >= notional_threshold_rupees
          - concentrated (avg order size large) if order count is available

        Args:
            instrument_token: Symbol token
            bid_levels: List of (price, qty) or (price, qty, num_orders)
            ask_levels: List of (price, qty) or (price, qty, num_orders)
            wall_threshold_multiplier: How many x the median qty to flag as a wall
            max_order_count: Max orders for concentrated institutional classification
            notional_threshold_rupees: Minimum ₹ notional size (qty × price) for a wall
            ltp: Current last traded price (for notional and banded OBI calculation)

        Returns:
            List of WallLevel objects, sorted by strength_ratio descending.
        """
        walls: List[WallLevel] = []
        now_ts = time.time()

        # Retrieve or create persistence tracking state
        if not hasattr(self, "_wall_snapshots"):
            self._wall_snapshots: Dict[int, Dict[str, _WallSnapshot]] = {}
        token_walls = self._wall_snapshots.setdefault(instrument_token, {})
        PERSISTENCE_MIN_SEC = 5.0   # wall must exist for at least 5s
        SNAPSHOT_EXPIRE_SEC = 30.0  # forget walls not seen for 30s

        # Expire old wall snapshots
        expired_keys = [k for k, ws in token_walls.items()
                        if now_ts - ws.last_seen_ts > SNAPSHOT_EXPIRE_SEC]
        for k in expired_keys:
            del token_walls[k]

        for side, levels in [("bid", bid_levels), ("ask", ask_levels)]:
            if len(levels) < 3:
                continue

            quantities = []
            for item in levels:
                qty = item[1] if len(item) >= 2 else 0
                if qty > 0:
                    quantities.append(qty)

            if not quantities:
                continue

            median_qty = statistics.median(quantities)
            if median_qty <= 0:
                continue

            threshold = median_qty * wall_threshold_multiplier

            for idx, item in enumerate(levels):
                price = float(item[0]) if len(item) >= 1 else 0.0
                qty = int(item[1]) if len(item) >= 2 else 0
                num_orders = int(item[2]) if len(item) >= 3 and item[2] is not None else None

                if qty < threshold or price <= 0:
                    continue

                # ── Notional size filter (Phase 2) ────────────────────────────
                effective_price = price if price > 0 else (ltp if ltp > 0 else 1.0)
                notional = qty * effective_price
                if notional_threshold_rupees > 0 and notional < notional_threshold_rupees:
                    # Too small in ₹ terms — skip (likely retail noise)
                    continue

                # ── Average order size filter (Phase 2) ───────────────────────
                # Many small orders = retail cluster. Few large orders = institutional.
                if num_orders is not None and num_orders > 0:
                    avg_order_size = qty / num_orders
                    avg_session_qty = median_qty  # proxy for typical order size
                    # If average order size is tiny compared to median AND num_orders is large
                    if num_orders > max_order_count and avg_order_size < avg_session_qty * 0.5:
                        continue  # retail cluster, skip

                strength = round(qty / median_qty, 2)

                # ── Persistence tracking (Phase 2) ────────────────────────────
                wall_key = f"{side}_{price:.2f}"
                if wall_key in token_walls:
                    ws = token_walls[wall_key]
                    # Track refill: if qty was lower last time, this level refilled
                    if ws.last_qty > 0 and qty > ws.last_qty * 1.5 and ws.qty_history:
                        prev_min = min(ws.qty_history[-3:]) if len(ws.qty_history) >= 3 else ws.last_qty
                        if qty > prev_min * 1.3:
                            ws.refill_count += 1
                    ws.last_seen_ts = now_ts
                    ws.last_qty = qty
                    ws.qty_history.append(qty)
                    if len(ws.qty_history) > 20:
                        ws.qty_history.pop(0)
                else:
                    token_walls[wall_key] = _WallSnapshot(
                        price=price, side=side,
                        first_seen_ts=now_ts, last_seen_ts=now_ts,
                        last_qty=qty, qty_history=[qty],
                    )
                    continue  # Skip on first observation (needs persistence)

                ws = token_walls[wall_key]
                persistence_sec = now_ts - ws.first_seen_ts
                # Only report walls that have persisted for the minimum time
                if persistence_sec < PERSISTENCE_MIN_SEC:
                    continue

                is_iceberg = ws.refill_count >= 5  # 5+ refills = iceberg order
                effective_strength = strength + (ws.refill_count * 0.5)  # boost score for icebergs

                walls.append(WallLevel(
                    price=price,
                    quantity=qty,
                    depth_index=idx,
                    side=side,
                    strength_ratio=round(effective_strength, 2),
                    num_orders=num_orders,
                    is_absorption=is_iceberg,  # re-use flag for iceberg
                ))

        # Sort by strength descending
        walls.sort(key=lambda w: w.strength_ratio, reverse=True)
        return walls

    def impact_cost_estimator(
        self,
        instrument_token: int,
        order_qty: int,
        side: str = "buy",
    ) -> Optional[ImpactCostResult]:
        """
        Estimate the execution slippage for a hypothetical market order.

        Walks the ask book (for buy) or bid book (for sell) to compute
        the weighted average fill price and resulting slippage in basis points.

        Args:
            instrument_token: Symbol token
            order_qty: Number of shares in the hypothetical order
            side: "buy" (walks asks) or "sell" (walks bids)

        Returns:
            ImpactCostResult or None if no deep depth data is available.
        """
        depth = self._deep_depth.get(instrument_token)
        if not depth:
            return None

        levels = depth["asks"] if side == "buy" else depth["bids"]
        if not levels:
            return None

        market_price = levels[0][0]  # Best ask (buy) or best bid (sell)
        remaining_qty = order_qty
        total_cost = 0.0
        levels_consumed = 0

        for price, qty in levels:
            if remaining_qty <= 0:
                break
            fill = min(remaining_qty, qty)
            total_cost += fill * price
            remaining_qty -= fill
            levels_consumed += 1

        filled_qty = order_qty - remaining_qty
        fully_fillable = remaining_qty == 0

        if filled_qty <= 0:
            return ImpactCostResult(
                order_qty=order_qty,
                side=side,
                vwap_fill_price=market_price,
                market_price=market_price,
                slippage_bps=0.0,
                levels_consumed=0,
                fully_fillable=False,
            )

        vwap_fill = total_cost / filled_qty
        if side == "buy":
            slippage_bps = (vwap_fill - market_price) / market_price * 10000
        else:
            slippage_bps = (market_price - vwap_fill) / market_price * 10000

        return ImpactCostResult(
            order_qty=order_qty,
            side=side,
            vwap_fill_price=round(vwap_fill, 2),
            market_price=market_price,
            slippage_bps=round(slippage_bps, 2),
            levels_consumed=levels_consumed,
            fully_fillable=fully_fillable,
        )

    def get_deep_obi(self, instrument_token: int) -> Optional[float]:
        """Get the latest deep OBI (20/200-level) for a token."""
        depth = self._deep_depth.get(instrument_token)
        if not depth:
            return None
        bids = depth.get("bids", [])
        asks = depth.get("asks", [])
        total_bid = sum(qty for _, qty in bids)
        total_ask = sum(qty for _, qty in asks)
        denom = total_bid + total_ask
        return (total_bid - total_ask) / denom if denom > 0 else 0.0

    def get_banded_obi(self, instrument_token: int, ltp: float, band_pct: float = 0.005) -> Optional[float]:
        """
        Banded OBI (Phase 2): only use depth levels within ±band_pct of LTP.
        Default band is ±0.5% — much less susceptible to far-away spoofed walls.
        Returns None if no depth data or ltp <= 0.
        """
        if ltp <= 0:
            return None
        depth = self._deep_depth.get(instrument_token)
        if not depth:
            return None
        bids = depth.get("bids", [])
        asks = depth.get("asks", [])
        lower = ltp * (1.0 - band_pct)
        upper = ltp * (1.0 + band_pct)
        banded_bid = sum(qty for px, qty in bids if lower <= px <= upper)
        banded_ask = sum(qty for px, qty in asks if lower <= px <= upper)
        denom = banded_bid + banded_ask
        if denom == 0:
            return None
        return (banded_bid - banded_ask) / denom

    def get_normalized_cvd(self, instrument_token: int) -> Optional[float]:
        """
        Normalized CVD z-score (Phase 2).
        Returns the current CVD as a standard-deviation multiple of the rolling
        20-bar CVD history, making it comparable across stocks and sessions.
        Returns None if fewer than 5 bar samples are available.
        """
        st = self._states.get(instrument_token)
        if not st or len(st._cvd_bar_history) < 5:
            return None
        history = list(st._cvd_bar_history)
        mean_cvd = statistics.mean(history)
        try:
            std_cvd = statistics.stdev(history)
        except statistics.StatisticsError:
            return None
        if std_cvd <= 0:
            return 0.0
        return round((st.running_cvd - mean_cvd) / std_cvd, 3)

    def get_absorption_signal(self, instrument_token: int) -> bool:
        """
        Absorption detection (Phase 2).
        Returns True if heavy aggressive volume is being absorbed at a level
        with little net price movement — indicates strong passive institutional
        order sitting in the book absorbing market orders.
        Proxied by: bullish_divergence flag from the CVD state machine.
        """
        st = self._states.get(instrument_token)
        return st.last_bullish_div if st else False

    # ── Diagnostics ───────────────────────────────────────────────────────

    def get_cvd(self, instrument_token: int) -> int:
        """Current running CVD for a token (read-only)."""
        st = self._states.get(instrument_token)
        return st.running_cvd if st else 0

    def get_obi(self, instrument_token: int) -> float:
        """Current 5-level OBI for a token (read-only)."""
        st = self._states.get(instrument_token)
        return st.last_obi if st else 0.0

    def is_absorbing(self, instrument_token: int) -> bool:
        """True if the latest bar snapshot shows bullish CVD divergence."""
        st = self._states.get(instrument_token)
        return st.last_bullish_div if st else False

    def get_deep_depth_summary(
        self,
        instrument_token: int,
        ltp: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        """Get a summary of the deep depth state for telemetry/frontend.
        Phase 2: includes banded_obi, normalized_cvd, and notional-filtered walls.
        """
        depth = self._deep_depth.get(instrument_token)
        if not depth:
            return None
        bids = depth.get("bids", [])
        asks = depth.get("asks", [])
        walls = self.institutional_wall_detector(instrument_token, bids, asks, ltp=ltp)
        deep_obi = self.get_deep_obi(instrument_token)
        banded_obi = self.get_banded_obi(instrument_token, ltp) if ltp > 0 else None
        norm_cvd = self.get_normalized_cvd(instrument_token)
        return {
            "deep_obi": deep_obi,
            "banded_obi": banded_obi,
            "normalized_cvd": norm_cvd,
            "bid_levels": len(bids),
            "ask_levels": len(asks),
            "total_bid_qty": sum(qty for _, qty in bids),
            "total_ask_qty": sum(qty for _, qty in asks),
            "walls": [
                {
                    "price": w.price,
                    "qty": w.quantity,
                    "side": w.side,
                    "strength": w.strength_ratio,
                    "is_iceberg": w.is_absorption,
                    "notional": round(w.price * w.quantity, 0),
                }
                for w in walls[:10]  # Top 10 walls
            ],
        }


# ── Singleton ──────────────────────────────────────────────────────────────
_cvd_engine: Optional[CVDEngine] = None

def get_cvd_engine() -> CVDEngine:
    global _cvd_engine
    if _cvd_engine is None:
        _cvd_engine = CVDEngine()
    return _cvd_engine

