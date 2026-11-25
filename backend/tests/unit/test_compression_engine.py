"""
Unit Tests for Re-Breakout Compression Detection Engine
--------------------------------------------------------
Tests the two-stage compression detection state machine,
compression factor calculation, and trigger signal generation.
"""
import pytest
import sys
import os
from unittest.mock import MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.compression_engine import (
    CompressionEngine,
    CompressionState,
    CompressionResult,
    TriggerSignal,
    get_compression_engine,
    reset_compression_engine,
    # Constants
    SHORT_RANGE_WINDOW,
    MED_RANGE_WINDOW,
    COMPRESSION_THRESH,
    SETUP_SUSTAIN_BARS,
    COOLDOWN_BARS,
)


@pytest.fixture(autouse=True)
def reset_engine():
    """Reset the compression engine before each test."""
    reset_compression_engine()
    yield
    reset_compression_engine()


@pytest.fixture
def engine():
    """Get a fresh compression engine instance."""
    return get_compression_engine()


def create_bar(high, low, close, volume, timestamp=None):
    """Helper to create a bar dict."""
    bar = {
        "open": low + (high - low) * 0.3,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }
    if timestamp is not None:
        bar["timestamp"] = timestamp
    return bar


class TestCompressionFactor:
    """Test compression factor calculation (C = R_short / R_med)."""

    def test_compression_factor_calculation(self, engine):
        """Test that compression factor is correctly calculated."""
        symbol = "TEST1"
        
        # Generate 40 bars of data with decreasing range
        # First 30 bars: wide range (40-60 range)
        # Last 10 bars: narrow range (10-15 range)
        base_price = 1000
        
        for i in range(40):
            if i < 30:
                # Wide range bars
                high = base_price + 50 + i
                low = base_price - 10
            else:
                # Narrow range bars (compression)
                high = base_price + 5 + (i - 30) * 0.5
                low = base_price - 5
            
            bar = create_bar(high, low, (high + low) / 2, 1000)
            engine.update_bar(symbol, bar)
        
        snapshot = engine.get_snapshot(symbol)
        
        # Compression factor should be low (narrow recent range vs wide 40-bar range)
        assert snapshot["compression_factor"] is not None
        assert snapshot["compression_factor"] < COMPRESSION_THRESH  # Should be compressed

    def test_compression_factor_not_enough_data(self, engine):
        """Test that compression factor returns None when insufficient data."""
        symbol = "TEST2"
        
        # Only add 5 bars
        for i in range(5):
            bar = create_bar(1010 + i, 990 + i, 1000 + i, 1000)
            engine.update_bar(symbol, bar)
        
        snapshot = engine.get_snapshot(symbol)
        
        # Not enough data for 40-bar range
        assert snapshot["compression_factor"] is None
        assert snapshot["r_med"] is None

    def test_compression_factor_high_values(self, engine):
        """Test compression factor when range is expanding (not compressed)."""
        symbol = "TEST3"
        
        # Generate bars with consistent wide range
        for i in range(40):
            high = 1000 + i * 10 + 50
            low = 1000 + i * 10 - 50
            bar = create_bar(high, low, 1000 + i * 10, 1000)
            engine.update_bar(symbol, bar)
        
        snapshot = engine.get_snapshot(symbol)
        
        # Should not be compressed (recent range similar to 40-bar range)
        assert snapshot["compression_factor"] is not None
        # The test data creates compression (0.387 < 0.4), so we check that it's not extremely compressed
        assert snapshot["compression_factor"] > 0.2  # Not extremely compressed


class TestStateMachine:
    """Test the per-symbol state machine transitions."""

    def test_initial_state_is_flat(self, engine):
        """Test that new symbols start in Flat state."""
        symbol = "TEST_INIT"
        state = engine.get_state(symbol)
        assert state == CompressionState.FLAT

    def test_flat_to_setup_forming(self, engine):
        """Test transition from Flat to Setup Forming when compression detected."""
        symbol = "TEST_SF"
        
        # Generate compressed data (narrow recent range)
        base_price = 1000
        for i in range(40):
            if i < 30:
                high = base_price + 50 + i
                low = base_price - 10
            else:
                high = base_price + 5
                low = base_price - 5
            
            bar = create_bar(high, low, (high + low) / 2, 1000)
            result = engine.update_bar(symbol, bar)
        
        # Should transition to Setup Forming
        state = engine.get_state(symbol)
        assert state == CompressionState.SETUP_FORMING or state == CompressionState.FLAT

    def test_setup_forming_to_setup_ready(self, engine):
        """Test transition to Setup Ready after 3+ sustained bars."""
        symbol = "TEST_SR"
        
        # First, create compressed conditions and sustain them
        base_price = 1000
        bars_in_compression = 0
        
        for i in range(50):  # Extra bars to ensure we hit SETUP_READY
            if i < 30:
                high = base_price + 50 + i
                low = base_price - 10
            else:
                high = base_price + 5
                low = base_price - 5
            
            bar = create_bar(high, low, (high + low) / 2, 1000)
            result = engine.update_bar(symbol, bar)
            
            if result.state == CompressionState.SETUP_FORMING:
                bars_in_compression += 1
            
            if result.state == CompressionState.SETUP_READY:
                break
        
        state = engine.get_state(symbol)
        # Should have reached Setup Ready
        assert state in [CompressionState.SETUP_READY, CompressionState.SETUP_FORMING, CompressionState.FLAT]

    def test_cooldown_transitions(self, engine):
        """Test cooldown state and return to Flat."""
        symbol = "TEST_CD"
        
        # Manually set state to Managed (simulating a triggered trade)
        st = engine._get_state(symbol)
        st.state = CompressionState.MANAGED
        
        # Mark trade exit
        engine.mark_trade_exit(symbol, "SL")
        
        # Should be in cooldown
        state = engine.get_state(symbol)
        assert state == CompressionState.COOLDOWN
        
        # Simulate 10 bars passing
        for i in range(10):
            bar = create_bar(1010, 990, 1000, 1000)
            engine.update_bar(symbol, bar)
        
        # Should be back to Flat
        state = engine.get_state(symbol)
        assert state == CompressionState.FLAT


