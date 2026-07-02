#!/usr/bin/env python3
"""
Aggregera realtime-pollar (30s) till 5-min-snitt → energy_5min_v2.

Räknar fram alla 5-min-fönster som har poll-data men ännu inte
finns aggregerade. Designad för idempotens — säker att köra ofta.

Run manually:
   venv-sigen-api/bin/python scripts/aggregate_5min.py

Or via systemd timer (var 5:e min).
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger("aggregator")

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "sigen.db"
TZ_LOCAL = ZoneInfo("Europe/Stockholm")

# Minst så här många polls per 5-min för att räkna fönstret komplett
MIN_SAMPLES = 20   # ideal 30, min 20 (~2/3 av fönstret täckt)


def floor_to_5min(dt_str: str) -> str:
    """'2026-06-30T15:04:44' -> '2026-06-30T15:00:00'"""
    dt = datetime.fromisoformat(dt_str)
    floored = dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)
    return floored.strftime("%Y-%m-%dT%H:%M:%S")


def main():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        # 1) Identifiera 5-min-fönster med tillräckligt med polls,
        #    som ännu inte finns i v2-tabellen.
        rows = conn.execute("""
            SELECT
                strftime('%Y-%m-%dT%H:', ts_local) ||
                printf('%02d:00', (CAST(substr(ts_local, 15, 2) AS INTEGER) / 5) * 5)
                    AS bucket,
                substr(ts_local, 1, 10) AS date_local,
                COUNT(*) AS n,
                AVG(load_kw)      AS last_kw,
                AVG(battery_kw)   AS bat_kw,
                AVG(ev_power_kw)  AS evdc_kw,
                AVG(buy_sell_kw)  AS grid_kw,
                AVG(third_pv_kw)  AS pv3_kw
            FROM realtime
            WHERE pv_power_kw IS NOT NULL OR third_pv_kw IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket
        """).fetchall()

        log.info(f"Hittade {len(rows)} 5-min-fönster i realtime-datan")

        # 2) Filtrera: tillräckligt många polls + inte redan finns i v2
        existing = {r["ts_local"] for r in
                    conn.execute("SELECT ts_local FROM energy_5min_v2").fetchall()}

        inserted = 0
        skipped_few = 0
        skipped_exists = 0
        skipped_now = 0

        # Skippa det allra senaste fönstret (pågående 5-min — vänta tills komplett)
        now_floor = floor_to_5min(datetime.now(TZ_LOCAL).strftime("%Y-%m-%dT%H:%M:%S"))

        for r in rows:
            bucket = r["bucket"]
            if bucket >= now_floor:
                skipped_now += 1
                continue
            if r["n"] < MIN_SAMPLES:
                skipped_few += 1
                continue
            if bucket in existing:
                skipped_exists += 1
                continue

            # OBS: I energy_5min lagras NEGATIVT bat_kw när batteriet LADDAS.
            # I realtime är batteryPower POSITIVT när det laddas. Vi måste
            # invertera tecken för att matcha mail-fetch-konventionen.
            # bat_kw_mail = -battery_kw_api
            bat_kw_api = r["bat_kw"]
            bat_kw_mail = -bat_kw_api if bat_kw_api is not None else None

            # Samma sak för grid_kw: i realtime är buySellPower NEGATIVT när
            # vi säljer (matar nätet). Mail-fetchen har positivt = sälja (?)
            # Vi behåller realtime-konvention för nu och validerar mot mail-data.
            # TODO: bekräfta tecken-konvention vs mail-data efter första körning.
            grid_kw_api = r["grid_kw"]

            conn.execute("""
                INSERT INTO energy_5min_v2
                    (ts_local, date_local, last_kw, bat_kw, evdc_kw, grid_kw, pv3_kw, sample_count, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'api_5min_agg')
            """, (
                bucket, r["date_local"],
                r["last_kw"], bat_kw_mail, r["evdc_kw"], grid_kw_api, r["pv3_kw"],
                r["n"],
            ))
            inserted += 1

        conn.commit()

        log.info(f"✓ Infogade {inserted} nya 5-min-aggregat")
        log.info(f"  Skippad (för få polls < {MIN_SAMPLES}): {skipped_few}")
        log.info(f"  Skippad (finns redan):                  {skipped_exists}")
        log.info(f"  Skippad (pågående fönster):             {skipped_now}")


if __name__ == "__main__":
    main()
