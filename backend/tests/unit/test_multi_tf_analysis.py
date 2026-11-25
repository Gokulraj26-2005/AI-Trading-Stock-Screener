"""
Unit tests for Multi-Timeframe Chart Analysis Service.
"""

from datetime import datetime, timedelta
import pytest
from zoneinfo import ZoneInfo

from services.multi_tf_analysis import (
    CandleBar,
    ConsolidationPattern,
    HistoricalCandleCache,
    MultiTFAnalysisService,
    compute_anchored_vwap,
    compute_atr,
    compute_bollinger_squeeze,
    compute_ema,
    compute_sma,
    detect_candle_pattern,
    detect_consolidation,
    evaluate_ma_interaction,
    resample_monthly,
    resample_weekly,
    synthesize_commentary_and_score,
)
from services.institutional_flow_engine import InstitutionalFlowResult

_IST = ZoneInfo("Asia/Kolkata")


def _generate_synthetic_bars(n=60, base_price=100.0, trend=0.1) -> list[CandleBar]:
    bars = []
    base_time = int(datetime(2024, 1, 1, 9, 15, tzinfo=_IST).timestamp())
    curr = base_price
    for i in range(n):
        o = curr
        c = curr + trend + (0.5 if i % 2 == 0 else -0.5)
        h = max(o, c) + 0.8
        l = min(o, c) - 0.8
        v = 10000 + (i * 100)
        bars.append(CandleBar(time=base_time + (i * 86400), open=o, high=h, low=l, close=c, volume=v))
        curr = c
    return bars


def test_compute_sma():
    closes = [10.0, 20.0, 30.0, 40.0, 50.0]
    sma3 = compute_sma(closes, 3)
    assert sma3[0] is None
    assert sma3[1] is None
    assert sma3[2] == 20.0
    assert sma3[3] == 30.0
    assert sma3[4] == 40.0


def test_compute_ema():
    closes = [10.0, 20.0, 30.0, 40.0, 50.0]
    ema3 = compute_ema(closes, 3)
    assert ema3[0] is None
    assert ema3[1] is None
    assert ema3[2] == 20.0  # Seeded by SMA
    # alpha = 2 / 4 = 0.5; ema4 = 0.5 * 40 + 0.5 * 20 = 30
    assert ema3[3] == 30.0
    # ema5 = 0.5 * 50 + 0.5 * 30 = 40
    assert ema3[4] == 40.0


def test_resample_weekly_and_monthly():
    bars = _generate_synthetic_bars(60)
    weekly = resample_weekly(bars)
    monthly = resample_monthly(bars)

    assert len(weekly) >= 8
    assert len(monthly) >= 2
    assert all(w.high >= max(w.open, w.close) for w in weekly)
    assert all(w.low <= min(w.open, w.close) for w in weekly)
    assert all(m.high >= max(m.open, m.close) for m in monthly)


def test_detect_consolidation():
    # Generate tight sideways range
    bars = []
    base_time = int(datetime(2024, 1, 1, 9, 15, tzinfo=_IST).timestamp())
    for i in range(25):
        bars.append(CandleBar(
            time=base_time + (i * 86400),
            open=100.0,
            high=102.0,
            low=98.0,
            close=100.5,
            volume=5000,
        ))
    pattern = detect_consolidation(bars, max_span_pct=8.5)
    assert pattern.is_consolidating is True
    assert pattern.resistance == 102.0
    assert pattern.support == 98.0
    assert pattern.range_span_pct < 5.0
    assert pattern.duration_bars >= 15


def test_evaluate_ma_interaction():
    # Bounce condition: low tests EMA50, closes green with wick rejection
    ma_status = evaluate_ma_interaction(
        current_price=105.0,
        current_low=99.8,
        current_high=106.0,
        current_open=101.0,
        ema50=100.0,
        sma50=100.2,
        avwap=100.1,
    )
    assert ma_status["touching_ema50"] is True
    assert ma_status["bouncing_ema50"] is True
    assert ma_status["confluence_shelf"] is True