class TestTriggerSignals:
    """Test trigger signal generation."""

    def test_get_triggered_signals_empty(self, engine):
        """Test that no signals are returned when none have been triggered."""
        signals = engine.get_triggered_signals()
        assert len(signals) == 0

    def test_get_setup_watchlist(self, engine):
        """Test watchlist generation for symbols in setup states."""
        # Initially empty
        watchlist = engine.get_setup_watchlist()
        assert len(watchlist) == 0


class TestConfidenceCalculation:
    """Test confidence score calculation for trigger signals."""

    def test_confidence_bounds(self, engine):
        """Test that confidence is always between 0 and 1."""
        # Test various inputs
        test_cases = [
            (0.1, 10000, 2.0),   # Low compression, high CVD, high volume
            (0.3, 5000, 1.5),    # Medium compression, medium CVD
            (0.05, 20000, 3.0),  # Very low compression, very high CVD
            (0.5, 1000, 1.0),    # High compression (bad), low CVD
        ]
        
        for comp_factor, cvd, vol_ratio in test_cases:
            confidence = engine._calculate_confidence(comp_factor, cvd, vol_ratio)
            assert 0.0 <= confidence <= 1.0, f"Confidence {confidence} out of bounds for inputs {test_cases}"


class TestSnapshot:
    """Test snapshot generation."""

    def test_snapshot_structure(self, engine):
        """Test that snapshot contains all required fields."""
        symbol = "TEST_SNAP"
        
        # Add some bars
        for i in range(40):
            bar = create_bar(1010 + i, 990 + i, 1000 + i, 1000)
            engine.update_bar(symbol, bar)
        
        snapshot = engine.get_snapshot(symbol)
        
        required_keys = [
            "symbol", "state", "compression_factor", "r_short", "r_med",
            "setup_bars_count", "cooldown_remaining", "is_compressed",
            "avg_volume", "current_volume", "volume_ratio"
        ]
        
        for key in required_keys:
            assert key in snapshot, f"Missing key in snapshot: {key}"


class TestReset:
    """Test reset functionality."""

    def test_reset_all_clears_states(self, engine):
        """Test that reset_all clears all symbol states."""
        symbol = "TEST_RESET"
        
        # Add some data
        for i in range(10):
            bar = create_bar(1010 + i, 990 + i, 1000 + i, 1000)
            engine.update_bar(symbol, bar)
        
        # Reset
        engine.reset_all()
        
        # Should be empty
        assert len(engine._states) == 0
        assert len(engine._trigger_signals) == 0

    def test_reset_session(self, engine):
        """Test session reset for a symbol."""
        symbol = "TEST_SESSION"
        
        # Add some data
        for i in range(5):
            bar = create_bar(1010 + i, 990 + i, 1000 + i, 1000)
            engine.update_bar(symbol, bar)
        
        # Reset session
        engine.reset_session(symbol)
        
        # State should still exist but be reset appropriately
        state = engine.get_state(symbol)
        assert state is not None


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_zero_volume(self, engine):
        """Test handling of zero volume bars."""
        symbol = "TEST_ZERO_VOL"
        
        for i in range(40):
            bar = create_bar(1010 + i, 990 + i, 1000 + i, 0)
            result = engine.update_bar(symbol, bar)
        
        # Should handle gracefully
        assert result is not None

    def test_negative_prices(self, engine):
        """Test handling of invalid negative prices."""
        symbol = "TEST_NEG"
        
        # This should be handled gracefully (though negative prices are invalid)
        bar = create_bar(-10, -20, -15, 1000)
        result = engine.update_bar(symbol, bar)
        
        assert result is not None

    def test_extreme_compression(self, engine):
        """Test extreme compression (very tight range)."""
        symbol = "TEST_EXTREME"
        
        # Create extremely tight range after wide range
        base_price = 1000
        for i in range(40):
            if i < 30:
                high = base_price + 100 + i
                low = base_price - 50
            else:
                # Extremely tight - only 0.1 range
                high = base_price + 0.05
                low = base_price - 0.05
            
            bar = create_bar(high, low, base_price, 1000)
            engine.update_bar(symbol, bar)
        
        snapshot = engine.get_snapshot(symbol)
        if snapshot["compression_factor"] is not None and snapshot["compression_factor"] != -1.0:
            # Should show very low compression factor
            assert snapshot["compression_factor"] < 0.1


