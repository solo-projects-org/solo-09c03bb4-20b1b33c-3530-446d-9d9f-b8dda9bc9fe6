"""SQLite access layer.

Design notes for concurrency (two API instances sharing one SQLite file):
- WAL journal mode lets readers proceed while a writer holds the write lock.
- Every ingest and every seal run inside a single BEGIN IMMEDIATE transaction,
  so writers serialize and each commit is atomic: raw readings and the
  recomputed hourly usage, or the seal position and purged readings, become
  visible together, never mixed.
- The seals cutoff is read inside the same write transaction that stores a
  batch, so a concurrent seal cannot move the boundary between the check and
  the insert.
- busy_timeout makes a second writer wait for the first one instead of
  failing with SQLITE_BUSY.
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id          TEXT PRIMARY KEY,
    received_at       TEXT NOT NULL,          -- ISO-8601 UTC
    rows_total        INTEGER NOT NULL,       -- valid rows in the CSV
    rows_inserted     INTEGER NOT NULL,       -- brand-new readings stored
    rows_deduplicated INTEGER NOT NULL        -- retransmitted identical rows
);

CREATE TABLE IF NOT EXISTS readings (
    meter_id   TEXT NOT NULL,
    utc_second INTEGER NOT NULL,              -- aligned to a UTC hour
    kwh        REAL NOT NULL,                 -- cumulative meter reading, >= 0
    batch_id   TEXT NOT NULL REFERENCES batches(batch_id),
    PRIMARY KEY (meter_id, utc_second)
);

CREATE TABLE IF NOT EXISTS hourly_usage (
    meter_id   TEXT NOT NULL,
    hour_start INTEGER NOT NULL,              -- UTC second of the hour start
    kwh_used   REAL,                          -- NULL when not billable
    billable   INTEGER NOT NULL,              -- 1 billable / 0 not billable
    reason     TEXT NOT NULL,                 -- ok | gap | rollback
    PRIMARY KEY (meter_id, hour_start)
);

-- One row per sealed meter. cutoff_second is a UTC hour boundary: raw readings
-- strictly before it have been purged and the hourly results before it are
-- frozen. The cutoff can only move forward (to a larger hour boundary).
CREATE TABLE IF NOT EXISTS seals (
    meter_id       TEXT PRIMARY KEY,
    cutoff_second  INTEGER NOT NULL,          -- UTC hour-aligned; readings < this are purged
    sealed_at      TEXT NOT NULL              -- ISO-8601 UTC of the latest seal action
);
"""

BUSY_TIMEOUT_MS = 30_000


def db_path() -> str:
    # Read at connect time so tests / containers can relocate the database.
    return os.environ.get("DB_PATH", "/data/energy.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=BUSY_TIMEOUT_MS / 1000.0,
                           isolation_level=None)  # autocommit; explicit BEGIN
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db() -> None:
    path = db_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
