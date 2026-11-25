import pytest
from app.services.institutional_flow_engine import (
    MarketSnapshot,
    InstitutionalFlowEngine,
    get_institutional_flow_engine,
)


def test_non_fno_dynamic_weight_rescaling():
    engine = InstitutionalFlowEngine()
    
    # Non-F&O equity snapshot
    snapshot = MarketSnapshot(
        symbol="PKTEA",  # pure cash smallcap
        price=100.0,
        volume=250000,
        adv_20=200000,
        volume_median_20=190000,
        volumes_20=[180000, 190000, 210000, 250000] * 5,
        daily_closes_20=[95.0 + (i * 0.25) for i in range(20)],
        daily_highs_20=[96.0 + (i * 0.25) for i in range(20)],
        daily_lows_20=[94.5 + (i * 0.25) for i in range(20)],
        daily_opens_20=[95.0 + (i * 0.25) for i in range(20)],
        is_fno_eligible=False,  # Non-F&O
        futures_oi=None,
        futures_oi_change_pct=None,
        delivery_pct=None,  # Delivery missing
        avwap_month=98.0,
        ema_50=97.0,
        support_level=96.0,
        resistance_level=102.0,
    )

    result = engine.compute_flow(snapshot)
    
    # 1. Non-F&O must have None for fno_oi component
    assert result.components["fno_oi"] is None
    # 2. Missing delivery must have None
    assert result.components["delivery"] is None
    # 3. Available weights were Volume (15) + CVD (20) + Large Trades (15) + Depth (10) + AVWAP (10) = 70%
    assert result.coverage == 70
    # 4. Result score must be in valid range [-100, 100]
    assert -100 <= result.score <= 100
    # 5. Non-F&O should have valid confidence
    assert 20 <= result.confidence <= 98
    print(f"Non-F&O Test Passed: Score={result.score}, Conf={result.confidence}%, Coverage={result.coverage}%")


def test_fno_equity_with_long_buildup():
    engine = InstitutionalFlowEngine()
    
    snapshot = MarketSnapshot(
        symbol="RELIANCE",  # Major F&O stock
        price=1400.0,
        volume=5000000,
        adv_20=3000000,
        volume_median_20=2900000,
        volumes_20=[2800000, 3000000, 3200000, 5000000] * 5,
        daily_closes_20=[1300.0 + (i * 5.0) for i in range(20)],
        daily_highs_20=[1305.0 + (i * 5.0) for i in range(20)],
        daily_lows_20=[1295.0 + (i * 5.0) for i in range(20)],
        daily_opens_20=[1300.0 + (i * 5.0) for i in range(20)],
        is_fno_eligible=True,
        futures_oi=15000000,
        futures_oi_change_pct=6.5,  # Price Up + OI Up -> Long Build Up
        delivery_pct=58.0,
        delivery_history=[40.0, 42.0, 41.0, 45.0] * 5,
        avwap_month=1350.0,
        ema_50=1340.0,
        support_level=1360.0,
        resistance_level=1420.0,
    )

    result = engine.compute_flow(snapshot)
    
    # 1. F&O component should be evaluated and strongly positive
    assert result.components["fno_oi"] is not None
    assert result.components["fno_oi"] >= 60
    # 2. Delivery should be evaluated
    assert result.components["delivery"] is not None
    # 3. 100% coverage
    assert result.coverage == 100
    # 4. Overall score should be in accumulation territory
    assert result.score >= 50
    assert result.classification in ("ACCUMULATION", "STRONG_ACCUMULATION")
    print(f"F&O Test Passed: Score={result.score}, Classification={result.classification}, Coverage={result.coverage}%")


