"""Durable, event-based trigger lifecycle state for live analysis."""

from __future__ import annotations

import sqlite3
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from logger import logger
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "live_trigger_state.sqlite"
SUSTAIN_SECONDS = 120.0
RETEST_TOLERANCE_PCT = 0.0015
MAX_SLIPS = 2


class LiveTriggerStateStore:
    """Persists trigger observations and emits events only on lifecycle changes."""

    def __init__(self, db_path: Path | str = DB_PATH) -> None:
        self.db_path = db_path
        if str(db_path) != ":memory:" and isinstance(db_path, Path):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path, timeout=15.0) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trigger_states (
                    symbol TEXT PRIMARY KEY,
                    direction TEXT NOT NULL,
                    trigger_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    target_1 REAL,
                    target_2 REAL,
                    status TEXT NOT NULL,
                    first_qualified_at REAL,
                    last_observed_at REAL,
                    slip_count INTEGER NOT NULL DEFAULT 0,
                    tick_count INTEGER NOT NULL DEFAULT 0,
                    accumulated_sustain_seconds REAL NOT NULL DEFAULT 0.0,
                    in_retest INTEGER NOT NULL DEFAULT 0,
                    retest_dwell_seconds REAL NOT NULL DEFAULT 0.0,
                    correlation_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Migrate columns if existing database table lacks them
            existing_cols = {col[1] for col in conn.execute("PRAGMA table_info(trigger_states)").fetchall()}
            for col_name, col_type in [
                ("tick_count", "INTEGER NOT NULL DEFAULT 0"),
                ("accumulated_sustain_seconds", "REAL NOT NULL DEFAULT 0.0"),
                ("in_retest", "INTEGER NOT NULL DEFAULT 0"),
                ("retest_dwell_seconds", "REAL NOT NULL DEFAULT 0.0"),
            ]:
                if col_name not in existing_cols:
                    try:
                        conn.execute(f"ALTER TABLE trigger_states ADD COLUMN {col_name} {col_type}")
                    except sqlite3.OperationalError:
                        pass
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trigger_events (
                    sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    symbol TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    correlation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trigger_events_symbol ON trigger_events(symbol, sequence_id)")
            conn.commit()

    @staticmethod
    def _now() -> float:
        return datetime.now(timezone.utc).timestamp()

    def observe(self, spec: Dict[str, Any], ltp: float, observed_at: Optional[float] = None) -> Dict[str, Any]:
        symbol = str(spec["symbol"]).upper().strip()
        direction = "BEAR" if str(spec.get("direction", "BULL")).upper() == "BEAR" else "BULL"
        trigger = float(spec["trigger_price"])
        stop_loss = float(spec.get("stop_loss") or 0.0)
        now = float(observed_at or self._now())
        correlation_id = str(spec.get("correlation_id") or uuid.uuid4())
        max_poll_gap = float(spec.get("max_poll_gap_seconds", 0.0))
        min_ticks = int(spec.get("min_ticks", 1))
        max_retest_dwell = float(spec.get("max_retest_dwell_seconds", 30.0))

        qualifies = ltp <= trigger if direction == "BEAR" else ltp >= trigger
        status = "WATCHING"

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM trigger_states WHERE symbol = ?", (symbol,)).fetchone()
            previous = dict(row) if row else None
            first_qualified = previous.get("first_qualified_at") if previous else None
            last_observed = previous.get("last_observed_at") if previous else None
            slips = int(previous.get("slip_count", 0)) if previous else 0
            tick_count = int(previous.get("tick_count", 0)) if previous else 0
            accumulated_sustain = float(previous.get("accumulated_sustain_seconds", 0.0)) if previous else 0.0
            in_retest = int(previous.get("in_retest", 0)) if previous else 0
            retest_dwell = float(previous.get("retest_dwell_seconds", 0.0)) if previous else 0.0
            first_observation = previous is None

            # Calculate time delta from last observation
            dt = max(0.0, now - float(last_observed)) if last_observed is not None else 0.0
            # Guard against dormant intervals / throttled browser gaps
            effective_dt = 0.0 if (max_poll_gap > 0.0 and dt > max_poll_gap) else dt

            if qualifies:
                if first_qualified is None:
                    # Fresh crossing into qualified zone
                    first_qualified = now
                    accumulated_sustain = 0.0
                    tick_count = 1
                    slips = 0
                    in_retest = 0
                    retest_dwell = 0.0
                else:
                    # Continuing qualification above trigger
                    accumulated_sustain += effective_dt
                    tick_count += 1
                    in_retest = 0
                    retest_dwell = 0.0
            elif first_qualified is not None:
                # Price pulled back below trigger; check if within micro-retest buffer
                distance = abs((ltp - trigger) / max(abs(trigger), 0.01))
                if distance <= RETEST_TOLERANCE_PCT:
                    if not in_retest:
                        # Transition from qualified into micro-retest excursion
                        slips += 1
                        in_retest = 1
                        retest_dwell = 0.0
                    else:
                        # Continuing inside existing retest excursion
                        retest_dwell += effective_dt

                    # Abort if exceeded max slip count or max dwell time inside buffer
                    if slips > MAX_SLIPS or retest_dwell > max_retest_dwell:
                        first_qualified = None
                        accumulated_sustain = 0.0
                        tick_count = 0
                        slips = 0
                        in_retest = 0
                        retest_dwell = 0.0
                    else:
                        # Retest in progress: timer is paused, but tick sample is registered
                        tick_count += 1
                else:
                    # Slipped beyond 0.15% tolerance: hard reset
                    first_qualified = None
                    accumulated_sustain = 0.0
                    tick_count = 0
                    slips = 0
                    in_retest = 0
                    retest_dwell = 0.0

            # Determine total qualified sustain time from accumulated active sustain
            total_sustain = accumulated_sustain

            if stop_loss and ((direction == "BEAR" and ltp >= stop_loss) or (direction == "BULL" and ltp <= stop_loss)):
                status = "SL_HIT"
            elif first_observation and not qualifies:
                status = "WATCHING"
            elif first_qualified is not None and slips <= MAX_SLIPS:
                # Must be currently qualifying (at or beyond trigger) to trigger confirmation!
                if total_sustain >= SUSTAIN_SECONDS and tick_count >= min_ticks and qualifies:
                    status = "CONFIRMED_TRIGGER"
                else:
                    status = "TRIGGER_PENDING"
            elif direction == "BEAR" and spec.get("target_2") and ltp <= float(spec["target_2"]):
                status = "TP2_HIT"
            elif direction == "BEAR" and spec.get("target_1") and ltp <= float(spec["target_1"]):
                status = "TP1_HIT"
            elif direction == "BULL" and spec.get("target_2") and ltp >= float(spec["target_2"]):
                status = "TP2_HIT"
            elif direction == "BULL" and spec.get("target_1") and ltp >= float(spec["target_1"]):
                status = "TP1_HIT"

            updated_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO trigger_states
                (symbol, direction, trigger_price, stop_loss, target_1, target_2, status,
                 first_qualified_at, last_observed_at, slip_count, tick_count,
                 accumulated_sustain_seconds, in_retest, retest_dwell_seconds,
                 correlation_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                  direction=excluded.direction, trigger_price=excluded.trigger_price,
                  stop_loss=excluded.stop_loss, target_1=excluded.target_1, target_2=excluded.target_2,
                  status=excluded.status, first_qualified_at=excluded.first_qualified_at,
                  last_observed_at=excluded.last_observed_at, slip_count=excluded.slip_count,
                  tick_count=excluded.tick_count,
                  accumulated_sustain_seconds=excluded.accumulated_sustain_seconds,
                  in_retest=excluded.in_retest,
                  retest_dwell_seconds=excluded.retest_dwell_seconds,
                  correlation_id=excluded.correlation_id, updated_at=excluded.updated_at
                """,
                (symbol, direction, trigger, stop_loss, spec.get("target_1"), spec.get("target_2"),
                 status, first_qualified, now, slips, tick_count,
                 accumulated_sustain, in_retest, retest_dwell,
                 correlation_id, updated_at),
            )
            event = None
            if not previous or previous["status"] != status:
                event_id = str(uuid.uuid4())
                payload = {"ltp": ltp, "status": status, "first_qualified_at": first_qualified}
                cursor = conn.execute(
                    """
                    INSERT INTO trigger_events
                    (event_id, symbol, event_type, status, observed_at, correlation_id, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (event_id, symbol, "TRIGGER_LIFECYCLE", status, now, correlation_id,
                     json.dumps(payload), updated_at),
                )
                event = {
                    "sequence_id": cursor.lastrowid,
                    "event_id": event_id,
                    "symbol": symbol,
                    "event_type": "TRIGGER_LIFECYCLE",
                    "status": status,
                    "observed_at": now,
                    "correlation_id": correlation_id,
                    **payload,
                }
                logger.info(
                    "trigger.lifecycle",
                    extra={
                        "event": "trigger.lifecycle",
                        "symbol": symbol,
                        "status": status,
                        "sequence_id": cursor.lastrowid,
                        "event_id": event_id,
                        "correlation_id": correlation_id,
                    },
                )
            conn.commit()

        state = {
            "symbol": symbol,
            "status": status,
            "first_qualified_at": first_qualified,
            "sustain_seconds": round(max(0.0, total_sustain), 3) if first_qualified else 0.0,
            "slip_count": slips,
            "tick_count": tick_count,
            "correlation_id": correlation_id,
        }
        if event:
            state["event"] = event
        return state

    def events_after(self, sequence_id: int = 0, limit: int = 100) -> list[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT sequence_id, event_id, symbol, event_type, status, observed_at, correlation_id, payload_json "
                "FROM trigger_events WHERE sequence_id > ? ORDER BY sequence_id ASC LIMIT ?",
                (max(0, int(sequence_id)), max(1, min(500, int(limit)))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.update(json.loads(item.pop("payload_json")))
            result.append(item)
        return result

    def get_state(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return the latest persisted trigger state for a symbol."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM trigger_states WHERE symbol = ?",
                (str(symbol).upper().strip(),),
            ).fetchone()
        return dict(row) if row else None


_store: Optional[LiveTriggerStateStore] = None


def get_live_trigger_state_store() -> LiveTriggerStateStore:
    global _store
    if _store is None:
        _store = LiveTriggerStateStore()
    return _store
