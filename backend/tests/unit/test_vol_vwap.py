"""
Unit tests for Advanced VWAP Engine (_classify_state, compound liquidity gate, CVD exhaustion).
"""
import pytest
from app.app_config import QUANT_CONFIG
from services.vol_vwap import VolVWAPEngine


def test_classify_state_compound_liquidity_gate():
    """Verify that 3σ mean reversion requires BOTH share volume AND turnover floors."""
    engine = VolVWAPEngine()

    # Test 1: Thin volume stock (400 shares < 500 min) at 3σ with 0 CVD
    # Must NOT fire mean_rev_flag despite perfect exhaustion ratio
    state, score, mean_rev, chop = engine._classify_state(
        dt=3.2,
        vwap_std=1.0,
        vol_state="normal",
        cvd=0.0,
        close_price=50.0,   # turnover = 400 * 50 = ₹20,000 (< ₹25,000 floor)
        bar_vol=400.0,
    )
    assert mean_rev is False, "Mean reversion flag must not trigger on sub-500 volume bar"

    # Test 2: Low turnover penny stock (2000 shares at ₹5 = ₹10,000 turnover < ₹25,000 floor)
    state, score, mean_rev, chop = engine._classify_state(
        dt=3.2,
        vwap_std=1.0,
        vol_state="normal",
        cvd=0.0,
        close_price=5.0,
        bar_vol=2000.0,
    )
    assert mean_rev is False, "Mean reversion flag must not trigger on sub-₹25,000 turnover bar"

    # Test 3: Liquid bar passing both gates (1000 shares at ₹50 = ₹50,000 turnover) with CVD exhausted
    # cvd_ratio = 50 / 1000 = 0.05 < 0.08 max threshold
    state, score, mean_rev, chop = engine._classify_state(
        dt=3.2,
        vwap_std=1.0,
        vol_state="normal",
        cvd=50.0,
        close_price=50.0,
        bar_vol=1000.0,
    )
    assert mean_rev is True, "Mean reversion flag must trigger when liquid and CVD ratio < 0.08"

    # Test 4: Liquid bar with expanding CVD (cvd_ratio = 200 / 1000 = 0.20 > 0.08 max)
    # Not exhausted -> mean_rev_flag is False
    state, score, mean_rev, chop = engine._classify_state(
        dt=3.2,
        vwap_std=1.0,
        vol_state="normal",
        cvd=200.0,
        close_price=50.0,
        bar_vol=1000.0,
    )
    assert mean_rev is False, "Expanding CVD must prevent false mean reversion flag"