def test_synthesize_commentary_and_score():
    bars = _generate_synthetic_bars(30)
    current_bar = bars[-1]
    consolidation = detect_consolidation(bars)
    ma_status = evaluate_ma_interaction(
        current_price=current_bar.close,
        current_low=current_bar.low,
        current_high=current_bar.high,
        current_open=current_bar.open,
        ema50=current_bar.close * 0.99,
        sma50=current_bar.close * 0.98,
        avwap=current_bar.close * 0.99,
    )

    commentary = synthesize_commentary_and_score(
        symbol="TESTSTOCK",
        current_bar=current_bar,
        consolidation=consolidation,
        ma_status=ma_status,
        squeeze_pct=12.0,
        atr=2.5,
        ema50=current_bar.close * 0.99,
        sma50=current_bar.close * 0.98,
        sma200=current_bar.close * 0.90,
        avwap=current_bar.close * 0.99,
        weekly_trend="BULLISH",
    )

    assert commentary.potential_score >= 60
    assert commentary.quality_tier in ("A+", "A", "B")
    assert len(commentary.structure_base) > 0
    assert len(commentary.actionable_thesis) > 0
    assert commentary.trigger_price is not None


def test_early_degenerate_sr_short_circuit():
    """Verify corrupted clustering where support >= resistance immediately halts scoring with sentinel."""
    bars = _generate_synthetic_bars(20)
    current_bar = bars[-1]
    # Synthetic degenerate S/R pattern: support higher than resistance
    degenerate_pattern = ConsolidationPattern(
        is_consolidating=True,
        resistance=100.0,
        support=105.0,  # sup >= res anomaly!
        range_span_pct=5.0,
        duration_bars=20,
        start_time=bars[0].time,
        end_time=bars[-1].time,
        containment_ratio=0.9,
    )
    ma_status = {
        "touching_ema50": False,
        "bouncing_ema50": False,
        "dist_ema50_pct": 0.0,
        "touching_avwap": False,
        "bouncing_avwap": False,
        "dist_avwap_pct": 0.0,
        "confluence_shelf": False,
        "confluence_level": None,
    }

    commentary = synthesize_commentary_and_score(
        symbol="BAD_SR",
        current_bar=current_bar,
        consolidation=degenerate_pattern,
        ma_status=ma_status,
        squeeze_pct=10.0,
        atr=2.0,
        ema50=100.0,
        sma50=100.0,
        sma200=90.0,
        avwap=100.0,
        weekly_trend="BULLISH",
    )

    assert commentary.bias == "INVALID_STRUCTURE"
    assert commentary.potential_score == 20
    assert commentary.quality_tier == "C"
    assert commentary.trigger_price is None


