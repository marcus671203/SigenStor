"""FastAPI server for Sigen energy data."""

import json
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
import uuid
import subprocess
import smtplib
import ssl
from email.message import EmailMessage
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from db import connect

# Ladda .env för mail-credentials
load_dotenv(Path(__file__).parent.parent / ".env")

# Jobb-lagring för async email tasks
EMAIL_JOBS = {}

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
    allow_methods=["GET", "POST"],
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
            "SELECT COUNT(*) as count, MAX(ts_utc) as latest FROM realtime"
        ).fetchone()
    return {
        "status": "ok",
        "total_pollings": row["count"],
        "latest_polling": row["latest"],
        "server_time": datetime.now(TZ).isoformat(timespec="seconds"),
    }


@app.get("/api/latest")
def latest():
    """Get the most recent realtime polling."""
    with connect() as conn:
        row = conn.execute("""
            SELECT ts_utc, ts_local,
                   substr(ts_local, 1, 10) as date_local,
                   pv_power_kw as pv_power,
                   third_pv_kw as third_pv_power,
                   load_kw as load_power,
                   battery_kw as battery_power,
                   battery_soc,
                   buy_sell_kw as buy_sell_power,
                   ev_power_kw as ev_power,
                   pv_day_kwh as pv_day_nrg,
                   op_mode as mode,
                   raw_json
            FROM realtime ORDER BY ts_utc DESC LIMIT 1
        """).fetchone()
    if not row:
        raise HTTPException(404, "No realtime data found")
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
            """
            SELECT ts_local,
                   pv_power_kw as pv_power,
                   third_pv_kw as third_pv_power,
                   load_kw as load_power,
                   battery_kw as battery_power,
                   battery_soc,
                   buy_sell_kw as buy_sell_power,
                   ev_power_kw as ev_power,
                   pv_day_kwh as pv_day_nrg,
                   op_mode as mode
            FROM realtime WHERE substr(ts_local, 1, 10) = ?
            ORDER BY ts_local
            """,
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
            "FROM realtime GROUP BY substr(ts_local, 1, 10) "
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


class SendExcelRequest(BaseModel):
    email: str


