"""FastAPI server for Sigen energy data."""

import json
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from db import connect

TZ = ZoneInfo("Europe/Stockholm")

app = FastAPI(
    title="Sigen Energy API",
    description="Real-time and historical data from Sigenergy system",
    version="1.1.0",
)

# Allow access from anywhere (we will secure via nginx later)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def row_to_dict(row):
    """Convert SQLite Row to dict, parsing raw_json."""
    d = dict(row)
    if "raw_json" in d and d["raw_json"]:
        try:
            d["raw"] = json.loads(d["raw_json"])
        except Exception:
            pass
        del d["raw_json"]
    return d


@app.get("/api/health")
def health():
    """Health check endpoint."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as count, MAX(ts_utc) as latest FROM pollings"
        ).fetchone()
    return {
        "status": "ok",
        "total_pollings": row["count"],
        "latest_polling": row["latest"],
        "server_time": datetime.now(TZ).isoformat(timespec="seconds"),
    }


@app.get("/api/latest")
def latest():
    """Get the most recent polling."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM pollings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise HTTPException(404, "No pollings found")
    return row_to_dict(row)


@app.get("/api/today")
def today():
    """Get all pollings from today (local time)."""
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    return day(today_str)


@app.get("/api/day/{date}")
def day(date: str):
    """Get all pollings for a specific date (YYYY-MM-DD)."""
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Date must be YYYY-MM-DD")

    with connect() as conn:
        rows = conn.execute(
            "SELECT ts_local, pv_power, third_pv_power, load_power, "
            "battery_power, battery_soc, buy_sell_power, ev_power, "
            "pv_day_nrg, mode FROM pollings WHERE date_local = ? "
            "ORDER BY ts_local",
            (date,),
        ).fetchall()

        summary = conn.execute(
            "SELECT * FROM daily_summary WHERE date = ?", (date,)
        ).fetchone()

    return {
        "date": date,
        "polling_count": len(rows),
        "summary": dict(summary) if summary else None,
        "pollings": [dict(r) for r in rows],
    }


@app.get("/api/day/{date}/5min")
def day_5min(date: str):
    """Get 5-min energy data for a specific date (from energy_5min table).

    This is high-quality data from Sigen Data Download (288 rows/day),
    not the polling data. Used for detailed charts.
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Date must be YYYY-MM-DD")

    with connect() as conn:
        rows = conn.execute(
            "SELECT ts_local, last_kw, bat_kw, evdc_kw, grid_kw, pv3_kw "
            "FROM energy_5min_v2 WHERE date_local = ? "
            "ORDER BY ts_local",
            (date,),
        ).fetchall()

        summary = conn.execute(
            "SELECT * FROM daily_summary WHERE date = ?", (date,)
        ).fetchone()

    return {
        "date": date,
        "row_count": len(rows),
        "summary": dict(summary) if summary else None,
        "data": [dict(r) for r in rows],
    }


@app.get("/api/today/5min")
def today_5min():
    """Get 5-min energy data for today (local time)."""
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    return day_5min(today_str)


@app.get("/api/days")
def days(limit: int = Query(60, ge=1, le=365)):
    """List all days that have data."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT date_local, COUNT(*) as count, "
            "MIN(ts_local) as first_ts, MAX(ts_local) as last_ts "
            "FROM pollings GROUP BY date_local "
            "ORDER BY date_local DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"days": [dict(r) for r in rows]}


@app.get("/api/days5min")
def days_5min(limit: int = Query(365, ge=1, le=400)):
    """List all days that have 5-min energy data."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT date_local, COUNT(*) as count "
            "FROM energy_5min_v2 GROUP BY date_local "
            "ORDER BY date_local DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"days": [dict(r) for r in rows]}


@app.get("/api/summary")
def summary():
    """Aggregated stats: total, monthly, last 30 days."""
    with connect() as conn:
        # Grand total
        grand = conn.execute(
            "SELECT COUNT(*) as days, "
            "SUM(total_kr) as grand_total, "
            "SUM(sol_kr) as grand_sol, "
            "SUM(bat_kr) as grand_bat "
            "FROM daily_summary"
        ).fetchone()

        # Monthly breakdown
        monthly = conn.execute(
            "SELECT substr(date, 1, 7) as month, "
            "SUM(total_kr) as total_kr, "
            "SUM(sol_kr) as sol_kr, "
            "SUM(bat_kr) as bat_kr, "
            "COUNT(*) as days "
            "FROM daily_summary GROUP BY month ORDER BY month"
        ).fetchall()

        # Last 30 days
        last_30 = conn.execute(
            "SELECT date, sol_kr, bat_kr, total_kr, "
            "sol_kwh, import_kwh, export_kwh "
            "FROM daily_summary ORDER BY date DESC LIMIT 30"
        ).fetchall()

    return {
        "generated": datetime.now(TZ).isoformat(timespec="seconds"),
        "total_days": grand["days"] if grand else 0,
        "grand_total_kr": grand["grand_total"] if grand else 0,
        "grand_sol_kr": grand["grand_sol"] if grand else 0,
        "grand_bat_kr": grand["grand_bat"] if grand else 0,
        "monthly": [dict(r) for r in monthly],
        "last_30_days": [dict(r) for r in reversed(last_30)],
    }