def test_explicit_conflict_matrix_cases():
    """Verify specific-before-general precedence in the 8-state conflict & confluence matrix."""
    base_bar = CandleBar(time=1700000000, open=100.0, high=102.0, low=98.0, close=100.0, volume=10000)
    ma_status = {
        "touching_ema50": False,
        "bouncing_ema50": False,
        "dist_ema50_pct": 0.0,
        "touching_avwap": False,
        "bouncing_avwap": False,
        "dist_avwap_pct": 0.0,
        "confluence_shelf": False,
        "confluence_level": None,
    }

    # Case 1: Breakdown + Accumulation (Conflict / Stop-Hunt)
    # Price breaks support (sup=100, close=98 < 99) but flow_result shows strong accumulation (+60)
    pattern_breakdown = ConsolidationPattern(
        is_consolidating=False, resistance=110.0, support=100.0,
        range_span_pct=10.0, duration_bars=20, start_time=0, end_time=0, containment_ratio=0.8
    )
    bar_below_sup = CandleBar(time=1700000000, open=99.0, high=99.0, low=97.0, close=98.0, volume=15000)
    flow_accumulation = InstitutionalFlowResult(
        score=60, classification="ACCUMULATION", confidence=80, coverage=100,
        trend={}, components={}, signals=[], conflicts=[], stealth_phase="NONE",
        summary="Strong institutional accumulation"
    )

    comm1 = synthesize_commentary_and_score(
        symbol="CASE1", current_bar=bar_below_sup, consolidation=pattern_breakdown,
        ma_status=ma_status, squeeze_pct=None, atr=2.0, ema50=105.0, sma50=105.0,
        sma200=105.0, avwap=105.0, weekly_trend="BEARISH", flow_result=flow_accumulation
    )
    # Critical test: accumulation floor CANNOT overwrite breakdown ceiling with 60+
    assert comm1.bias == "CONFLICTED_SIGNAL"
    assert comm1.potential_score <= 35

    # Case 2: Breakout + Distribution (Conflict / Bull Trap)
    # Price breaks resistance (res=100, close=102 > 101) but flow shows distribution (-50)
    pattern_breakout = ConsolidationPattern(
        is_consolidating=True, resistance=100.0, support=90.0,
        range_span_pct=10.0, duration_bars=20, start_time=0, end_time=0, containment_ratio=0.9
    )
    bar_above_res = CandleBar(time=1700000000, open=101.0, high=103.0, low=100.5, close=102.5, volume=20000)
    flow_distribution = InstitutionalFlowResult(
        score=-50, classification="DISTRIBUTION", confidence=85, coverage=100,
        trend={}, components={}, signals=[], conflicts=[], stealth_phase="NONE",
        summary="Heavy institutional distribution"
    )

    comm2 = synthesize_commentary_and_score(
        symbol="CASE2", current_bar=bar_above_res, consolidation=pattern_breakout,
        ma_status=ma_status, squeeze_pct=None, atr=2.0, ema50=95.0, sma50=95.0,
        sma200=90.0, avwap=95.0, weekly_trend="BULLISH", flow_result=flow_distribution
    )
    assert comm2.bias == "DISTRIBUTION_TRAP"
    assert comm2.potential_score <= 40

    # Case 3: Concordant Bearish (Breakdown + Distribution)
    comm3 = synthesize_commentary_and_score(
        symbol="CASE3", current_bar=bar_below_sup, consolidation=pattern_breakdown,
        ma_status=ma_status, squeeze_pct=None, atr=2.0, ema50=105.0, sma50=105.0,
        sma200=105.0, avwap=105.0, weekly_trend="BEARISH", flow_result=flow_distribution
    )
    assert comm3.bias == "BEARISH_BREAKDOWN"
    assert comm3.potential_score <= 20

    # Case 4: Concordant Bullish (Reclaim + Accumulation)
    comm4 = synthesize_commentary_and_score(
        symbol="CASE4", current_bar=bar_above_res, consolidation=pattern_breakout,
        ma_status=ma_status, squeeze_pct=15.0, atr=2.0, ema50=95.0, sma50=95.0,
        sma200=90.0, avwap=95.0, weekly_trend="BULLISH", flow_result=flow_accumulation
    )
    assert comm4.bias == "BULLISH_BREAKOUT"
    assert comm4.potential_score >= 75

    # Case 5: Symmetric zero guards (zero support or resistance must not crash or trigger false breaks)
    pattern_zero_sr = ConsolidationPattern(
        is_consolidating=False, resistance=0.0, support=0.0,
        range_span_pct=0.0, duration_bars=0, start_time=0, end_time=0, containment_ratio=0.0
    )
    comm5 = synthesize_commentary_and_score(
        symbol="CASE5", current_bar=base_bar, consolidation=pattern_zero_sr,
        ma_status=ma_status, squeeze_pct=None, atr=2.0, ema50=None, sma50=None,
        sma200=None, avwap=None, weekly_trend="BEARISH", flow_result=None
    )
    assert comm5.bias != "INVALID_STRUCTURE"
    assert comm5.bias != "BEARISH_BREAKDOWN"
    assert comm5.potential_score >= 10


@pytest.mark.asyncio
async def test_prior_base_breakdown_and_breakout_symmetry():
    """Verify that prior-base consolidation allows both breakdown and breakout to trigger without circular self-reference."""
    from services.multi_tf_analysis import get_multi_tf_analysis_service, HistoricalCandleCache
    service = get_multi_tf_analysis_service()

    # 20 prior bars forming base: High=102.0, Low=98.0
    bars_base = [
        CandleBar(time=1700000000 + (i * 86400), open=100.0, high=102.0, low=98.0, close=100.0, volume=10000)
        for i in range(20)
    ]

    # Sub-case A: Active breakdown day (drops to Low=90, Close=92 < 98 * 0.99)
    bars_breakdown = list(bars_base) + [
        CandleBar(time=1700000000 + (20 * 86400), open=97.0, high=97.5, low=90.0, close=92.0, volume=25000)
    ]
    prior_base_down = detect_consolidation(bars_breakdown[:-1])
    assert prior_base_down.support == 98.0
    assert bars_breakdown[-1].close < prior_base_down.support * 0.99  # 92 < 97.02 -> True breakdown!

    # Sub-case B: Active breakout day (surges to High=112, Close=110 > 102 * 1.01)
    bars_breakout = list(bars_base) + [
        CandleBar(time=1700000000 + (20 * 86400), open=103.0, high=112.0, low=102.5, close=110.0, volume=35000)
    ]
    prior_base_up = detect_consolidation(bars_breakout[:-1])
    assert prior_base_up.resistance == 102.0
    assert bars_breakout[-1].close > prior_base_up.resistance * 1.01  # 110 > 103.02 -> True breakout!


