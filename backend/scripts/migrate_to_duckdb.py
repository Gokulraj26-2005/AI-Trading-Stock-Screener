"""
Migration Script: SQLite daily_candles -> DuckDB
=================================================
Copies all historical candle data from SQLite to DuckDB columnar storage.
Safe and idempotent: uses INSERT OR IGNORE / INSERT OR REPLACE.
"""

from __future__ import annotations

import os
import sys
import time
import sqlite3
from pathlib import Path
import duckdb

SQLITE_PATH = Path(__file__).resolve().parent.parent / "app" / "data" / "historical_candles.sqlite"
DUCKDB_PATH = Path(__file__).resolve().parent.parent / "app" / "data" / "historical_candles.duckdb"


def migrate():
    print(f"Reading from SQLite: {SQLITE_PATH}")
    if not SQLITE_PATH.exists():
        print(f"ERROR: SQLite database not found at {SQLITE_PATH}")
        sys.exit(1)

    t0 = time.perf_counter()
    with sqlite3.connect(SQLITE_PATH) as s_conn:
        cur = s_conn.cursor()
        total_rows = cur.execute("SELECT COUNT(*) FROM daily_candles").fetchone()[0]
        print(f"Found {total_rows:,} rows in SQLite daily_candles")

        rows = cur.execute("""
            SELECT symbol, timestamp, open, high, low, close, volume
            FROM daily_candles
        """).fetchall()

    t_read = time.perf_counter() - t0
    print(f"Read {len(rows):,} rows in {t_read:.2f}s")

    print(f"Writing to DuckDB: {DUCKDB_PATH}")
    DUCKDB_PATH.parent.mkdir(parents=True, exist_ok=True)

    d_conn = duckdb.connect(str(DUCKDB_PATH))
    d_conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_candles (
            symbol VARCHAR NOT NULL,
            timestamp BIGINT NOT NULL,
            open DOUBLE NOT NULL,
            high DOUBLE NOT NULL,
            low DOUBLE NOT NULL,
            close DOUBLE NOT NULL,
            volume DOUBLE NOT NULL,
            PRIMARY KEY (symbol, timestamp)
        )
    """)

    t1 = time.perf_counter()
    # Batch insertion via appender or executemany
    # executemany in DuckDB is heavily optimized and vectorized
    d_conn.executemany("""
        INSERT OR REPLACE INTO daily_candles 
        (symbol, timestamp, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, rows)
    d_conn.commit()
    t_write = time.perf_counter() - t1

    # Verify count
    count = d_conn.execute("SELECT COUNT(*) FROM daily_candles").fetchone()[0]
    print(f"DuckDB daily_candles count: {count:,} (written in {t_write:.2f}s)")

    # Sample query
    symbols = d_conn.execute("SELECT DISTINCT symbol FROM daily_candles LIMIT 5").fetchall()
    print("Sample symbols in DuckDB:", [s[0] for s in symbols])

    # Check query performance
    if symbols:
        sample_sym = symbols[0][0]
        t_query0 = time.perf_counter()
        q_rows = d_conn.execute("""
            SELECT timestamp, open, high, low, close, volume
            FROM daily_candles
            WHERE symbol = ?
            ORDER BY timestamp ASC
        """, [sample_sym]).fetchall()
        t_query = time.perf_counter() - t_query0
        print(f"Query test for '{sample_sym}': {len(q_rows)} candles fetched in {t_query*1000:.3f} ms")

    d_conn.close()
    print("Migration completed successfully!")


if __name__ == "__main__":
    migrate()

