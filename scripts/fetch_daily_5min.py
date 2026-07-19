#!/usr/bin/env python3
"""
Hämta Sigens EGNA 5-min-aggregat för en dag och skriv till energy_5min_v2.

Källa: /data-process/sigen/station/statistics/v1/energy (samma som webbappen använder)

TECKENKONVENTION (mail-fetch-kompatibel):
   last_kw  > 0 → konsumtion
   bat_kw   > 0 → laddning (API_BATTERY < 0)  [invertat!]
   bat_kw   < 0 → urladdning (API_BATTERY > 0)
   grid_kw  ? beroende — vi speglar API:s tecken tills vidare
   pv3_kw   > 0 → tredjepart-inverter producerar
   evdc_kw  > 0 → TBD

Usage:
   venv-sigen-api/bin/python scripts/fetch_daily_5min.py                # gårdagen
   venv-sigen-api/bin/python scripts/fetch_daily_5min.py 2026-06-28     # specifik dag
   venv-sigen-api/bin/python scripts/fetch_daily_5min.py 2026-06-25 2026-06-30
"""

# User-Agent patch för att undvika CloudFront-blockering
# Sigen-paketet skickar inga headers vilket CloudFront blockerar som bot
import aiohttp as _aiohttp_ua_patch
_orig_ua_init = _aiohttp_ua_patch.ClientSession.__init__
def _patched_ua_init(self, *args, **kwargs):
    headers = kwargs.get('headers') or {}
    if isinstance(headers, dict):
        headers.setdefault('User-Agent', 'okhttp/4.12.0')
        headers.setdefault('Accept', 'application/json, */*')
    kwargs['headers'] = headers
    _orig_ua_init(self, *args, **kwargs)
_aiohttp_ua_patch.ClientSession.__init__ = _patched_ua_init


import asyncio
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("fetch_daily")

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "data" / "sigen.db"

USERNAME = os.environ["SIGEN_USERNAME"]
PASSWORD = os.environ["SIGEN_PASSWORD"]
REGION = os.environ.get("SIGEN_REGION", "eu")

# Sigen series-id → (kolumn, tecken)
# tecken +1 = spegla API, -1 = invertera för mail-kompatibilitet
SERIES_MAP = {
    "FROM_SOLAR":           ("pv_sigen_kw", +1),   # ny — sigen-inverter (0 för oss)
    "FROM_THIRD_PARTY_INV": ("pv3_kw",      +1),   # tredjepart-inverter
    "TO_LOAD":              ("last_kw",     -1),   # konsumtion
    "BATTERY":              ("bat_kw",      +1),   # invertera! api urladdning=+, mail=-
    "GRID":                 ("grid_kw",     +1),   # api köp=+, mail =? tills vidare speglar vi
    "TO_EVDC":              ("evdc_in_kw",  +1),   # laddning EVDC
    "FROM_EVDC":            ("evdc_ur_kw",  +1),   # V2H — EVDC ger effekt
}


async def fetch_day(sigen, target_date: date) -> dict:
    import aiohttp
    date_str = target_date.strftime("%Y%m%d")
    url = f"{sigen.BASE_URL}data-process/sigen/station/statistics/v1/energy"
    params = {
        "dateFlag": 1,
        "startDate": date_str,
        "endDate": date_str,
        "stationId": sigen.station_id,
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=sigen.headers, params=params) as r:
            payload = await r.json()

    if payload.get("code") != 0:
        raise RuntimeError(f"API returnerade fel: {payload}")

    chart = payload["data"]["chartData"]

    rows: dict[str, dict[str, float]] = {}
    for series in chart["dataSeries"]:
        mapping = SERIES_MAP.get(series["id"])
        if not mapping:
            log.warning(f"Okänd series-id: {series['id']} — hoppar över")
            continue
        column, sign = mapping
        for pt in series["points"]:
            t = pt["time"]  # "20260630 14:35"
            ts_local = f"{t[:4]}-{t[4:6]}-{t[6:8]}T{t[9:11]}:{t[12:14]}:00"
            rows.setdefault(ts_local, {})[column] = float(pt["value"]) * sign

    return rows


def upsert_rows(rows: dict[str, dict], target_date: date):
    date_local = target_date.isoformat()
    inserted = 0
    updated = 0

    with sqlite3.connect(DB_PATH) as conn:
        existing = {r[0] for r in conn.execute(
            "SELECT ts_local FROM energy_5min_v2 WHERE date_local = ?",
            (date_local,),
        )}

        for ts_local, vals in sorted(rows.items()):
            last_kw = vals.get("last_kw", 0.0)
            bat_kw = vals.get("bat_kw", 0.0)
            grid_kw = vals.get("grid_kw", 0.0)
            pv3_kw = vals.get("pv3_kw", 0.0)
            evdc_kw = vals.get("evdc_ur_kw", 0.0) + vals.get("evdc_in_kw", 0.0)

            if ts_local in existing:
                conn.execute("""
                    UPDATE energy_5min_v2
                    SET last_kw = ?, bat_kw = ?, evdc_kw = ?, grid_kw = ?, pv3_kw = ?,
                        sample_count = 288, source = 'api_5min_native'
                    WHERE ts_local = ?
                """, (last_kw, bat_kw, evdc_kw, grid_kw, pv3_kw, ts_local))
                updated += 1
            else:
                conn.execute("""
                    INSERT INTO energy_5min_v2
                        (ts_local, date_local, last_kw, bat_kw, evdc_kw, grid_kw, pv3_kw,
                         sample_count, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 288, 'api_5min_native')
                """, (ts_local, date_local, last_kw, bat_kw, evdc_kw, grid_kw, pv3_kw))
                inserted += 1

        conn.commit()
    return inserted, updated





def parse_args() -> list[date]:
    """Parsa CLI-argument till lista av dagar."""
    if len(sys.argv) == 1:
        # Default: idag + igår (idag först så uppdateras oftast)
        today = date.today()
        return [today, today - timedelta(days=1)]
    if len(sys.argv) == 2:
        return [date.fromisoformat(sys.argv[1])]
    if len(sys.argv) == 3:
        start = date.fromisoformat(sys.argv[1])
        end = date.fromisoformat(sys.argv[2])
        days = []
        d = start
        while d <= end:
            days.append(d)
            d += timedelta(days=1)
        return days
    raise SystemExit("Usage: fetch_daily_5min.py [date] [end_date]")

async def main():
    dates = parse_args()
    log.info(f"Hämtar {len(dates)} dag(ar): {dates[0]} → {dates[-1]}")

    from sigen import Sigen
    sigen = Sigen(username=USERNAME, password=PASSWORD, region=REGION)
    await sigen.async_initialize()

    total_ins = 0
    total_upd = 0
    for d in dates:
        if len(dates) > 5:
            await asyncio.sleep(1)  # rate-limit safety
        try:
            rows = await fetch_day(sigen, d)
            ins, upd = upsert_rows(rows, d)
            total_ins += ins
            total_upd += upd
            log.info(f"  {d}: {len(rows)} rader → {ins} inserted, {upd} updated")
        except Exception as e:
            log.error(f"  {d}: FAIL — {type(e).__name__}: {e}")

    log.info(f"✓ Klart: {total_ins} nya, {total_upd} uppdaterade")


if __name__ == "__main__":
    asyncio.run(main())