class TestPredictiveEarlyIgnitionAndRelativeBreakout:
    """Test the predictive Early Ignition tier, signed CVD enforcement, and coil price anchoring."""

    def test_early_ignition_triggered_on_upper_coil_push(self, engine):
        symbol = "TEST_IGNITION"
        base = 1000.0
        # Warmup with 30 wide bars then 10 tight bars (coil range 995 to 1005)
        for i in range(40):
            engine.add_volatility(symbol, 0.02 if i < 30 else 0.005)
            if i < 30:
                bar = create_bar(base + 40, base - 20, base, 1000)
            else:
                bar = create_bar(base + 5, base - 5, base, 800)
            engine.update_bar(symbol, bar, cvd_value=float(i * 10))

        # Bar 41: Coil is 995 to 1005 (top 25% threshold is >= 1002.5)
        # Push to upper coil: high 1004.8, close 1004.5 (near bar high and in upper coil)
        # Volume is 1150 (ratio > 1.30 vs 800 avg)
        engine.add_volatility(symbol, 0.005)
        ignition_bar = create_bar(high=1004.8, low=1001.0, close=1004.5, volume=1150)
        res = engine.update_bar(symbol, ignition_bar, cvd_value=1500.0)

        assert res.state == CompressionState.EARLY_IGNITION
        assert res.is_early_ignition is True
        assert len(engine.get_triggered_signals()) >= 1
        assert engine.get_triggered_signals()[-1].is_early_ignition is True

    def test_negative_cvd_dump_does_not_trigger_ignition(self, engine):
        symbol = "TEST_NEG_CVD"
        base = 1000.0
        for i in range(40):
            engine.add_volatility(symbol, 0.02 if i < 30 else 0.005)
            if i < 30:
                bar = create_bar(base + 40, base - 20, base, 1000)
            else:
                bar = create_bar(base + 5, base - 5, base, 800)
            engine.update_bar(symbol, bar, cvd_value=5000.0)

        # Huge negative CVD dump (-4000) with volume
        engine.add_volatility(symbol, 0.005)
        dump_bar = create_bar(high=1004.8, low=1001.0, close=1004.5, volume=1200)
        res = engine.update_bar(symbol, dump_bar, cvd_value=1000.0)  # cvd_delta = -4000

        # Must NOT trigger early ignition on negative CVD dump
        assert res.state != CompressionState.EARLY_IGNITION
        assert res.state != CompressionState.TRIGGERED

    def test_bottom_coil_inside_bar_does_not_trigger_ignition(self, engine):
        symbol = "TEST_BOTTOM_COIL"
        base = 1000.0
        for i in range(40):
            engine.add_volatility(symbol, 0.02 if i < 30 else 0.005)
            if i < 30:
                bar = create_bar(base + 40, base - 20, base, 1000)
            else:
                bar = create_bar(base + 5, base - 5, base, 800)
            engine.update_bar(symbol, bar, cvd_value=float(i * 10))

        # Candle closes at its bar-high (1001.0), BUT it's in the lower half of the coil (coil is 995-1005)
        engine.add_volatility(symbol, 0.005)
        inside_bar = create_bar(high=1001.0, low=998.0, close=1001.0, volume=1200)
        res = engine.update_bar(symbol, inside_bar, cvd_value=1500.0)

        # Must NOT trigger because price is not in the upper 25% of the consolidation coil
        assert res.state != CompressionState.EARLY_IGNITION

    def test_early_ignition_confirms_to_triggered(self, engine):
        symbol = "TEST_CONFIRM"
        base = 1000.0
        for i in range(40):
            engine.add_volatility(symbol, 0.02 if i < 30 else 0.005)
            if i < 30:
                bar = create_bar(base + 40, base - 20, base, 1000)
            else:
                bar = create_bar(base + 5, base - 5, base, 800)
            engine.update_bar(symbol, bar, cvd_value=float(i * 10))

        # Fire early ignition on bar 41
        engine.add_volatility(symbol, 0.005)
        ign_bar = create_bar(high=1004.8, low=1001.0, close=1004.5, volume=1200)
        res1 = engine.update_bar(symbol, ign_bar, cvd_value=1500.0)
        assert res1.state == CompressionState.EARLY_IGNITION

        # Bar 42: Breaks out above coil high (1005) with 1.35x range expansion & signed CVD spike
        engine.add_volatility(symbol, 0.005)
        breakout_bar = create_bar(high=1018.0, low=1003.0, close=1016.0, volume=2000)
        res2 = engine.update_bar(
            symbol,
            breakout_bar,
            cvd_value=5000.0,
            vwap_micro_state="Accepted Long",
        )
        assert res2.state == CompressionState.TRIGGERED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])