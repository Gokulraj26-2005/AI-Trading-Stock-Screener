from services.live_trigger_state import LiveTriggerStateStore


def test_trigger_confirmation_uses_observed_time_and_persists(tmp_path):
    store = LiveTriggerStateStore(tmp_path / "trigger.sqlite")
    spec = {"symbol": "RELIANCE", "trigger_price": 100.0, "stop_loss": 95.0}

    first = store.observe(spec, 99.0, observed_at=1_000.0)
    assert first["status"] == "WATCHING"
    pending = store.observe(spec, 100.5, observed_at=1_001.0)
    assert pending["status"] == "TRIGGER_PENDING"
    confirmed = store.observe(spec, 100.5, observed_at=1_121.0)
    assert confirmed["status"] == "CONFIRMED_TRIGGER"
    assert confirmed["event"]["sequence_id"] > 0

    restarted = LiveTriggerStateStore(tmp_path / "trigger.sqlite")
    persisted = restarted.get_state("reliance")
    assert persisted is not None
    assert persisted["status"] == "CONFIRMED_TRIGGER"
    assert persisted["correlation_id"] == confirmed["correlation_id"]
    state = restarted.observe(spec, 100.5, observed_at=1_122.0)
    assert state["status"] == "CONFIRMED_TRIGGER"
    assert len(restarted.events_after(0)) == 3
    assert restarted.events_after(2)[0]["event_id"] == confirmed["event"]["event_id"]


def test_trigger_events_have_stable_cursor_and_correlation_id(tmp_path):
    store = LiveTriggerStateStore(tmp_path / "trigger.sqlite")
    state = store.observe(
        {"symbol": "TCS", "trigger_price": 200.0, "correlation_id": "corr-123"},
        201.0,
        observed_at=2_000.0,
    )
    event = state["event"]
    assert event["correlation_id"] == "corr-123"
    assert event["sequence_id"] == 1
    assert store.events_after(event["sequence_id"]) == []


def test_micro_retest_excursion_counting_and_dwell_pausing(tmp_path):
    """Flaws 1 & 3: Multiple polls during a single micro-retest only increment slip_count once,
    and timer does not advance while below trigger."""
    store = LiveTriggerStateStore(tmp_path / "trigger.sqlite")
    spec = {"symbol": "INFY", "trigger_price": 1000.0, "stop_loss": 980.0}

    # Initial trigger cross at t=100.0
    s1 = store.observe(spec, 1001.0, observed_at=100.0)
    assert s1["status"] == "TRIGGER_PENDING"
    assert s1["slip_count"] == 0

    # Advance 10s above trigger -> t=110.0
    s2 = store.observe(spec, 1002.0, observed_at=110.0)
    assert s2["sustain_seconds"] == 10.0
    assert s2["slip_count"] == 0

    # Pullback to 999.0 (-0.10%, inside 0.15% tolerance) -> excursion 1 starts at t=113.0
    s3 = store.observe(spec, 999.0, observed_at=113.0)
    assert s3["status"] == "TRIGGER_PENDING"
    assert s3["slip_count"] == 1
    # Sustain timer is PAUSED during retest
    assert s3["sustain_seconds"] == 10.0

    # Second poll in same pullback at t=116.0 (still at 999.0) -> slip_count must NOT increment again!
    s4 = store.observe(spec, 999.0, observed_at=116.0)
    assert s4["slip_count"] == 1  # Key fix for Flaw 3!
    assert s4["sustain_seconds"] == 10.0

    # Third poll in same pullback at t=119.0 (still at 999.0) -> slip_count remains 1
    s5 = store.observe(spec, 999.0, observed_at=119.0)
    assert s5["slip_count"] == 1

    # Price re-crosses above trigger at t=122.0 (excursion 1 ends)
    s6 = store.observe(spec, 1001.0, observed_at=122.0)
    assert s6["slip_count"] == 1
    assert s6["status"] == "TRIGGER_PENDING"

    # Second pullback excursion starts at t=130.0 -> slip_count increments to 2
    s7 = store.observe(spec, 999.2, observed_at=130.0)
    assert s7["slip_count"] == 2

    # Third pullback excursion starts at t=150.0 (after recovering) -> exceeds MAX_SLIPS (2) -> reset to WATCHING
    store.observe(spec, 1001.0, observed_at=140.0)
    s8 = store.observe(spec, 999.0, observed_at=150.0)
    assert s8["status"] == "WATCHING"
    assert s8["slip_count"] == 0


def test_cannot_confirm_while_in_retest_below_trigger(tmp_path):
    """Flaw 3 Case B: Confirmation cannot trigger while price is currently below trigger."""
    store = LiveTriggerStateStore(tmp_path / "trigger.sqlite")
    spec = {"symbol": "SBIN", "trigger_price": 500.0, "stop_loss": 490.0}

    store.observe(spec, 501.0, observed_at=100.0)
    # Sustain for 120s above trigger
    store.observe(spec, 502.0, observed_at=220.0)

    # At second 221, price pulls back to 499.50 (-0.10% below trigger)
    s_pullback = store.observe(spec, 499.5, observed_at=221.0)
    # Must remain TRIGGER_PENDING because it cannot confirm while below trigger
    assert s_pullback["status"] == "TRIGGER_PENDING"

    # Once price reclaims trigger, it confirms immediately!
    s_confirm = store.observe(spec, 500.5, observed_at=222.0)
    assert s_confirm["status"] == "CONFIRMED_TRIGGER"


def test_max_poll_gap_guards_against_browser_sleep(tmp_path):
    """Flaw 2: A sleeping/minimized browser tab gap is not credited to sustain time."""
    store = LiveTriggerStateStore(tmp_path / "trigger.sqlite")
    spec = {"symbol": "HDFC", "trigger_price": 1500.0, "max_poll_gap_seconds": 15.0}

    store.observe(spec, 1501.0, observed_at=100.0)
    # Browser was throttled/slept for 90 seconds (gap > 15s)
    s_wake = store.observe(spec, 1502.0, observed_at=190.0)
    # Gap was 90s (> 15s max_poll_gap), so effective_dt = 0
    assert s_wake["sustain_seconds"] == 0.0
    assert s_wake["status"] == "TRIGGER_PENDING"