def test_stealth_accumulation_detection():
    engine = InstitutionalFlowEngine()
    
    # Elevated volume and positive CVD, but tight sideways price over 5 sessions (range <= 2.5%)
    closes = [100.0, 100.2, 99.8, 100.1, 100.3] * 4
    snapshot = MarketSnapshot(
        symbol="COIL_STOCK",
        price=100.3,
        volume=800000,
        adv_20=400000,
        volume_median_20=380000,
        volumes_20=[400000] * 19 + [800000],
        daily_closes_20=closes,
        daily_highs_20=[c + 0.05 for c in closes],  # closes right near the top of the candle
        daily_lows_20=[c - 0.5 for c in closes],    # long lower wick (absorption)
        daily_opens_20=[c - 0.2 for c in closes],
        is_fno_eligible=False,
        avwap_month=100.0,
        ema_50=99.5,
    )

    result = engine.compute_flow(snapshot)
    assert result.stealth_phase == "STEALTH_ACCUMULATION"
    assert "⚡ Stealth Accumulation In Progress" in result.signals
    print(f"Stealth Accumulation Test Passed: Stealth Phase={result.stealth_phase}")


def test_conflict_detection_technical_breakout_vs_distribution():
    engine = InstitutionalFlowEngine()
    
    # Price trying to break out, but massive volume dumping (closes near low)
    closes = [100.0] * 19 + [99.0]
    highs = [101.0] * 19 + [104.0]  # spiked up to 104 then dumped to 99
    lows = [99.5] * 19 + [98.5]
    opens = [100.0] * 19 + [103.5]
    
    snapshot = MarketSnapshot(
        symbol="TRAP_STOCK",
        price=99.0,
        volume=2000000,
        adv_20=500000,
        volume_median_20=480000,
        volumes_20=[500000] * 19 + [2000000],
        daily_closes_20=closes,
        daily_highs_20=highs,
        daily_lows_20=lows,
        daily_opens_20=opens,
        is_fno_eligible=False,
        technical_bias="BULLISH_BREAKOUT",
        avwap_month=102.0,
        ema_50=101.0,
    )

    result = engine.compute_flow(snapshot)
    assert result.score <= -30
    assert len(result.conflicts) > 0
    assert any("Technical Bullish Breakout vs Institutional Supply Overhang" in c for c in result.conflicts)
    print(f"Conflict Detection Test Passed: Conflicts={result.conflicts}")


def test_doji_absorption_floor_defense():
    """Verify zero-body Doji candles do not trigger spurious +45 support absorption bonuses."""
    engine = InstitutionalFlowEngine()

    # Exact Doji on support: open == close == 100.0, tiny wick down to 99.96
    # support_level = 100.0
    snapshot = MarketSnapshot(
        symbol="DOJI_STOCK",
        price=100.0,
        volume=100000,
        adv_20=100000,
        volume_median_20=100000,
        volumes_20=[100000] * 20,
        daily_closes_20=[100.0] * 19 + [100.0],
        daily_highs_20=[100.10] * 19 + [100.04],
        daily_lows_20=[99.90] * 19 + [99.96],  # 4 paise lower wick
        daily_opens_20=[100.0] * 19 + [100.0],  # zero body!
        is_fno_eligible=False,
        support_level=100.0,
        resistance_level=105.0,
    )

    depth_factor = engine.compute_depth_and_absorption(snapshot)
    # The depth factor score must NOT have received the +45 absorption bonus on a 4-paise Doji wick
    assert depth_factor.score < 45, f"Expected depth score < 45 on Doji, got {depth_factor.score}"


def test_volume_dryup_accumulation():
    """Verify that low volume (<0.5x median) with price holding near highs (>0.55 close pos) signals supply dry-up."""
    engine = InstitutionalFlowEngine()

    snapshot = MarketSnapshot(
        symbol="DRYUP_STOCK",
        price=104.5,
        volume=30000,  # 0.3x median of 100k
        adv_20=100000,
        volume_median_20=100000,
        volumes_20=[100000] * 20,
        daily_closes_20=[100.0] * 19 + [104.5],
        daily_highs_20=[101.0] * 19 + [105.0],
        daily_lows_20=[99.0] * 19 + [103.0],
        daily_opens_20=[100.0] * 19 + [103.5],
        is_fno_eligible=False,
    )

    factor = engine.compute_volume_participation(snapshot)
    assert factor.score >= 30
    assert "Low-Vol Supply Dry-Up" in factor.detail