@pytest.mark.asyncio
async def test_insufficient_history_sentinel():
    """Verify <=15 bars returns explicit INSUFFICIENT_HISTORY sentinel rather than self-referential base."""
    from unittest.mock import AsyncMock
    from services.multi_tf_analysis import MultiTFAnalysisService

    service = MultiTFAnalysisService()
    # 10 bars available (newly listed IPO / insufficient history)
    short_history = [
        CandleBar(time=1700000000 + (i * 86400), open=100.0, high=102.0, low=98.0, close=100.0, volume=1000)
        for i in range(10)
    ]
    service.fetch_or_generate_daily_candles = AsyncMock(return_value=short_history)

    result = await service.analyze_symbol("NEW_IPO")

    assert result["commentary"]["bias"] == "INSUFFICIENT_HISTORY"
    assert result["commentary"]["potential_score"] == 20
    assert result["commentary"]["quality_tier"] == "C"
    assert result["commentary"]["trigger_price"] is None
    assert result["consolidation"]["is_consolidating"] is False


def test_dynamic_atr_volatility_bands():
    """Verify breakout/breakdown buffers scale dynamically with ATR rather than fixed 1% percentages."""
    pattern = ConsolidationPattern(
        is_consolidating=True, resistance=105.0, support=100.0,
        range_span_pct=5.0, duration_bars=20, start_time=0, end_time=0, containment_ratio=0.9
    )
    ma_status = {
        "touching_ema50": False, "bouncing_ema50": False, "dist_ema50_pct": 0.0,
        "touching_avwap": False, "bouncing_avwap": False, "dist_avwap_pct": 0.0,
        "confluence_shelf": False, "confluence_level": None,
    }

    # 1. Low-beta stock: ATR = 0.80 (0.8%). Breakdown buffer = 0.20 * 0.80 = 0.16.
    # Price = 99.70 (< 100 - 0.16 = 99.84). Under old fixed 1% rule (needs < 99.0), this failed.
    # Under dynamic ATR rule, it successfully detects breakdown.
    bar_low_beta = CandleBar(time=1700000000, open=99.8, high=99.8, low=99.6, close=99.70, volume=10000)
    comm_low_beta = synthesize_commentary_and_score(
        symbol="LOW_BETA", current_bar=bar_low_beta, consolidation=pattern,
        ma_status=ma_status, squeeze_pct=None, atr=0.80, ema50=100.0, sma50=100.0,
        sma200=100.0, avwap=100.0, weekly_trend="BEARISH", flow_result=None
    )
    assert comm_low_beta.bias == "BEARISH_BREAKDOWN"

    # 2. High-beta stock: ATR = 4.00 (4.0%). Breakdown buffer = 0.20 * 4.00 = 0.80 (needs < 99.20).
    # Price = 99.50 is only 0.50 below support (0.125x ATR noise).
    # Dynamic ATR correctly rejects this as noise, keeping it in NEUTRAL_CHOP.
    bar_high_beta_noise = CandleBar(time=1700000000, open=99.8, high=99.8, low=99.4, close=99.50, volume=10000)
    comm_high_beta = synthesize_commentary_and_score(
        symbol="HIGH_BETA", current_bar=bar_high_beta_noise, consolidation=pattern,
        ma_status=ma_status, squeeze_pct=None, atr=4.00, ema50=100.0, sma50=100.0,
        sma200=100.0, avwap=100.0, weekly_trend="BEARISH", flow_result=None
    )
    assert comm_high_beta.bias != "BEARISH_BREAKDOWN"