def run_excel_job(job_id: str, email: str):
    """Bygg Excel och skicka via mail. Uppdatera EMAIL_JOBS med status."""
    root = Path(__file__).parent.parent
    xlsx = root / "energibesparing.xlsx"
    
    try:
        EMAIL_JOBS[job_id] = {"status": "running", "message": "Bygger Excel..."}
        
        # 1) Bygg Excel-filen via build_v60.py, men rename output
        # Vi kör en Python inline så vi kan sätta EXCEL_OUTPUT_PATH
        build_script = root / "scripts" / "excel" / "build_v60.py"
        result = subprocess.run(
            [str(root / "venv-sigen-api" / "bin" / "python"), str(build_script)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode != 0:
            EMAIL_JOBS[job_id] = {
                "status": "error",
                "message": f"Excel-generering misslyckades: {result.stderr[:300]}"
            }
            return
        
        # 2) Hitta senaste v-filen (v61.xlsx etc) och kopiera till energibesparing.xlsx
        v_files = sorted(root.glob("energibesparing_v*.xlsx"))
        if not v_files:
            EMAIL_JOBS[job_id] = {"status": "error", "message": "Ingen Excel-fil genererad"}
            return
        latest = v_files[-1]
        import shutil
        shutil.copy(latest, xlsx)
        
        EMAIL_JOBS[job_id] = {"status": "running", "message": f"Skickar mail till {email}..."}
        
        # 3) Skicka mail
        msg = EmailMessage()
        msg["From"] = os.environ["GMAIL_USER"]
        msg["To"] = email
        msg["Subject"] = "Sigen energibesparing (senaste)"
        msg.set_content(
            f"Hej!\n\n"
            f"Senaste Sigen energibesparing bifogas.\n\n"
            f"Genererad: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M')}\n\n"
            f"MVH,\nSigen VPS\n"
        )
        with open(xlsx, "rb") as f:
            msg.add_attachment(
                f.read(),
                maintype="application",
                subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename="energibesparing.xlsx"
            )
        
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
            s.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
            s.send_message(msg)
        
        EMAIL_JOBS[job_id] = {
            "status": "done",
            "message": f"✅ Skickat till {email}!"
        }
    except Exception as e:
        EMAIL_JOBS[job_id] = {
            "status": "error",
            "message": f"Fel: {str(e)[:300]}"
        }


@app.post("/api/send-excel")
def send_excel(req: SendExcelRequest, background: BackgroundTasks):
    """Bygg Excel och skicka via mail. Async - returnera job_id direkt."""
    if not req.email or "@" not in req.email:
        raise HTTPException(400, "Ogiltig mailadress")
    
    job_id = str(uuid.uuid4())
    EMAIL_JOBS[job_id] = {"status": "pending", "message": "Väntar på start..."}
    background.add_task(run_excel_job, job_id, req.email)
    return {"job_id": job_id, "status": "pending"}


@app.get("/api/send-excel/status/{job_id}")
def send_excel_status(job_id: str):
    """Hämta status för Excel-mail-jobb."""
    if job_id not in EMAIL_JOBS:
        raise HTTPException(404, "Job not found")
    return EMAIL_JOBS[job_id]


@app.get("/api/bill/{period}")
def get_bill(period: str):
    """Beräkna elräkning för aktuell eller föregående månad.
    
    period: 'current' eller 'previous'
    """
    from datetime import date
    import json
    
    today = datetime.now(TZ).date()
    
    if period == "current":
        year, month = today.year, today.month
        label = f"{month:02d}-{year} (pågående)"
    elif period == "previous":
        if today.month == 1:
            year, month = today.year - 1, 12
        else:
            year, month = today.year, today.month - 1
        label = f"{month:02d}-{year}"
    else:
        raise HTTPException(400, "period måste vara 'current' eller 'previous'")
    
    # Hämta alla 5-min-rader för månaden
    month_pattern = f"{year:04d}-{month:02d}%"
    with connect() as conn:
        rows = conn.execute(
            "SELECT ts_local, grid_kw FROM energy_5min_v2 WHERE date_local LIKE ? ORDER BY ts_local",
            (month_pattern,)
        ).fetchall()
    
    if not rows:
        return {
            "period": label,
            "error": "Ingen data för denna period",
            "kop_kwh": 0, "sal_kwh": 0, "totalt": 0
        }
    
    # Ladda spot-priser
    root = Path(__file__).parent.parent
    spot_cache = root / "data" / "spot_cache"
    spot_lookup = {}
    from datetime import date as ddate, timedelta
    d = ddate(year, month, 1)
    while d.month == month:
        f = spot_cache / f"{d.isoformat()}_SE3.json"
        if f.exists():
            data = json.loads(f.read_text())
            for entry in data:
                ts_start = entry["time_start"]
                price = entry["SEK_per_kWh"]
                dt = datetime.fromisoformat(ts_start.replace("Z", "+00:00"))
                spot_lookup[(dt.date().isoformat(), dt.hour)] = price
        d += timedelta(days=1)
    
    # Räkna
    H = 5/60.0
    kop_kwh_vikt = 0.0
    kop_kwh = 0.0
    sal_kwh_vikt = 0.0
    sal_kwh = 0.0
    
    for r in rows:
        ts = datetime.fromisoformat(r["ts_local"])
        grid = r["grid_kw"] or 0
        key = (ts.date().isoformat(), ts.hour)
        spot = spot_lookup.get(key)
        if spot is None:
            continue
        if grid > 0:
            kop_kwh_vikt += grid * spot * H
            kop_kwh += grid * H
        else:
            sal_kwh_vikt += (-grid) * spot * H
            sal_kwh += (-grid) * H
    
    # Formler
    elhandel_kop = (kop_kwh_vikt + kop_kwh * 0.04) * 1.25
    elhandel_fast = 39.0
    elhandel_sal = sal_kwh_vikt + sal_kwh * 0.104
    
    # Nätöverföring: bytte 2026-04-02
    #   Före:  44.5 öre/kWh inkl moms
    #   Efter: 24.4 öre/kWh exkl moms = 30.5 öre/kWh inkl moms
    CONTRACT_CHANGE = ddate(2026, 4, 2)
    period_date = ddate(year, month, 15)  # mitten av månaden avgör
    if period_date >= CONTRACT_CHANGE:
        nat_over_rate = 0.244 * 1.25  # 30.5 öre inkl moms
    else:
        nat_over_rate = 0.445
    nat_over = kop_kwh * nat_over_rate
    
    energiskatt = kop_kwh * 0.45
    nat_fast = 6468 * 1.25 / 12
    
    elhandel_tot = elhandel_kop + elhandel_fast - elhandel_sal
    vattenfall_tot = nat_over + energiskatt + nat_fast
    totalt = elhandel_tot + vattenfall_tot
    
    return {
        "period": label,
        "year": year,
        "month": month,
        "kop_kwh": round(kop_kwh, 1),
        "sal_kwh": round(sal_kwh, 1),
        "snitt_spot_kop": round(kop_kwh_vikt / kop_kwh, 3) if kop_kwh else 0,
        "snitt_spot_sal": round(sal_kwh_vikt / sal_kwh, 3) if sal_kwh else 0,
        "elhandel": {
            "kop": round(elhandel_kop, 2),
            "fast": elhandel_fast,
            "salj_intakt": round(elhandel_sal, 2),
            "summa": round(elhandel_tot, 2)
        },
        "vattenfall": {
            "nat_overforing": round(nat_over, 2),
            "nat_overforing_rate_ore": round(nat_over_rate * 100, 1),
            "energiskatt": round(energiskatt, 2),
            "fast": round(nat_fast, 2),
            "summa": round(vattenfall_tot, 2)
        },
        "totalt": round(totalt, 2)
    }


@app.get("/api/alerts")
def alerts():
    """Hämta alla aktiva (olösta) larm för mobilappen."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT id, ts_utc, alert_type, severity, title, message
            FROM alerts
            WHERE resolved_at IS NULL
            ORDER BY ts_utc DESC
            LIMIT 20
        """).fetchall()
    return {
        "count": len(rows),
        "alerts": [dict(r) for r in rows]
    }



@app.get("/api/evdc-cost")
def evdc_cost():
    """EVDC-laddkostnad denna manad med LIFO-buffer for bat och EVDC."""
    import json
    from datetime import date as ddate, timedelta

    today = datetime.now(TZ).date()
    year, month = today.year, today.month
    month_prefix = f"{year:04d}-{month:02d}"

    with connect() as conn:
        rows = conn.execute(
            "SELECT ts_local, date_local, last_kw, bat_kw, evdc_kw, pv3_kw "
            "FROM energy_5min_v2 ORDER BY ts_local"
        ).fetchall()

    if not rows:
        return {"month": f"{month:02d}-{year}", "evdc_lad_kwh": 0,
                "kostnad_kr": 0, "snittpris": 0,
                "sol_andel_pct": 0, "bat_andel_pct": 0, "nat_andel_pct": 0}

    root = Path(__file__).parent.parent
    spot_cache = root / "data" / "spot_cache"
    spot_lookup = {}
    for f in spot_cache.glob("*_SE3.json"):
        try:
            data = json.loads(f.read_text())
            for entry in data:
                dt = datetime.fromisoformat(entry["time_start"].replace("Z", "+00:00"))
                spot_lookup[(dt.date().isoformat(), dt.hour)] = entry["SEK_per_kWh"]
        except Exception:
            pass

    H = 5 / 60.0
    bat_buffer = []
    evdc_buffer = []

    def pop_lifo_kr(buf, kwh_needed):
        kostnad = 0.0
        kvar = kwh_needed
        while kvar > 0.0001 and buf:
            chunk = buf[-1]
            ta = min(chunk[0], kvar)
            kostnad += ta * chunk[1]
            chunk[0] -= ta
            kvar -= ta
            if chunk[0] < 0.0001:
                buf.pop()
        return kostnad

    m_evdc_lad_kwh = 0.0
    m_kostnad_kr = 0.0
    m_sol_kwh = 0.0
    m_bat_kwh = 0.0
    m_nat_kwh = 0.0

    for r in rows:
        ts = datetime.fromisoformat(r["ts_local"])
        spot = spot_lookup.get((ts.date().isoformat(), ts.hour))
        if spot is None:
            continue

        last = r["last_kw"] or 0
        bat = r["bat_kw"] or 0
        evdc = r["evdc_kw"] or 0
        sol = r["pv3_kw"] or 0

        bat_url = max(0, bat)
        bat_lad = max(0, -bat)
        evdc_lad = max(0, -evdc)
        evdc_url = max(0, evdc)

        buy_price = 1.25 * (spot + 0.604) + 0.04
        sell_price = spot + 0.104

        sol_till_last = min(sol, last)
        sol_over = sol - sol_till_last
        sol_till_bat = min(sol_over, bat_lad)
        sol_over -= sol_till_bat
        sol_till_evdc = min(sol_over, evdc_lad)

        rest_last = max(0, last - sol_till_last)
        bat_till_last = min(bat_url, rest_last)
        bat_url_rest = bat_url - bat_till_last
        bat_till_evdc = min(bat_url_rest, max(0, evdc_lad - sol_till_evdc))

        nat_till_bat = max(0, bat_lad - sol_till_bat)
        nat_till_evdc = max(0, evdc_lad - sol_till_evdc - bat_till_evdc)

        if sol_till_bat > 0.0001:
            bat_buffer.append([sol_till_bat * H, sell_price])
        if nat_till_bat > 0.0001:
            bat_buffer.append([nat_till_bat * H, buy_price])

        bat_till_evdc_kr = 0.0
        if bat_till_last > 0.0001:
            pop_lifo_kr(bat_buffer, bat_till_last * H)
        if bat_till_evdc > 0.0001:
            bat_till_evdc_kr = pop_lifo_kr(bat_buffer, bat_till_evdc * H)
        bat_till_nat = bat_url - bat_till_last - bat_till_evdc
        if bat_till_nat > 0.0001:
            pop_lifo_kr(bat_buffer, bat_till_nat * H)

        if sol_till_evdc > 0.0001:
            evdc_buffer.append([sol_till_evdc * H, sell_price])
        if bat_till_evdc > 0.0001:
            bat_avg = bat_till_evdc_kr / (bat_till_evdc * H)
            evdc_buffer.append([bat_till_evdc * H, bat_avg])
        if nat_till_evdc > 0.0001:
            evdc_buffer.append([nat_till_evdc * H, buy_price])

        if evdc_url > 0.0001:
            pop_lifo_kr(evdc_buffer, evdc_url * H)

        if r["date_local"].startswith(month_prefix):
            m_evdc_lad_kwh += evdc_lad * H
            m_sol_kwh += sol_till_evdc * H
            m_bat_kwh += bat_till_evdc * H
            m_nat_kwh += nat_till_evdc * H
            m_kostnad_kr += sol_till_evdc * sell_price * H
            m_kostnad_kr += bat_till_evdc_kr
            m_kostnad_kr += nat_till_evdc * buy_price * H

    if m_evdc_lad_kwh > 0.01:
        snittpris = m_kostnad_kr / m_evdc_lad_kwh
        sol_p = m_sol_kwh / m_evdc_lad_kwh * 100
        bat_p = m_bat_kwh / m_evdc_lad_kwh * 100
        nat_p = m_nat_kwh / m_evdc_lad_kwh * 100
    else:
        snittpris = sol_p = bat_p = nat_p = 0

    return {
        "month": f"{month:02d}-{year}",
        "evdc_lad_kwh": round(m_evdc_lad_kwh, 1),
        "kostnad_kr": round(m_kostnad_kr, 2),
        "snittpris": round(snittpris, 3),
        "sol_andel_pct": round(sol_p, 1),
        "bat_andel_pct": round(bat_p, 1),
        "nat_andel_pct": round(nat_p, 1)
    }

@app.get("/api/evdc-history")
def evdc_history():
    """EVDC-laddning per manad, senaste 12 manaderna med LIFO-buffer."""
    import json
    from datetime import date as ddate, timedelta

    today = datetime.now(TZ).date()

    with connect() as conn:
        rows = conn.execute(
            "SELECT ts_local, date_local, last_kw, bat_kw, evdc_kw, pv3_kw "
            "FROM energy_5min_v2 ORDER BY ts_local"
        ).fetchall()

    if not rows:
        return {"months": []}

    root = Path(__file__).parent.parent
    spot_cache = root / "data" / "spot_cache"
    spot_lookup = {}
    for f in spot_cache.glob("*_SE3.json"):
        try:
            data = json.loads(f.read_text())
            for entry in data:
                dt = datetime.fromisoformat(entry["time_start"].replace("Z", "+00:00"))
                spot_lookup[(dt.date().isoformat(), dt.hour)] = entry["SEK_per_kWh"]
        except Exception:
            pass

    H = 5 / 60.0
    bat_buffer = []
    evdc_buffer = []

    def pop_lifo_kr(buf, kwh_needed):
        kostnad = 0.0
        kvar = kwh_needed
        while kvar > 0.0001 and buf:
            chunk = buf[-1]
            ta = min(chunk[0], kvar)
            kostnad += ta * chunk[1]
            chunk[0] -= ta
            kvar -= ta
            if chunk[0] < 0.0001:
                buf.pop()
        return kostnad

    monthly = {}

    for r in rows:
        ts = datetime.fromisoformat(r["ts_local"])
        spot = spot_lookup.get((ts.date().isoformat(), ts.hour))
        if spot is None:
            continue

        last = r["last_kw"] or 0
        bat = r["bat_kw"] or 0
        evdc = r["evdc_kw"] or 0
        sol = r["pv3_kw"] or 0

        bat_url = max(0, bat)
        bat_lad = max(0, -bat)
        evdc_lad = max(0, -evdc)
        evdc_url = max(0, evdc)

        buy_price = 1.25 * (spot + 0.604) + 0.04
        sell_price = spot + 0.104

        sol_till_last = min(sol, last)
        sol_over = sol - sol_till_last
        sol_till_bat = min(sol_over, bat_lad)
        sol_over -= sol_till_bat
        sol_till_evdc = min(sol_over, evdc_lad)

        rest_last = max(0, last - sol_till_last)
        bat_till_last = min(bat_url, rest_last)
        bat_url_rest = bat_url - bat_till_last
        bat_till_evdc = min(bat_url_rest, max(0, evdc_lad - sol_till_evdc))

        nat_till_bat = max(0, bat_lad - sol_till_bat)
        nat_till_evdc = max(0, evdc_lad - sol_till_evdc - bat_till_evdc)

        if sol_till_bat > 0.0001:
            bat_buffer.append([sol_till_bat * H, sell_price])
        if nat_till_bat > 0.0001:
            bat_buffer.append([nat_till_bat * H, buy_price])

        bat_till_evdc_kr = 0.0
        if bat_till_last > 0.0001:
            pop_lifo_kr(bat_buffer, bat_till_last * H)
        if bat_till_evdc > 0.0001:
            bat_till_evdc_kr = pop_lifo_kr(bat_buffer, bat_till_evdc * H)
        bat_till_nat = bat_url - bat_till_last - bat_till_evdc
        if bat_till_nat > 0.0001:
            pop_lifo_kr(bat_buffer, bat_till_nat * H)

        if sol_till_evdc > 0.0001:
            evdc_buffer.append([sol_till_evdc * H, sell_price])
        if bat_till_evdc > 0.0001:
            bat_avg = bat_till_evdc_kr / (bat_till_evdc * H)
            evdc_buffer.append([bat_till_evdc * H, bat_avg])
        if nat_till_evdc > 0.0001:
            evdc_buffer.append([nat_till_evdc * H, buy_price])

        if evdc_url > 0.0001:
            pop_lifo_kr(evdc_buffer, evdc_url * H)

        # Aggregera per manad
        month_key = r["date_local"][:7]  # YYYY-MM
        if month_key not in monthly:
            monthly[month_key] = {
                "lad_kwh": 0.0, "url_kwh": 0.0,
                "kostnad": 0.0, "sol_kwh": 0.0,
                "bat_kwh": 0.0, "nat_kwh": 0.0
            }
        m = monthly[month_key]
        m["lad_kwh"] += evdc_lad * H
        m["url_kwh"] += evdc_url * H
        m["sol_kwh"] += sol_till_evdc * H
        m["bat_kwh"] += bat_till_evdc * H
        m["nat_kwh"] += nat_till_evdc * H
        m["kostnad"] += sol_till_evdc * sell_price * H
        m["kostnad"] += bat_till_evdc_kr
        m["kostnad"] += nat_till_evdc * buy_price * H

    # Ta senaste 12 manaderna
    sorted_months = sorted(monthly.keys(), reverse=True)[:12]
    sorted_months.reverse()  # kronologisk ordning

    result = []
    for mk in sorted_months:
        m = monthly[mk]
        year, mo = mk.split("-")
        snittpris = m["kostnad"] / m["lad_kwh"] if m["lad_kwh"] > 0.01 else 0
        if m["lad_kwh"] > 0.01:
            sol_p = m["sol_kwh"] / m["lad_kwh"] * 100
            bat_p = m["bat_kwh"] / m["lad_kwh"] * 100
            nat_p = m["nat_kwh"] / m["lad_kwh"] * 100
        else:
            sol_p = bat_p = nat_p = 0
        result.append({
            "month": f"{mo}-{year}",
            "year": int(year),
            "month_num": int(mo),
            "lad_kwh": round(m["lad_kwh"], 1),
            "url_kwh": round(m["url_kwh"], 1),
            "kostnad_kr": round(m["kostnad"], 2),
            "snittpris": round(snittpris, 3),
            "sol_pct": round(sol_p, 1),
            "bat_pct": round(bat_p, 1),
            "nat_pct": round(nat_p, 1)
        })

    # Totals
    total = {
        "lad_kwh": sum(r["lad_kwh"] for r in result),
        "url_kwh": sum(r["url_kwh"] for r in result),
        "kostnad_kr": sum(r["kostnad_kr"] for r in result)
    }
    total["snittpris"] = total["kostnad_kr"] / total["lad_kwh"] if total["lad_kwh"] > 0.01 else 0
    total = {k: round(v, 2) if isinstance(v, float) else v for k, v in total.items()}

    return {"months": result, "total": total}

