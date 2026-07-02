"""SQLite database module for Sigen energy data."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent.parent / "data" / "sigen.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS pollings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    ts_local TEXT NOT NULL,
    date_local TEXT NOT NULL,
    pv_power REAL,
    third_pv_power REAL,
    load_power REAL,
    battery_power REAL,
    battery_soc REAL,
    buy_sell_power REAL,
    ev_power REAL,
    pv_day_nrg REAL,
    mode TEXT,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_pollings_ts ON pollings(ts_utc);
CREATE INDEX IF NOT EXISTS idx_pollings_date ON pollings(date_local);

CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,
    rows INTEGER NOT NULL,
    sol_kwh REAL,
    last_kwh REAL,
    import_kwh REAL,
    export_kwh REAL,
    bat_lad_kwh REAL,
    bat_url_kwh REAL,
    evdc_kwh REAL,
    spot_min REAL,
    spot_max REAL,
    spot_mean REAL,
    sol_kr REAL,
    bat_kr REAL,
    total_kr REAL,
    bat_lad_nat REAL,
    bat_lad_sol REAL,
    bat_url_nat REAL,
    bat_url_last REAL,
    sol_last REAL,
    sol_nat REAL,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def connect():
    """Context manager for database connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they do not exist."""
    with connect() as conn:
        conn.executescript(SCHEMA)


def insert_polling(ts_utc, ts_local, flow, mode, raw_json):
    """Insert a polling record from Sigen API response."""
    date_local = ts_local[:10]
    with connect() as conn:
        conn.execute("""
            INSERT INTO pollings (
                ts_utc, ts_local, date_local,
                pv_power, third_pv_power, load_power,
                battery_power, battery_soc, buy_sell_power,
                ev_power, pv_day_nrg, mode, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ts_utc, ts_local, date_local,
            flow.get("pvPower") or 0,
            flow.get("thirdPvPower") or 0,
            flow.get("loadPower") or 0,
            flow.get("batteryPower") or 0,
            flow.get("batterySoc") or 0,
            flow.get("buySellPower") or 0,
            flow.get("evPower") or 0,
            flow.get("pvDayNrg") or 0,
            mode,
            raw_json,
        ))


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