def test_compression_collinearity_cap():
    """Verify combined base tightness (+25) and Bollinger squeeze (+15) cannot exceed max_compression_bonus (30)."""
    pattern_tight = ConsolidationPattern(
        is_consolidating=True, resistance=105.0, support=100.0,
        range_span_pct=4.8,  # span <= 5.0 -> raw +25 pts
        duration_bars=25, start_time=0, end_time=0, containment_ratio=0.95
    )
    ma_status = {
        "touching_ema50": False, "bouncing_ema50": False, "dist_ema50_pct": 0.0,
        "touching_avwap": False, "bouncing_avwap": False, "dist_avwap_pct": 0.0,
        "confluence_shelf": False, "confluence_level": None,
    }
    bar = CandleBar(time=1700000000, open=102.0, high=103.0, low=101.5, close=102.0, volume=10000)

    # Base starts at 50. Tightness (+25) + Squeeze (+15) would equal +40 without cap.
    # With cap (30), score should be 50 + 30 = 80 (not 90).
    comm = synthesize_commentary_and_score(
        symbol="SQUEEZE_STOCK", current_bar=bar, consolidation=pattern_tight,
        ma_status=ma_status, squeeze_pct=10.0, atr=2.0, ema50=102.0, sma50=102.0,
        sma200=102.0, avwap=102.0, weekly_trend="NEUTRAL", flow_result=None
    )
    assert comm.potential_score == 80  # 50 base + 30 capped compression


def test_liquidity_execution_drag_gate():
    """Verify that equities with 20-day ADV turnover < ₹5 Crore receive drag penalty and tier cap."""
    pattern = ConsolidationPattern(
        is_consolidating=True, resistance=105.0, support=100.0,
        range_span_pct=4.8, duration_bars=25, start_time=0, end_time=0, containment_ratio=0.95
    )
    ma_status = {
        "touching_ema50": False, "bouncing_ema50": False, "dist_ema50_pct": 0.0,
        "touching_avwap": False, "bouncing_avwap": False, "dist_avwap_pct": 0.0,
        "confluence_shelf": False, "confluence_level": None,
    }
    bar = CandleBar(time=1700000000, open=102.0, high=103.0, low=101.5, close=102.0, volume=5000)

    # Illiquid turnover: adv_20 = 10,000 shares * ₹102 = ₹10.2 Lakhs (< ₹5 Crore)
    comm_illiquid = synthesize_commentary_and_score(
        symbol="ILLIQUID_STOCK", current_bar=bar, consolidation=pattern,
        ma_status=ma_status, squeeze_pct=10.0, atr=2.0, ema50=102.0, sma50=102.0,
        sma200=102.0, avwap=102.0, weekly_trend="BULLISH", flow_result=None,
        adv_20=10000.0,  # ₹10.2 Lakhs turnover
    )
    # Liquid turnover: adv_20 = 1,000,000 shares * ₹102 = ₹10.2 Crore (>= ₹5 Crore)
    comm_liquid = synthesize_commentary_and_score(
        symbol="LIQUID_STOCK", current_bar=bar, consolidation=pattern,
        ma_status=ma_status, squeeze_pct=10.0, atr=2.0, ema50=102.0, sma50=102.0,
        sma200=102.0, avwap=102.0, weekly_trend="BULLISH", flow_result=None,
        adv_20=1000000.0,  # ₹10.2 Crore turnover
    )

    assert comm_illiquid.potential_score == comm_liquid.potential_score - 10
    assert comm_illiquid.quality_tier in ("B", "C")


def test_cache_date_deduplication_and_no_ghost_bars(tmp_path):
    """Verify that HistoricalCandleCache deduplicates candles by calendar date, keeping the latest bar."""
    from services.multi_tf_analysis import HistoricalCandleCache
    import tempfile

    db_file = tmp_path / "test_candles.sqlite"
    cache = HistoricalCandleCache(db_file)

    # Simulate two candles on the same date:
    # Bar 1 at 00:00:00 (timestamp 1700000000, price 170.0)
    # Bar 2 at 09:15:00 on same day (timestamp 1700033300, price 175.0)
    bar1 = CandleBar(time=1700000000, open=169.0, high=171.0, low=168.0, close=170.0, volume=1000)
    bar2 = CandleBar(time=1700033300, open=170.0, high=176.0, low=169.5, close=175.0, volume=2000)

    cache.save_candles("TEST_SYM", [bar1, bar2])
    retrieved = cache.get_candles("TEST_SYM")

    # Deduplication must return exactly 1 bar for the calendar day (the latest bar, close=175.0)
    assert len(retrieved) == 1
    assert retrieved[0].close == 175.0
    assert retrieved[0].time == 1700033300

    # Test overwrite flag
    new_bar = CandleBar(time=1700086400, open=175.0, high=180.0, low=174.0, close=178.0, volume=3000)
    cache.save_candles("TEST_SYM", [new_bar], overwrite=True)
    overwritten = cache.get_candles("TEST_SYM")
    assert len(overwritten) == 1
    assert overwritten[0].close == 178.0


