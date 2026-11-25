"""
Watchdog Data Recorder — Phase 0
==================================
Records per-second market snapshots and alert events to a dedicated SQLite
database for post-session outcome analysis and threshold calibration.

Tables
------
watchdog_snapshots   — per-second: LTP, CVD, banded OBI, walls, depth summary
watchdog_alerts      — per-alert: type, trigger values, outcome tags (+1m/3m/5m)

The outcome tagger runs as a background asyncio task that fills in price
changes after the fact once enough time has elapsed.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from logger import logger

# ── DB location ──────────────────────────────────────────────────────────────
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
RECORDING_DB = _DATA_DIR / "watchdog_recordings.db"

# Outcome tagging windows (seconds)
_OUTCOME_WINDOWS = [60, 180, 300]  # 1m, 3m, 5m


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(RECORDING_DB), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def _init_recording_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS watchdog_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL    NOT NULL,
                symbol      TEXT    NOT NULL,
                direction   TEXT    NOT NULL,   -- 'LONG' | 'SHORT'
                ltp         REAL,
                volume      INTEGER,
                cvd         INTEGER,
                cvd_delta   INTEGER,            -- CVD change since last snapshot
                banded_obi  REAL,               -- OBI using only ±0.5% levels
                deep_obi    REAL,
                obi_smooth  REAL,               -- 10-tick rolling average OBI
                top_walls   TEXT,               -- JSON list [{side,price,qty,strength,notional}]
                depth_snap  TEXT,               -- JSON compressed depth (top-20 bid/ask)
                rsi_5m      REAL,
                vwap_5m     REAL,
                ema20_5m    REAL
            );
            CREATE INDEX IF NOT EXISTS idx_snap_sym_ts ON watchdog_snapshots(symbol, ts);

            CREATE TABLE IF NOT EXISTS watchdog_alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL    NOT NULL,
                symbol          TEXT    NOT NULL,
                direction       TEXT    NOT NULL,
                alert_type      TEXT    NOT NULL,
                tier            TEXT    NOT NULL,   -- 'INFO' | 'WATCH' | 'ACT'
                signals         TEXT,               -- JSON list of contributing signal names
                trigger_values  TEXT,               -- JSON dict of the values that fired it
                ltp_at_alert    REAL,
                outcome_1m      REAL,               -- price change % after 1m (NULL until tagged)
                outcome_3m      REAL,
                outcome_5m      REAL,
                tag_ts          REAL                -- when outcome was filled
            );
            CREATE INDEX IF NOT EXISTS idx_alert_sym_ts ON watchdog_alerts(symbol, ts);
        """)


_init_recording_db()


# ── Public API ────────────────────────────────────────────────────────────────

def record_snapshot(
    symbol: str,
    direction: str,
    ltp: float,
    volume: int = 0,
    cvd: int = 0,
    cvd_delta: int = 0,
    banded_obi: Optional[float] = None,
    deep_obi: Optional[float] = None,
    obi_smooth: Optional[float] = None,
    top_walls: Optional[List[Dict[str, Any]]] = None,
    depth_snap: Optional[Dict[str, Any]] = None,
    rsi_5m: Optional[float] = None,
    vwap_5m: Optional[float] = None,
    ema20_5m: Optional[float] = None,
) -> None:
    """Fire-and-forget snapshot write (non-blocking in the async sense; write
    is synchronous but cheap — WAL mode means it never blocks readers)."""
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO watchdog_snapshots
                   (ts, symbol, direction, ltp, volume, cvd, cvd_delta,
                    banded_obi, deep_obi, obi_smooth, top_walls, depth_snap,
                    rsi_5m, vwap_5m, ema20_5m)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(), symbol, direction, ltp, volume, cvd, cvd_delta,
                    banded_obi, deep_obi, obi_smooth,
                    json.dumps(top_walls or []),
                    json.dumps(
                        {
                            "bids": (depth_snap or {}).get("bids", [])[:20],
                            "asks": (depth_snap or {}).get("asks", [])[:20],
                        }
                    ),
                    rsi_5m, vwap_5m, ema20_5m,
                ),
            )
    except Exception as exc:
        logger.debug("WatchdogRecorder snapshot write failed: %s", exc)


def record_alert(
    symbol: str,
    direction: str,
    alert_type: str,
    tier: str,
    signals: List[str],
    trigger_values: Dict[str, Any],
    ltp_at_alert: float,
) -> int:
    """Insert an alert record; returns the row id so the outcome tagger can
    update it later."""
    try:
        with _get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO watchdog_alerts
                   (ts, symbol, direction, alert_type, tier, signals,
                    trigger_values, ltp_at_alert)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    time.time(), symbol, direction, alert_type, tier,
                    json.dumps(signals),
                    json.dumps({k: (float(v) if isinstance(v, (int, float)) else str(v))
                                for k, v in trigger_values.items()}),
                    ltp_at_alert,
                ),
            )
            return cur.lastrowid or 0
    except Exception as exc:
        logger.debug("WatchdogRecorder alert write failed: %s", exc)
        return 0


# ── Outcome tagger background task ───────────────────────────────────────────

class WatchdogOutcomeTagger:
    """
    Background asyncio task that checks for untagged alerts older than 5 minutes
    and fills in the outcome columns using snapshot data recorded at the correct
    timestamps.  Runs every 60 seconds.
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            logger.info("WatchdogOutcomeTagger started.")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60.0)
                await asyncio.get_event_loop().run_in_executor(None, self._tag_pending)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("WatchdogOutcomeTagger loop error: %s", exc)

    def _tag_pending(self) -> None:
        """Find untagged alerts with enough elapsed time and fill outcome columns."""
        now = time.time()
        cutoff = now - max(_OUTCOME_WINDOWS)  # only tag alerts old enough for all windows
        try:
            conn = _get_conn()
            rows = conn.execute(
                "SELECT id, ts, symbol, ltp_at_alert FROM watchdog_alerts "
                "WHERE tag_ts IS NULL AND ts <= ?",
                (cutoff,),
            ).fetchall()

            for row in rows:
                alert_id = row["id"]
                alert_ts = row["ts"]
                symbol = row["symbol"]
                ltp_at_alert = row["ltp_at_alert"] or 0.0

                outcomes = {}
                for window in _OUTCOME_WINDOWS:
                    target_ts = alert_ts + window
                    snap = conn.execute(
                        "SELECT ltp FROM watchdog_snapshots "
                        "WHERE symbol=? AND ts >= ? ORDER BY ts ASC LIMIT 1",
                        (symbol, target_ts),
                    ).fetchone()
                    if snap and snap["ltp"] and ltp_at_alert > 0:
                        outcomes[window] = round(
                            (snap["ltp"] - ltp_at_alert) / ltp_at_alert * 100.0, 4
                        )
                    else:
                        outcomes[window] = None

                conn.execute(
                    "UPDATE watchdog_alerts SET "
                    "outcome_1m=?, outcome_3m=?, outcome_5m=?, tag_ts=? "
                    "WHERE id=?",
                    (
                        outcomes.get(60), outcomes.get(180), outcomes.get(300),
                        now, alert_id,
                    ),
                )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.debug("WatchdogOutcomeTagger tagging error: %s", exc)


# ── Singleton ─────────────────────────────────────────────────────────────────
_tagger: Optional[WatchdogOutcomeTagger] = None


def get_outcome_tagger() -> WatchdogOutcomeTagger:
    global _tagger
    if _tagger is None:
        _tagger = WatchdogOutcomeTagger()
    return _tagger
