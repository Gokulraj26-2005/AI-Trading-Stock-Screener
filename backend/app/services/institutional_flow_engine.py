"""
Universal Institutional Flow Engine (IFP) for VIA-Agent.
---------------------------------------------------------
Computes a robust, normalized Institutional Flow Profile score (-100 to +100)
for any equity across the 640+ stock universe, supporting both F&O and Non-F&O stocks.

Core Design Principles:
1. Universal Non-F&O Normalization: Missing F&O ≠ zero activity. Missing data is
   handled by dynamically rescaling weights across available factors without penalty.
2. Separation of Score and Confidence:
   - Score ∈ [-100, +100]: Direction and magnitude of institutional positioning.
   - Confidence ∈ [0, 100%]: Data coverage, quality, and factor consensus agreement.
3. Factor Engines:
   - Factor 1: Volume Participation (Median 20D + MAD robust z-score)
   - Factor 2: Aggressive Flow / CVD (Directional execution delta & divergence)
   - Factor 3: Large Trade Activity (Normalized per stock 95th/99th percentile bursts)
   - Factor 4: Order-Book / Depth Pressure & Absorption (Live OBI + S/R wick absorption)
   - Factor 5: Delivery / Position Building (Conditional on delivery data)
   - Factor 6: F&O / Derivatives Positioning (Conditional on F&O eligibility & OI)
   - Factor 7: Price Acceptance / AVWAP Benchmark (Monthly & Weekly AVWAP holding)
4. Multi-Day Flow State:
   - 5D trend, 20D trend, and 10D directional persistence.
5. Stealth Phase & Conflict Detection:
   - Stealth Accumulation / Stealth Distribution.
   - Divergence between Technical Breakout and Institutional Distribution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import statistics
import numpy as np
import time
from typing import Any, Dict, List, Optional, Tuple

from logger import logger

try:
    from app.app_config import QUANT_CONFIG
except ImportError:
    from app_config import QUANT_CONFIG

# ── Base Weights Configuration ────────────────────────────────────────────────
# Total possible weight = 100
BASE_WEIGHTS: Dict[str, float] = {
    "volume": 15.0,
    "cvd": 20.0,
    "large_trades": 15.0,
    "depth": 10.0,
    "delivery": 15.0,        # Conditional (Cash equities with delivery data)
    "fno_oi": 15.0,          # Conditional (Only ~200 F&O eligible stocks)
    "price_acceptance": 10.0,
}


@dataclass(slots=True)
class MarketSnapshot:
    """Normalized multi-source market data structure."""
    symbol: str
    price: float
    volume: float
    adv_20: float
    volume_median_20: float
    volumes_20: List[float]
    daily_closes_20: List[float]
    daily_highs_20: List[float]
    daily_lows_20: List[float]
    daily_opens_20: List[float]
    # Microstructure / Live CVD
    live_cvd: Optional[float] = None
    live_cvd_slope: Optional[float] = None
    live_obi: Optional[float] = None
    live_deep_obi: Optional[float] = None
    live_is_absorbing: Optional[bool] = None
    # Delivery (optional)
    delivery_qty: Optional[float] = None
    delivery_pct: Optional[float] = None
    delivery_history: Optional[List[float]] = None
    # F&O / Derivatives (optional)
    is_fno_eligible: bool = False
    futures_oi: Optional[float] = None
    futures_oi_change_pct: Optional[float] = None
    # Technical Anchors
    avwap_month: Optional[float] = None
    avwap_week: Optional[float] = None
    ema_50: Optional[float] = None
    support_level: Optional[float] = None
    resistance_level: Optional[float] = None
    weekly_trend: str = "NEUTRAL"
    technical_bias: Optional[str] = None
    # Options Chain & Institutional Disclosures (optional)
    pcr: Optional[float] = None
    max_pain: Optional[float] = None
    bulk_deals_net_cr: Optional[float] = None


@dataclass(slots=True)
class FactorResult:
    score: Optional[int]  # -100 to +100, or None if unavailable
    available: bool
    weight: float
    detail: str


@dataclass(slots=True)
class InstitutionalFlowResult:
    """Final unified institutional flow evaluation."""
    score: int  # -100 to +100
    classification: str  # STRONG_ACCUMULATION | ACCUMULATION | NEUTRAL | DISTRIBUTION | STRONG_DISTRIBUTION
    confidence: int  # 0 to 100%
    coverage: int  # 0 to 100% (e.g. 100% for F&O, 85% for Cash, 70% if delivery missing)
    trend: Dict[str, Any]  # 5d, 20d, persistence_10d
    components: Dict[str, Optional[int]]  # factor scores or None
    signals: List[str]
    conflicts: List[str]
    stealth_phase: Optional[str]  # STEALTH_ACCUMULATION | STEALTH_DISTRIBUTION | None
    summary: str  # Comprehensive narrative

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class InstitutionalFlowEngine:
    """Core mathematical and heuristic engine for institutional order flow profiling."""

    def __init__(self):
        self._fno_universe_cache: Optional[set] = None

    def is_fno_symbol(self, symbol: str) -> bool:
        """Determines if a symbol is traded in the NSE F&O (Derivatives) segment."""
        if self._fno_universe_cache is None:
            self._load_fno_universe()
        return symbol.strip().upper() in (self._fno_universe_cache or set())

    def _load_fno_universe(self) -> None:
        """Loads F&O eligible stocks from dhan_instruments or fallback list."""
        try:
            from services.dhan_instrument_resolver import get_instrument_resolver
            resolver = get_instrument_resolver()
            resolver.load()
            
            # Find all symbols that have FUTSTK instruments
            import pandas as pd
            from pathlib import Path
            csv_path = Path(__file__).resolve().parent.parent / "data" / "dhan_instruments.csv"
            if csv_path.exists():
                df = pd.read_csv(csv_path, low_memory=False)
                fut_df = df[(df['SEM_EXM_EXCH_ID'] == 'NSE') & (df['SEM_INSTRUMENT_NAME'] == 'FUTSTK')]
                # Map SM_SYMBOL_NAME or SEM_TRADING_SYMBOL roots
                raw_names = set(fut_df['SM_SYMBOL_NAME'].dropna().unique())
                # Normalize e.g. "TUBEFUT" -> "TIINDIA", or use symbol root
                fno_set = set()
                for name in raw_names:
                    clean = name.replace("FUT", "").strip().upper()
                    fno_set.add(clean)
                # Ensure top known F&O equities are explicitly included
                prominent_fno = {
                    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "BHARTIARTL",
                    "KOTAKBANK", "LT", "ITC", "AXISBANK", "BAJFINANCE", "TATAMOTORS", "MARUTI",
                    "SUNPHARMA", "TITAN", "NTPC", "ONGC", "JSWSTEEL", "POWERGRID", "TATASTEEL",
                    "COALINDIA", "HCLTECH", "ADANIENT", "ADANIPORTS", "WIPRO", "HINDALCO",
                    "DRREDDY", "ULTRACEMCO", "APOLLOHOSP", "GRASIM", "HEROMOTOCO", "BAJAJFINSV",
                    "NESTLEIND", "BPCL", "EICHERMOT", "DIVISLAB", "CIPLA", "TECHM", "BRITANNIA",
                    "TATACONSUM", "HINDUNILVR", "SBILIFE", "HDFCLIFE", "LTIM", "ASIANPAINT",
                    "BEL", "HAL", "VEDL", "PFC", "RECLTD", "SIEMENS", "DLF", "CHOLAFIN",
                    "TRENT", "ETERNAL", "JIOFIN", "POLYCAB", "PERSISTENT", "CUMMINSIND"
                }
                self._fno_universe_cache = fno_set.union(prominent_fno)
            else:
                self._fno_universe_cache = set()
        except Exception as exc:
            logger.debug("Could not load FNO universe from CSV: %s", exc)
            self._fno_universe_cache = set()

    # ── Factor 1: Volume Participation ────────────────────────────────────────
    def compute_volume_participation(self, snapshot: MarketSnapshot) -> FactorResult:
        """
        Calculates volume participation using Median 20D + MAD robust z-score.
        Volume intensity is directionalized by candle return and close location.
        """
        vols = snapshot.volumes_20
        if not vols or snapshot.volume <= 0:
            return FactorResult(score=0, available=True, weight=BASE_WEIGHTS["volume"], detail="Insufficient volume data")

        med = snapshot.volume_median_20 if snapshot.volume_median_20 > 0 else float(np.median(vols))
        deviations = [abs(v - med) for v in vols]
        mad = float(np.median(deviations)) if deviations else 1.0
        mad = max(1.0, mad)

        # Robust z-score using MAD
        z_score = (snapshot.volume - med) / (1.4826 * mad)
        ratio = snapshot.volume / max(1.0, med)

        # Intraday / bar price action direction
        p_curr = snapshot.price
        p_open = snapshot.daily_opens_20[-1] if snapshot.daily_opens_20 else p_curr
        p_high = snapshot.daily_highs_20[-1] if snapshot.daily_highs_20 else p_curr
        p_low = snapshot.daily_lows_20[-1] if snapshot.daily_lows_20 else p_curr
        
        span = max(0.01, p_high - p_low)
        close_pos = (p_curr - p_low) / span  # 0.0 (low) to 1.0 (high)
        ret_pct = ((p_curr - p_open) / max(0.01, p_open)) * 100.0

        # Intensity [0, 100]
        intensity = min(100.0, max(10.0, 40.0 + (z_score * 18.0)))
        direction_factor = (close_pos - 0.5) * 2.0  # -1.0 to +1.0

        # High volume near high = +100; high volume near low = -100
        directional_score = round(direction_factor * intensity)
        directional_score = max(-100, min(100, directional_score))

        detail = f"Vol Ratio: {ratio:.1f}x 20D Median (z-score: {z_score:+.1f}, close pos: {int(close_pos * 100)}%)"
        if ratio < 0.5 and close_pos > 0.55:
            # Extremely low volume with price holding at highs = supply withdrawal / dry-up
            directional_score = max(directional_score, 30)
            detail += " [Low-Vol Supply Dry-Up]"
        return FactorResult(score=directional_score, available=True, weight=BASE_WEIGHTS["volume"], detail=detail)

    # ── Factor 2: Aggressive Flow / CVD ───────────────────────────────────────
    def compute_aggressive_flow(self, snapshot: MarketSnapshot) -> FactorResult:
        """
        Calculates directional execution delta. Uses live CVD engine if active,
        or vectorized multi-bar aggressor delta with divergence detection.
        """
        # 1. Live CVD from engine if available
        if snapshot.live_cvd is not None and snapshot.adv_20 > 0:
            norm_cvd = snapshot.live_cvd / max(1.0, snapshot.adv_20)
            score = int(max(-100, min(100, norm_cvd * 120.0)))
            if snapshot.live_is_absorbing:
                score = min(100, score + 25)
            detail = f"Live Tick CVD: {snapshot.live_cvd:+,.0f} ({norm_cvd:+.2f}x ADV)"
            return FactorResult(score=score, available=True, weight=BASE_WEIGHTS["cvd"], detail=detail)

        # 2. Vectorized multi-bar aggressor delta proxy over last 5-10 sessions
        closes = snapshot.daily_closes_20
        opens = snapshot.daily_opens_20
        highs = snapshot.daily_highs_20
        lows = snapshot.daily_lows_20
        vols = snapshot.volumes_20

        n = min(len(closes), len(opens), len(highs), len(lows), len(vols))
        if n < 3:
            return FactorResult(score=0, available=True, weight=BASE_WEIGHTS["cvd"], detail="Baseline delta proxy")

        deltas = []
        span_floor = QUANT_CONFIG["doji_abs_span_floor"]
        for i in range(n):
            rng = max(span_floor, highs[i] - lows[i])
            body_ret = (closes[i] - opens[i]) / rng
            loc = (closes[i] - lows[i]) / rng
            aggressor_mult = (body_ret * 0.7) + ((loc - 0.5) * 0.6)
            aggressor_mult = max(-1.0, min(1.0, aggressor_mult))
            deltas.append(vols[i] * aggressor_mult)

        adv = snapshot.adv_20 if snapshot.adv_20 > 0 else (float(np.mean(vols)) if vols else 1.0)
        recent_delta_sum = sum(deltas[-3:])
        cum_ratio = recent_delta_sum / max(1.0, adv * 3.0)

        # Check for absorption divergence: Price lower low in recent leg, but delta higher low
        has_bullish_div = False
        has_bearish_div = False
        if n >= 5:
            recent_min_price = min(closes[-2:])
            prior_min_price = min(closes[-5:-2])
            recent_min_delta = min(deltas[-2:])
            prior_min_delta = min(deltas[-5:-2])
            if recent_min_price < prior_min_price and recent_min_delta > prior_min_delta:
                has_bullish_div = True

            recent_max_price = max(closes[-2:])
            prior_max_price = max(closes[-5:-2])
            recent_max_delta = max(deltas[-2:])
            prior_max_delta = max(deltas[-5:-2])
            if recent_max_price > prior_max_price and recent_max_delta < prior_max_delta:
                has_bearish_div = True

        cvd_score = int(max(-90, min(90, cum_ratio * 130.0)))
        if has_bullish_div:
            cvd_score = min(100, cvd_score + 25)
        elif has_bearish_div:
            cvd_score = max(-100, cvd_score - 25)

        div_tag = " [Bullish Absorption Divergence]" if has_bullish_div else (" [Bearish Exhaustion Divergence]" if has_bearish_div else "")
        detail = f"3D Aggressor Delta: {cum_ratio:+.2f}x ADV{div_tag}"
        return FactorResult(score=cvd_score, available=True, weight=BASE_WEIGHTS["cvd"], detail=detail)

    # ── Factor 3: Large Trade Activity ────────────────────────────────────────
    def compute_large_trade_activity(self, snapshot: MarketSnapshot) -> FactorResult:
        """
        Normalized strictly per stock: identifies trade volume bursts exceeding
        the stock's own 95th and 99th percentile, measuring buy vs sell imbalance.
        """
        vols = snapshot.volumes_20
        closes = snapshot.daily_closes_20
        opens = snapshot.daily_opens_20
        if len(vols) < 5:
            return FactorResult(score=0, available=True, weight=BASE_WEIGHTS["large_trades"], detail="Insufficient history for percentile thresholds")

        sorted_vols = sorted(vols)
        p95_idx = int(len(sorted_vols) * 0.95)
        p95_threshold = sorted_vols[min(p95_idx, len(sorted_vols) - 1)]

        large_buy_vol = 0.0
        large_sell_vol = 0.0
        burst_count = 0

        for i in range(len(vols)):
            if vols[i] >= p95_threshold:
                burst_count += 1
                if closes[i] >= opens[i]:
                    large_buy_vol += vols[i]
                else:
                    large_sell_vol += vols[i]

        total_large = large_buy_vol + large_sell_vol
        if total_large <= 0:
            score = 10 if snapshot.price >= snapshot.daily_closes_20[-2] else -10
            detail = "Orderly baseline turnover (no 95th percentile bursts)"
            return FactorResult(score=score, available=True, weight=BASE_WEIGHTS["large_trades"], detail=detail)

        imbalance = (large_buy_vol - large_sell_vol) / total_large  # -1.0 to +1.0
        score = int(round(imbalance * 90.0))
        detail = f"Large Volume Imbalance: {imbalance:+.2f} ({burst_count} institutional bursts in 20D)"

        # Incorporate official exchange Bulk/Block deals if recorded
        if snapshot.bulk_deals_net_cr is not None and abs(snapshot.bulk_deals_net_cr) > 0:
            if snapshot.bulk_deals_net_cr >= 10.0:
                score = min(100, score + 25)
                detail += f" | Verified Bulk Deal Accumulation (+₹{snapshot.bulk_deals_net_cr:.1f} Cr)"
            elif snapshot.bulk_deals_net_cr <= -10.0:
                score = max(-100, score - 25)
                detail += f" | Verified Bulk Deal Distribution (-₹{abs(snapshot.bulk_deals_net_cr):.1f} Cr)"

        return FactorResult(score=score, available=True, weight=BASE_WEIGHTS["large_trades"], detail=detail)

    # ── Factor 4: Order-Book / Depth Pressure & Absorption ────────────────────
    def compute_depth_and_absorption(self, snapshot: MarketSnapshot) -> FactorResult:
        """
        Evaluates order-book pressure (OBI) and price absorption at support/resistance.
        """
        obi_val = snapshot.live_deep_obi if snapshot.live_deep_obi is not None else snapshot.live_obi
        
        p_curr = snapshot.price
        p_low = snapshot.daily_lows_20[-1] if snapshot.daily_lows_20 else p_curr
        p_high = snapshot.daily_highs_20[-1] if snapshot.daily_highs_20 else p_curr
        p_open = snapshot.daily_opens_20[-1] if snapshot.daily_opens_20 else p_curr
        
        body = abs(p_curr - p_open)
        min_body = max(QUANT_CONFIG["doji_abs_span_floor"], QUANT_CONFIG["doji_body_bps"] * p_curr)
        effective_body = max(body, min_body)
        span = max(QUANT_CONFIG["doji_abs_span_floor"], p_high - p_low)
        lower_wick = min(p_open, p_curr) - p_low
        upper_wick = p_high - max(p_open, p_curr)

        is_support_absorption = False
        is_resistance_rejection = False

        if snapshot.support_level and abs(p_low - snapshot.support_level) / snapshot.support_level < 0.015:
            if lower_wick > effective_body * 1.5 and p_curr > p_low:
                is_support_absorption = True

        if snapshot.avwap_month and abs(p_low - snapshot.avwap_month) / snapshot.avwap_month < 0.012:
            if p_curr >= snapshot.avwap_month:
                is_support_absorption = True

        if snapshot.resistance_level and abs(p_high - snapshot.resistance_level) / snapshot.resistance_level < 0.015:
            if upper_wick > effective_body * 1.5 and p_curr < p_high:
                is_resistance_rejection = True

        score = 0
        if obi_val is not None:
            score += int(round(obi_val * 60.0))
        
        if is_support_absorption:
            score += 45
        elif is_resistance_rejection:
            score -= 45

        score = max(-100, min(100, score))
        
        detail_parts = []
        if obi_val is not None:
            detail_parts.append(f"OBI: {obi_val:+.2f}")
        if is_support_absorption:
            detail_parts.append("Buy Absorption at Support/AVWAP")
        elif is_resistance_rejection:
            detail_parts.append("Sell Exhaustion at Resistance")
        else:
            detail_parts.append("Balanced Book Depth")

        detail = " | ".join(detail_parts)
        return FactorResult(score=score, available=True, weight=BASE_WEIGHTS["depth"], detail=detail)

    # ── Factor 5: Delivery / Position Building (Conditional) ──────────────────
    def compute_delivery_building(self, snapshot: MarketSnapshot) -> FactorResult:
        """
        Evaluates delivery accumulation/distribution if delivery data is present.
        If unavailable, returns available=False and score=None (no penalty).
        """
        if snapshot.delivery_pct is None and not snapshot.delivery_history:
            return FactorResult(score=None, available=False, weight=BASE_WEIGHTS["delivery"], detail="Delivery data unavailable")

        pct = snapshot.delivery_pct or 0.0
        hist = snapshot.delivery_history or [pct]
        med_deliv = float(np.median(hist)) if hist else 40.0

        p_curr = snapshot.price
        p_prev = snapshot.daily_closes_20[-2] if len(snapshot.daily_closes_20) >= 2 else p_curr
        p_chg = ((p_curr - p_prev) / max(0.01, p_prev)) * 100.0

        diff = pct - med_deliv
        if diff > 10.0 and p_chg >= -0.5:
            score = int(min(100, 50 + diff * 3.0))
            detail = f"High Delivery Build ({pct:.1f}% vs {med_deliv:.1f}% med) with positive containment"
        elif diff > 10.0 and p_chg < -1.5:
            score = int(max(-100, -40 - diff * 2.5))
            detail = f"Elevated Delivery on Downside ({pct:.1f}%) — Institutional liquidation"
        else:
            score = int(max(-40, min(40, diff * 2.5)))
            detail = f"Delivery: {pct:.1f}% (normal baseline)"

        return FactorResult(score=score, available=True, weight=BASE_WEIGHTS["delivery"], detail=detail)

    # ── Factor 6: F&O / Derivatives Positioning (Conditional) ─────────────────
    def compute_fno_positioning(self, snapshot: MarketSnapshot) -> FactorResult:
        """
        Evaluates F&O Open Interest changes vs price changes.
        For Non-F&O stocks, returns available=False and score=None.
        DOES NOT penalize or treat missing F&O as neutral zero.
        """
        if not snapshot.is_fno_eligible:
            return FactorResult(
                score=None,
                available=False,
                weight=BASE_WEIGHTS["fno_oi"],
                detail="Non-F&O Equity (Exempt from Derivatives Weighting)"
            )

        if snapshot.futures_oi_change_pct is None:
            return FactorResult(
                score=None,
                available=False,
                weight=BASE_WEIGHTS["fno_oi"],
                detail="F&O Eligible (Real-time OI data pending)"
            )

        oi_chg = snapshot.futures_oi_change_pct
        p_curr = snapshot.price
        p_prev = snapshot.daily_closes_20[-2] if len(snapshot.daily_closes_20) >= 2 else p_curr
        p_chg = ((p_curr - p_prev) / max(0.01, p_prev)) * 100.0

        if p_chg >= 0.2 and oi_chg >= 2.0:
            score = min(100, int(60 + oi_chg * 4.0))
            detail = f"Long Build Up (Price +{p_chg:.1f}%, OI +{oi_chg:.1f}%)"
        elif p_chg >= 0.2 and oi_chg <= -2.0:
            score = min(75, int(40 + abs(oi_chg) * 3.0))
            detail = f"Short Covering (Price +{p_chg:.1f}%, OI {oi_chg:.1f}%)"
        elif p_chg <= -0.2 and oi_chg <= -2.0:
            score = max(-75, int(-40 - abs(oi_chg) * 3.0))
            detail = f"Long Unwinding (Price {p_chg:.1f}%, OI {oi_chg:.1f}%)"
        elif p_chg <= -0.2 and oi_chg >= 2.0:
            score = max(-100, int(-60 - oi_chg * 4.0))
            detail = f"Short Build Up (Price {p_chg:.1f}%, OI +{oi_chg:.1f}%)"
        else:
            score = 0
            detail = f"Neutral F&O OI (OI {oi_chg:+.1f}%)"

        # Incorporate Option Chain Put-Call Ratio (PCR) & Max Pain if available
        if snapshot.pcr is not None and snapshot.pcr > 0:
            if snapshot.pcr >= 1.35:
                score = min(100, score + 25)
                detail += f" | PCR: {snapshot.pcr:.2f} (Institutional Put Floor)"
            elif snapshot.pcr <= 0.65:
                score = max(-100, score - 25)
                detail += f" | PCR: {snapshot.pcr:.2f} (Heavy Call Wall Resistance)"
            else:
                detail += f" | PCR: {snapshot.pcr:.2f} (Balanced)"

        if snapshot.max_pain is not None and snapshot.max_pain > 0:
            dist_pct = ((snapshot.max_pain - p_curr) / p_curr) * 100.0
            if abs(dist_pct) <= 1.5:
                detail += f" | Pinning Near Max Pain ₹{snapshot.max_pain:.1f}"

        return FactorResult(score=score, available=True, weight=BASE_WEIGHTS["fno_oi"], detail=detail)

    # ── Factor 7: Price Acceptance & AVWAP Benchmark ──────────────────────────
    def compute_price_acceptance(self, snapshot: MarketSnapshot) -> FactorResult:
        """
        Measures price retention above institutional benchmarks (Monthly AVWAP, 50-EMA).
        """
        p = snapshot.price
        score = 0
        details = []

        if snapshot.avwap_month and snapshot.avwap_month > 0:
            dist_avwap = ((p - snapshot.avwap_month) / snapshot.avwap_month) * 100.0
            if dist_avwap >= 0.5:
                score += min(50, int(20 + dist_avwap * 8.0))
                details.append(f"Above Monthly AVWAP ({dist_avwap:+.1f}%)")
            elif dist_avwap >= -0.5:
                score += 15
                details.append("Coiling at Monthly AVWAP")
            else:
                score -= min(50, int(20 + abs(dist_avwap) * 8.0))
                details.append(f"Below Monthly AVWAP ({dist_avwap:+.1f}%)")

        if snapshot.ema_50 and snapshot.ema_50 > 0:
            dist_ema = ((p - snapshot.ema_50) / snapshot.ema_50) * 100.0
            if dist_ema >= 0.0:
                score += min(50, int(15 + dist_ema * 6.0))
                details.append(f"Above 50 EMA ({dist_ema:+.1f}%)")
            else:
                score -= min(50, int(15 + abs(dist_ema) * 6.0))
                details.append(f"Below 50 EMA ({dist_ema:+.1f}%)")

        score = max(-100, min(100, score))
        detail = " | ".join(details) if details else "Benchmark alignment neutral"
        return FactorResult(score=score, available=True, weight=BASE_WEIGHTS["price_acceptance"], detail=detail)

    # ── Master Orchestrator: Universal Scoring & Normalization ────────────────
    def compute_flow(self, snapshot: MarketSnapshot) -> InstitutionalFlowResult:
        """
        Calculates all 7 factors, dynamically normalizes weights across available factors,
        evaluates confidence & coverage, detects stealth phases & conflicts, and derives trend.
        """
        if not snapshot.is_fno_eligible:
            snapshot.is_fno_eligible = self.is_fno_symbol(snapshot.symbol)

        # 1. Compute Individual Factors
        factors: Dict[str, FactorResult] = {
            "volume": self.compute_volume_participation(snapshot),
            "cvd": self.compute_aggressive_flow(snapshot),
            "large_trades": self.compute_large_trade_activity(snapshot),
            "depth": self.compute_depth_and_absorption(snapshot),
            "delivery": self.compute_delivery_building(snapshot),
            "fno_oi": self.compute_fno_positioning(snapshot),
            "price_acceptance": self.compute_price_acceptance(snapshot),
        }

        # 2. Dynamic Weight Normalization
        available_factors = {k: v for k, v in factors.items() if v.available and v.score is not None}
        available_weight_sum = sum(v.weight for v in available_factors.values())
        total_possible_weight = sum(BASE_WEIGHTS.values())

        if available_weight_sum <= 0:
            weighted_score = 0
            coverage_pct = 0
        else:
            weighted_score = sum(v.score * v.weight for v in available_factors.values()) / available_weight_sum
            coverage_pct = int(round((available_weight_sum / total_possible_weight) * 100.0))

        score = int(round(max(-100.0, min(100.0, weighted_score))))

        # 3. Factor Agreement Consensus
        active_scores = [v.score for v in available_factors.values() if v.score is not None]
        if active_scores:
            if score > 0:
                matching = sum(1 for s in active_scores if s > 0)
            elif score < 0:
                matching = sum(1 for s in active_scores if s < 0)
            else:
                matching = sum(1 for s in active_scores if abs(s) <= 20)
            consensus = matching / float(len(active_scores))
        else:
            consensus = 0.5

        # 4. Confidence Score
        data_quality = 1.0 if len(snapshot.volumes_20) >= 15 else 0.8
        confidence = int(round(consensus * (coverage_pct / 100.0) * data_quality * 100.0))
        confidence = max(20, min(98, confidence))

        # 5. Classification
        if score >= 60:
            classification = "STRONG_ACCUMULATION"
        elif score >= 20:
            classification = "ACCUMULATION"
        elif score <= -60:
            classification = "STRONG_DISTRIBUTION"
        elif score <= -20:
            classification = "DISTRIBUTION"
        else:
            classification = "NEUTRAL"

        # 6. Multi-Day Historical State (5D, 20D Trend & 10D Persistence)
        trend_metrics = self._calculate_flow_trend(snapshot, score)

        # 7. Stealth Phase Detection
        stealth_phase = self._detect_stealth_phase(snapshot, score, factors)

        # 8. Conflict Detection
        conflicts = self._detect_conflicts(snapshot, score, factors)

        # 9. Key Signals Extraction
        signals = []
        for name, f_res in factors.items():
            if f_res.available and f_res.score is not None:
                if f_res.score >= 50:
                    signals.append(f"Bullish {name.replace('_', ' ').title()}: {f_res.detail}")
                elif f_res.score <= -50:
                    signals.append(f"Bearish {name.replace('_', ' ').title()}: {f_res.detail}")

        if stealth_phase:
            signals.insert(0, f"⚡ {stealth_phase.replace('_', ' ').title()} In Progress")

        # 10. Natural Language Narrative Synthesis
        summary = self._synthesize_narrative(
            snapshot=snapshot,
            score=score,
            classification=classification,
            confidence=confidence,
            coverage=coverage_pct,
            factors=factors,
            trend=trend_metrics,
            stealth=stealth_phase,
            conflicts=conflicts,
        )

        return InstitutionalFlowResult(
            score=score,
            classification=classification,
            confidence=confidence,
            coverage=coverage_pct,
            trend=trend_metrics,
            components={k: v.score for k, v in factors.items()},
            signals=signals,
            conflicts=conflicts,
            stealth_phase=stealth_phase,
            summary=summary,
        )

    def _calculate_flow_trend(self, snapshot: MarketSnapshot, current_score: int) -> Dict[str, Any]:
        """Calculates multi-session IFP trend and persistence."""
        closes = snapshot.daily_closes_20
        vols = snapshot.volumes_20
        n = min(len(closes), len(vols))

        if n < 5:
            return {"trend_5d": current_score, "trend_20d": current_score, "persistence_10d": 0.5}

        past_ifps = []
        for i in range(max(0, n - 20), n - 1):
            p_c = closes[i]
            p_o = snapshot.daily_opens_20[i] if i < len(snapshot.daily_opens_20) else p_c
            p_h = snapshot.daily_highs_20[i] if i < len(snapshot.daily_highs_20) else p_c
            p_l = snapshot.daily_lows_20[i] if i < len(snapshot.daily_lows_20) else p_c
            span = max(0.01, p_h - p_l)
            loc = (p_c - p_l) / span
            ret = (p_c - p_o) / max(0.01, p_o)
            vol_ratio = vols[i] / max(1.0, snapshot.adv_20)
            
            s = int(round(((loc - 0.5) * 1.4 + (ret * 8.0)) * min(2.5, vol_ratio) * 50.0))
            s = max(-100, min(100, s))
            past_ifps.append(s)

        past_ifps.append(current_score)
        
        last_5 = past_ifps[-5:]
        last_10 = past_ifps[-10:] if len(past_ifps) >= 10 else past_ifps
        trend_5d = int(round(float(np.mean(last_5))))
        trend_20d = int(round(float(np.mean(past_ifps))))
        
        pos_count = sum(1 for s in last_10 if s > 0)
        persistence_10d = round(pos_count / float(len(last_10)), 2)

        return {
            "trend_5d": trend_5d,
            "trend_20d": trend_20d,
            "persistence_10d": persistence_10d,
        }

    def _detect_stealth_phase(self, snapshot: MarketSnapshot, score: int, factors: Dict[str, FactorResult]) -> Optional[str]:
        """Detects stealth accumulation or stealth distribution regimes."""
        closes = snapshot.daily_closes_20
        if len(closes) < 5:
            return None

        pct_change_5d = abs((closes[-1] - closes[-5]) / max(0.01, closes[-5])) * 100.0
        
        cvd_score = factors["cvd"].score or 0
        large_score = factors["large_trades"].score or 0
        depth_score = factors["depth"].score or 0

        # Stealth Accumulation: Strong institutional demand/absorption, but price constrained sideways
        if score >= 35 and (cvd_score >= 25 or large_score >= 35 or depth_score >= 35) and pct_change_5d <= 3.0:
            return "STEALTH_ACCUMULATION"

        # Stealth Distribution: Heavy institutional supply/outflow, price held artificially stable
        if score <= -35 and (cvd_score <= -25 or large_score <= -35 or depth_score <= -35) and pct_change_5d <= 3.0:
            return "STEALTH_DISTRIBUTION"

        return None

    def _detect_conflicts(self, snapshot: MarketSnapshot, score: int, factors: Dict[str, FactorResult]) -> List[str]:
        """Identifies conflicting signals across data layers."""
        conflicts = []

        # 1. Cash Flow Accumulation vs Derivatives Selling
        cash_score = factors["large_trades"].score or 0
        fno_score = factors["fno_oi"].score
        if fno_score is not None:
            if cash_score >= 40 and fno_score <= -40:
                conflicts.append("Derivatives Distribution Conflict (Cash buying vs F&O short buildup)")
            elif cash_score <= -40 and fno_score >= 40:
                conflicts.append("Cash Liquidation Conflict (F&O long bias vs heavy cash dumping)")

        # 2. Technical Setup vs Institutional Flow Divergence
        tech_bias = snapshot.technical_bias
        if tech_bias in ("BULLISH_BREAKOUT", "50EMA_BOUNCE", "SQUEEZE_COIL") and score <= -30:
            conflicts.append("Technical Bullish Breakout vs Institutional Supply Overhang (Caution: Distribution Risk)")
        elif tech_bias == "BEARISH_BREAKDOWN" and score >= 35:
            conflicts.append("Technical Breakdown vs Institutional Absorption (Possible Bear Trap / Sweep)")

        return conflicts

    def _synthesize_narrative(
        self,
        snapshot: MarketSnapshot,
        score: int,
        classification: str,
        confidence: int,
        coverage: int,
        factors: Dict[str, FactorResult],
        trend: Dict[str, Any],
        stealth: Optional[str],
        conflicts: List[str],
    ) -> str:
        """Synthesizes an executive-grade narrative paragraph."""
        sign_str = f"+{score}" if score > 0 else f"{score}"
        class_str = classification.replace("_", " ").title()
        fno_type = "F&O Active" if snapshot.is_fno_eligible else "Cash Equity (Non-F&O)"
        
        parts = [
            f"Institutional Flow Index: {sign_str}/100 [{class_str}] with {confidence}% Confidence ({coverage}% data coverage, {fno_type})."
        ]

        if stealth == "STEALTH_ACCUMULATION":
            parts.append(
                "⚡ Stealth Accumulation Detected: Commercial participants are aggressively absorbing floating supply "
                "within a tight price corridor without triggering an overt momentum expansion."
            )
        elif stealth == "STEALTH_DISTRIBUTION":
            parts.append(
                "⚠️ Stealth Distribution Detected: Large participant supply is actively offloading while local price "
                "action remains artificially stabilized."
            )

        # Highlight top factor drivers
        driver_details = []
        for f_name in ["cvd", "volume", "large_trades", "depth", "price_acceptance"]:
            f_res = factors.get(f_name)
            if f_res and f_res.available and f_res.score is not None and abs(f_res.score) >= 30:
                driver_details.append(f_res.detail)

        if driver_details:
            parts.append(f"Key Drivers: {'; '.join(driver_details[:2])}.")

        # Trend & Persistence
        p_pct = int(trend.get("persistence_10d", 0.5) * 100)
        parts.append(f"Multi-Day Profile: 5D flow tracks at {trend.get('trend_5d', 0):+d} with {p_pct}% 10-session bullish persistence.")

        # Conflict alert
        if conflicts:
            parts.append(f"Alert: {conflicts[0]}")

        return " ".join(parts)


# ── Global Singleton Accessor ─────────────────────────────────────────────────
_ENGINE_INSTANCE: Optional[InstitutionalFlowEngine] = None

def get_institutional_flow_engine() -> InstitutionalFlowEngine:
    global _ENGINE_INSTANCE
    if _ENGINE_INSTANCE is None:
        _ENGINE_INSTANCE = InstitutionalFlowEngine()
    return _ENGINE_INSTANCE