def test_resample_weekly_and_monthly_anchoring():
    """Verify that resample_weekly and resample_monthly correctly anchor bars and aggregate forming session data."""
    dates = [
        datetime(2026, 9, 7, 9, 15, tzinfo=_IST),
        datetime(2026, 9, 8, 9, 15, tzinfo=_IST),
        datetime(2026, 9, 9, 9, 15, tzinfo=_IST),
        datetime(2026, 9, 10, 9, 15, tzinfo=_IST),
        datetime(2026, 9, 11, 9, 15, tzinfo=_IST),
    ]
    daily_bars = [
        CandleBar(time=int(dates[0].timestamp()), open=173.0, high=174.5, low=172.8, close=174.0, volume=1000),
        CandleBar(time=int(dates[1].timestamp()), open=174.0, high=175.5, low=173.8, close=175.0, volume=1200),
        CandleBar(time=int(dates[2].timestamp()), open=175.0, high=176.5, low=174.5, close=176.0, volume=1500),
        CandleBar(time=int(dates[3].timestamp()), open=176.0, high=177.0, low=172.5, close=173.5, volume=2000),
        CandleBar(time=int(dates[4].timestamp()), open=173.5, high=176.0, low=173.0, close=175.5, volume=2500),
    ]

    weekly = resample_weekly(daily_bars)
    assert len(weekly) == 1
    w_bar = weekly[0]
    # Timestamp must anchor to Monday 09:15 IST
    w_dt = datetime.fromtimestamp(w_bar.time, tz=_IST)
    assert w_dt.weekday() == 0  # Monday
    assert w_dt.year == 2026 and w_dt.month == 9 and w_dt.day == 7
    # Aggregated metrics: Open of Mon, Close of Fri, Max High, Min Low, Sum Vol
    assert w_bar.open == 173.0
    assert w_bar.close == 175.5  # Matches latest forming daily close
    assert w_bar.high == 177.0
    assert w_bar.low == 172.5
    assert w_bar.volume == 1000 + 1200 + 1500 + 2000 + 2500

    monthly = resample_monthly(daily_bars)
    assert len(monthly) == 1
    m_bar = monthly[0]
    # Timestamp must anchor to 1st of month 09:15 IST
    m_dt = datetime.fromtimestamp(m_bar.time, tz=_IST)
    assert m_dt.year == 2026 and m_dt.month == 9 and m_dt.day == 1
    # Aggregated metrics: Close matches latest daily close
    assert m_bar.close == 175.5
    assert m_bar.volume == 1000 + 1200 + 1500 + 2000 + 2500


def test_finbert_sentiment_integration_in_scoring():
    """Verify that FinBERT sentiment score and narrative alignment directly impact potential score."""
    bars = _generate_synthetic_bars(20)
    current_bar = bars[-1]
    consolidation = detect_consolidation(bars)
    ma_status = {
        "touching_ema50": False,
        "bouncing_ema50": False,
        "dist_ema50_pct": 2.0,
        "touching_avwap": False,
        "bouncing_avwap": False,
        "dist_avwap_pct": 2.0,
        "confluence_shelf": False,
        "confluence_level": None,
    }

    # Baseline with no sentiment (use weekly_trend="NEUTRAL" and sma200 > p to avoid ceiling clamp)
    base = synthesize_commentary_and_score(
        symbol="SENT_TEST", current_bar=current_bar, consolidation=consolidation,
        ma_status=ma_status, squeeze_pct=25.0, atr=2.0, ema50=100.0, sma50=100.0,
        sma200=110.0, avwap=100.0, weekly_trend="NEUTRAL"
    )

    # Bullish FinBERT (+60) + CONCORDANT_BULLISH (+8 + 5 = +13)
    bullish = synthesize_commentary_and_score(
        symbol="SENT_TEST", current_bar=current_bar, consolidation=consolidation,
        ma_status=ma_status, squeeze_pct=25.0, atr=2.0, ema50=100.0, sma50=100.0,
        sma200=110.0, avwap=100.0, weekly_trend="NEUTRAL",
        sentiment_detail={"sentiment_score": 60.0},
        narrative_alignment={"state": "CONCORDANT_BULLISH"}
    )
    assert bullish.potential_score == base.potential_score + 13

    # Bearish FinBERT (-50) + MEDIA_HYPE_TRAP (-8 - 5 = -13)
    bearish = synthesize_commentary_and_score(
        symbol="SENT_TEST", current_bar=current_bar, consolidation=consolidation,
        ma_status=ma_status, squeeze_pct=25.0, atr=2.0, ema50=100.0, sma50=100.0,
        sma200=110.0, avwap=100.0, weekly_trend="NEUTRAL",
        sentiment_detail={"sentiment_score": -50.0},
        narrative_alignment={"state": "MEDIA_HYPE_TRAP"}
    )
    assert bearish.potential_score == base.potential_score - 13


