"""Adds the 5-min energy data table to the SQLite database."""

import sqlite3
import sys
sys.path.insert(0, '.')
from scripts.db import DB_PATH, connect

SCHEMA_EXTRA = """
CREATE TABLE IF NOT EXISTS energy_5min (
    ts_local TEXT PRIMARY KEY,
    date_local TEXT NOT NULL,
    last_kw REAL,
    bat_kw REAL,
    evdc_kw REAL,
    grid_kw REAL,
    pv3_kw REAL
);

CREATE INDEX IF NOT EXISTS idx_energy5_date ON energy_5min(date_local);
"""

if __name__ == "__main__":
    with connect() as conn:
        conn.executescript(SCHEMA_EXTRA)
    print(f"Schema extended at {DB_PATH}")
