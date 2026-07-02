"""Calculate daily savings from 5-min energy data + spot prices.

Same model as the Excel: solar-priority allocation, contracts before/after Apr 2 2026.
"""

import os
import sys
import json
import logging
import urllib.request
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from db import connect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("calc")

TZ = ZoneInfo("Europe/Stockholm")
CONTRACT_2026 = date(2026, 1, 1)
CONTRACT_CHANGE = date(2026, 4, 2)
H = 5 / 60.0
SPOT_CACHE = Path(__file__).parent.parent / "data" / "spot_cache"
SPOT_CACHE.mkdir(parents=True, exist_ok=True)


def fetch_spot_prices(target_date):
    cache_file = SPOT_CACHE / f"{target_date.isoformat()}_SE3.json"
    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)
    url = (
        f"https://www.elprisetjustnu.se/api/v1/prices/"
        f"{target_date.year}/{target_date.month:02d}-{target_date.day:02d}_SE3.json"
    )
    log.info("Fetching spot prices from %s", url)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "SigenStor/1.0 (marcus671203@github)"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        with open(cache_file, "w") as f:
            json.dump(data, f)
        return data
    except Exception as e:
        log.error("Failed to fetch spot prices: %s", e)
        return None


def build_spot_lookup(spots):
    lookup = {}
    for s in spots:
        ts = datetime.fromisoformat(s["time_start"])
        key = (ts.date(), ts.hour, ts.minute // 15)
        lookup[key] = s["SEK_per_kWh"]
    # Fyll saknade kvarter med timpriset (historisk timupplöst data)
    for (d, hh, q) in list(lookup.keys()):
        if q == 0:
            base = lookup[(d, hh, 0)]
            for qq in (1, 2, 3):
                lookup.setdefault((d, hh, qq), base)
    return lookup


def buy_price(spot, the_date):
    if the_date < CONTRACT_2026:
        return 1.25 * spot + 0.9532
    if the_date < CONTRACT_CHANGE:
        return 1.25 * spot + 0.935
    return 1.25 * (spot + 0.604) + 0.04


def sell_price(spot, the_date):
    if the_date < CONTRACT_2026:
        return spot + 0.72
    return spot + 0.104


def calculate_day(target_date, evdc_buffer=None):
    """
    evdc_buffer: list av [kwh, price_per_kwh]. Om None skapas en lokal
    (för single-day runs). För --all skickas samma buffer in för alla dagar
    så FIFO fungerar över tid.
    """
    if evdc_buffer is None:
        evdc_buffer = []
    log.info("Calculating savings for %s", target_date.isoformat())

    spots = fetch_spot_prices(target_date)
    if not spots:
        log.error("No spot prices available for %s", target_date)
        return None
    spot_lookup = build_spot_lookup(spots)

    with connect() as conn:
        rows = conn.execute(
            "SELECT ts_local, last_kw, bat_kw, evdc_kw, grid_kw, pv3_kw "
            "FROM energy_5min_v2 WHERE date_local = ? ORDER BY ts_local",
            (target_date.isoformat(),)
        ).fetchall()

    if not rows:
        log.warning("No 5-min data for %s", target_date)
        return None

    log.info("  Found %d 5-min rows", len(rows))

    bat_lad_nat = 0
    bat_lad_sol_avdr = 0
    bat_lad_sol = 0
    bat_url_nat = 0
    bat_url_last = 0
    sol_last = 0
    sol_nat = 0
    evdc_url_last = 0
    evdc_url_nat = 0
    evdc_url_kwh = 0
    evdc_ladd_kostnad = 0  # FIFO-avdrag för dagens V2H
    skipped = 0

    sol_kwh = 0
    last_kwh = 0
    import_kwh = 0
    export_kwh = 0
    bat_lad_kwh = 0
    bat_url_kwh = 0
    evdc_kwh = 0

    spot_min = None
    spot_max = None
    spot_sum = 0
    spot_count = 0

    for row in rows:
        ts = datetime.fromisoformat(row["ts_local"])
        last = row["last_kw"] or 0
        bat = row["bat_kw"] or 0
        evdc = row["evdc_kw"] or 0
        nat = row["grid_kw"] or 0
        sol = row["pv3_kw"] or 0

        evdc_lad = max(0, -evdc)
        evdc_url = max(0, evdc)  # V2H/V2G
        load = last + evdc_lad

        bat_url = max(0, bat)
        bat_lad = max(0, -bat)

        nat_kop = max(0, nat)
        nat_sal = max(0, -nat)

        stl = min(sol, load)
        etl = min(evdc_url, load - stl)
        etg = evdc_url - etl
        stb = min(sol - stl, bat_lad)
        stg = sol - stl - stb

        btl = min(bat_url, load - stl - etl)
        btg = bat_url - btl
        gtb = bat_lad - stb

        spot = spot_lookup.get((ts.date(), ts.hour, ts.minute // 15))
        if spot is None:
            skipped += 1
            continue

        bp = buy_price(spot, target_date)
        sp = sell_price(spot, target_date)

        bat_lad_nat += -gtb * bp * H
        bat_lad_sol_avdr += -stb * sp * H
        bat_lad_sol += stb * sp * H
        bat_url_nat += btg * sp * H
        bat_url_last += btl * bp * H
        sol_last += stl * bp * H
        sol_nat += stg * sp * H
        evdc_url_last += etl * bp * H
        evdc_url_nat += etg * sp * H

        # FIFO-buffer:
        # EVDC-laddning: gör allokering av evdc_lad mellan sol och nät
        # sol delen: kostnad 0, nät delen: kostnad bp
        # OBS: bat_url_evdc räknar vi inte separat (bat är redan bokfört)
        if evdc_lad > 0.0001:
            # Sol-överskott efter sol->last kan gå till EVDC (approx)
            sol_till_evdc = min(sol - stl, evdc_lad)
            nat_till_evdc = evdc_lad - sol_till_evdc - min(bat_url - btl - etl, 0) if False else max(0, evdc_lad - sol_till_evdc)
            # Push till FIFO
            if sol_till_evdc > 0.0001:
                evdc_buffer.append([sol_till_evdc * H, 0.0])   # sol = 0 kr/kWh
            if nat_till_evdc > 0.0001:
                evdc_buffer.append([nat_till_evdc * H, bp])    # nät = bp kr/kWh

        # V2H/V2G: pop från LIFO (senaste laddningar först - fysiskt korrekt för batteri)
        if evdc_url > 0.0001:
            kwh_kvar = evdc_url * H
            kostnad = 0.0
            while kwh_kvar > 0.0001 and evdc_buffer:
                chunk = evdc_buffer[-1]  # sista (nyaste)
                ta = min(chunk[0], kwh_kvar)
                kostnad += ta * chunk[1]
                chunk[0] -= ta
                kwh_kvar -= ta
                if chunk[0] < 0.0001:
                    evdc_buffer.pop()  # pop sista
            evdc_ladd_kostnad -= kostnad  # negativt avdrag

        sol_kwh += sol * H
        last_kwh += load * H
        import_kwh += nat_kop * H
        export_kwh += nat_sal * H
        bat_lad_kwh += bat_lad * H
        bat_url_kwh += bat_url * H
        evdc_kwh += evdc_lad * H
        evdc_url_kwh += evdc_url * H

        spot_min = spot if spot_min is None else min(spot_min, spot)
        spot_max = spot if spot_max is None else max(spot_max, spot)
        spot_sum += spot
        spot_count += 1

    if skipped:
        log.warning("  Skipped %d rows due to missing spot prices", skipped)

    sol_kr = sol_last + sol_nat + bat_lad_sol
    bat_kr = bat_lad_nat + bat_lad_sol_avdr + bat_url_nat + bat_url_last
    evdc_kr = evdc_url_last + evdc_url_nat + evdc_ladd_kostnad  # V2H-värde + FIFO-avdrag
    total_kr = sol_kr + bat_kr + evdc_kr
    spot_mean = spot_sum / spot_count if spot_count else None

    return {
        "date": target_date.isoformat(),
        "rows": len(rows),
        "sol_kwh": sol_kwh,
        "last_kwh": last_kwh,
        "import_kwh": import_kwh,
        "export_kwh": export_kwh,
        "bat_lad_kwh": bat_lad_kwh,
        "bat_url_kwh": bat_url_kwh,
        "evdc_kwh": evdc_kwh,
        "spot_min": spot_min,
        "spot_max": spot_max,
        "spot_mean": spot_mean,
        "sol_kr": sol_kr,
        "bat_kr": bat_kr,
        "total_kr": total_kr,
        "bat_lad_nat": bat_lad_nat,
        "bat_lad_sol": bat_lad_sol,
        "bat_url_nat": bat_url_nat,
        "bat_url_last": bat_url_last,
        "sol_last": sol_last,
        "sol_nat": sol_nat,
        "evdc_url_kwh": evdc_url_kwh,
        "evdc_url_last": evdc_url_last,
        "evdc_url_nat": evdc_url_nat,
        "evdc_ladd_kostnad": evdc_ladd_kostnad,
        "evdc_kr": evdc_kr,
    }


def save_to_db(r):
    now = datetime.now(TZ).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO daily_summary (
                date, rows, sol_kwh, last_kwh, import_kwh, export_kwh,
                bat_lad_kwh, bat_url_kwh, evdc_kwh,
                spot_min, spot_max, spot_mean,
                sol_kr, bat_kr, total_kr,
                bat_lad_nat, bat_lad_sol, bat_url_nat, bat_url_last,
                sol_last, sol_nat,
                evdc_url_kwh, evdc_url_last, evdc_url_nat, evdc_kr,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            r["date"], r["rows"], r["sol_kwh"], r["last_kwh"], r["import_kwh"], r["export_kwh"],
            r["bat_lad_kwh"], r["bat_url_kwh"], r["evdc_kwh"],
            r["spot_min"], r["spot_max"], r["spot_mean"],
            r["sol_kr"], r["bat_kr"], r["total_kr"],
            r["bat_lad_nat"], r["bat_lad_sol"], r["bat_url_nat"], r["bat_url_last"],
            r["sol_last"], r["sol_nat"], r["evdc_url_kwh"], r["evdc_url_last"], r["evdc_url_nat"], r["evdc_kr"], now
        ))


def calculate_recent_days(n_days=7):
    with connect() as conn:
        dates = conn.execute(
            "SELECT DISTINCT date_local FROM energy_5min_v2 ORDER BY date_local DESC LIMIT ?",
            (n_days,)
        ).fetchall()
    for row in dates:
        d = date.fromisoformat(row["date_local"])
        result = calculate_day(d)
        if result:
            save_to_db(result)
            log.info("  %s: Sol=%.2f Bat=%.2f Total=%.2f kr",
                     result["date"], result["sol_kr"], result["bat_kr"], result["total_kr"])


def calculate_missing():
    """Calculate days that exist in energy_5min but not in daily_summary."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT DISTINCT date_local FROM energy_5min_v2
            WHERE date_local NOT IN (SELECT date FROM daily_summary)
            ORDER BY date_local
        """).fetchall()
    if not rows:
        log.info("No missing dates.")
        return 0
    log.info("Found %d missing date(s)", len(rows))
    for row in rows:
        d = date.fromisoformat(row["date_local"])
        result = calculate_day(d)
        if result:
            save_to_db(result)
            log.info("  %s: Sol=%.2f Bat=%.2f Total=%.2f kr",
                     result["date"], result["sol_kr"], result["bat_kr"], result["total_kr"])
    return len(rows)


def calculate_stale():
    """Calculate days where energy_5min.rows != daily_summary.rows.

    This is self-healing - any day where new 5-min data has been added
    (e.g. backfilling a partial day) will get its summary recomputed.
    """
    with connect() as conn:
        # Get current row counts per date in energy_5min
        actual = conn.execute("""
            SELECT date_local, COUNT(*) as actual_rows
            FROM energy_5min_v2
            GROUP BY date_local
        """).fetchall()

        # Get stored row counts per date in daily_summary
        stored = conn.execute("""
            SELECT date, rows as stored_rows FROM daily_summary
        """).fetchall()
        stored_map = {r["date"]: r["stored_rows"] for r in stored}

    stale_dates = []
    for row in actual:
        d = row["date_local"]
        actual_rows = row["actual_rows"]
        stored_rows = stored_map.get(d)
        if stored_rows is None:
            continue  # missing, handled by calculate_missing()
        if actual_rows != stored_rows:
            stale_dates.append((d, actual_rows, stored_rows))

    if not stale_dates:
        log.info("No stale dates - all summaries are up to date.")
        return 0

    log.info("Found %d stale date(s) where row count has changed:", len(stale_dates))
    for d, actual_rows, stored_rows in stale_dates:
        log.info("  %s: was %d rows, now %d rows", d, stored_rows, actual_rows)

    for d, _, _ in stale_dates:
        target = date.fromisoformat(d)
        result = calculate_day(target)
        if result:
            save_to_db(result)
            log.info("  %s: Sol=%.2f Bat=%.2f Total=%.2f kr",
                     result["date"], result["sol_kr"], result["bat_kr"], result["total_kr"])
    return len(stale_dates)


def calculate_all():
    """Recalculate all dates from scratch. Use after model changes."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT date_local FROM energy_5min_v2 ORDER BY date_local"
        ).fetchall()
    log.info("Recalculating %d date(s) with FIFO EVDC buffer", len(rows))
    evdc_buffer = []  # FIFO-buffer spänner hela historien
    for row in rows:
        d = date.fromisoformat(row["date_local"])
        result = calculate_day(d, evdc_buffer=evdc_buffer)
        if result:
            save_to_db(result)
            log.info("  %s: Sol=%.2f Bat=%.2f EVDC=%.2f Total=%.2f kr (fifo=%.1f kWh)",
                     result["date"], result["sol_kr"], result["bat_kr"],
                     result["evdc_kr"], result["total_kr"],
                     sum(c[0] for c in evdc_buffer))


def calculate_auto():
    """Default scheduled run: handle missing + stale dates."""
    missing = calculate_missing()
    stale = calculate_stale()
    log.info("Auto run complete: %d missing, %d stale recalculated", missing, stale)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--missing":
            calculate_missing()
        elif arg == "--stale":
            calculate_stale()
        elif arg == "--auto":
            calculate_auto()
        elif arg == "--all":
            calculate_all()
        elif arg == "--recent":
            calculate_recent_days(7)
        else:
            target = date.fromisoformat(arg)
            result = calculate_day(target)
            if result:
                save_to_db(result)
                print(f"\n{result['date']}: Sol={result['sol_kr']:.2f} Bat={result['bat_kr']:.2f} Total={result['total_kr']:.2f} kr")
    else:
        calculate_auto()