def test_neutral_chop_atr_levels():
    """Verify that NEUTRAL_CHOP uses dynamic ATR-based targets instead of fixed percentage levels."""
    bars = _generate_synthetic_bars(20)
    current_bar = bars[-1]
    # Wide non-consolidating range with price well inside
    loose_pattern = ConsolidationPattern(
        is_consolidating=False, resistance=110.0, support=90.0,
        range_span_pct=20.0, duration_bars=20, start_time=0, end_time=0, containment_ratio=0.5
    )
    ma_status = {
        "touching_ema50": False, "bouncing_ema50": False, "dist_ema50_pct": 5.0,
        "touching_avwap": False, "bouncing_avwap": False, "dist_avwap_pct": 5.0,
        "confluence_shelf": False, "confluence_level": None,
    }
    atr = 4.0
    comm = synthesize_commentary_and_score(
        symbol="CHOP_TEST", current_bar=current_bar, consolidation=loose_pattern,
        ma_status=ma_status, squeeze_pct=50.0, atr=atr, ema50=100.0, sma50=100.0,
        sma200=95.0, avwap=100.0, weekly_trend="NEUTRAL"
    )
    assert comm.bias == "NEUTRAL_CHOP"
    assert comm.trigger_price == 110.0
    # target1 = res + (atr * 1.5) = 110.0 + 6.0 = 116.0 (not 110 * 1.02 = 112.2)
    assert comm.target_price_1 == round(110.0 + (4.0 * 1.5), 2)
    # target2 = res + (atr * 3.0) = 110.0 + 12.0 = 122.0 (not 110 * 1.05 = 115.5)
    assert comm.target_price_2 == round(110.0 + (4.0 * 3.0), 2)
    assert comm.stop_loss == 90.0


def test_candle_pattern_bounce_confirmation():
    """Verify that a Bullish Hammer at 50-EMA support grants a +5 confirmation bonus."""
    bars = _generate_synthetic_bars(20)
    current_bar = bars[-1]
    consolidation = detect_consolidation(bars)
    ma_status_bounce = {
        "touching_ema50": True,
        "bouncing_ema50": True,
        "dist_ema50_pct": 0.2,
        "touching_avwap": False,
        "bouncing_avwap": False,
        "dist_avwap_pct": 2.0,
        "confluence_shelf": False,
        "confluence_level": None,
    }

    # Use squeeze_pct=50.0 (no squeeze bonus) so baseline is 90 and adding +5 gives 95 without hitting 98 ceiling
    base = synthesize_commentary_and_score(
        symbol="HAMMER_TEST", current_bar=current_bar, consolidation=consolidation,
        ma_status=ma_status_bounce, squeeze_pct=50.0, atr=2.0, ema50=current_bar.close,
        sma50=current_bar.close, sma200=current_bar.close * 1.2, avwap=current_bar.close,
        weekly_trend="NEUTRAL", candle_pattern=None
    )

    with_hammer = synthesize_commentary_and_score(
        symbol="HAMMER_TEST", current_bar=current_bar, consolidation=consolidation,
        ma_status=ma_status_bounce, squeeze_pct=50.0, atr=2.0, ema50=current_bar.close,
        sma50=current_bar.close, sma200=current_bar.close * 1.2, avwap=current_bar.close,
        weekly_trend="NEUTRAL", candle_pattern="Bullish Hammer Rejection"
    )

    assert with_hammer.potential_score == base.potential_score + 5



