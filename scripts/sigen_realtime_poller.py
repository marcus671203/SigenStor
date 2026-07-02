#!/usr/bin/env python3
"""
Sigen realtidspollare.
Pollar energy_flow + operational_mode var POLL_INTERVAL_SEC och
lagrar resultatet i `realtime`-tabellen.

Designad för att köra som en långlivad systemd-service.
"""

import asyncio
import json
import logging
import os
import signal
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# === Setup ===
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger("sigen_poll")

TZ_LOCAL = ZoneInfo("Europe/Stockholm")
DB_PATH = ROOT / "data" / "sigen.db"
POLL_INTERVAL_SEC = int(os.environ.get("SIGEN_POLL_INTERVAL_SEC", "10"))
ERROR_BACKOFF_SEC = 60   # vänta 60s vid fel innan ny försök

USERNAME = os.environ["SIGEN_USERNAME"]
PASSWORD = os.environ["SIGEN_PASSWORD"]
REGION = os.environ.get("SIGEN_REGION", "eu")

# === Graceful shutdown ===
shutdown_event = asyncio.Event()

def handle_signal(signum, _frame):
    log.info(f"Caught signal {signum}, shutting down...")
    shutdown_event.set()

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def init_db():
    """Verifiera att realtime-tabellen finns."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='realtime'"
        ).fetchone()
        if not row:
            raise RuntimeError("realtime-tabellen saknas — skapa den först")
    log.info("✓ realtime-tabellen finns")


def insert_sample(flow: dict, op_mode):
    """Skriv en sample till SQLite."""
    now_local = datetime.now(TZ_LOCAL)
    now_utc = now_local.astimezone(timezone.utc)
    ts_local = now_local.strftime("%Y-%m-%dT%H:%M:%S")
    ts_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%S")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO realtime (
                ts_local, ts_utc, pv_power_kw, third_pv_kw, load_kw,
                battery_kw, battery_soc, buy_sell_kw, ev_power_kw,
                ac_power_kw, heatpump_kw, generator_kw, pv_day_kwh,
                op_mode, station_status, on_grid, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts_local, ts_utc,
                flow.get("pvPower"),
                flow.get("thirdPvPower"),
                flow.get("loadPower"),
                flow.get("batteryPower"),
                flow.get("batterySoc"),
                flow.get("buySellPower"),
                flow.get("evPower"),
                flow.get("acPower"),
                flow.get("heatPumpPower"),
                flow.get("generatorPower"),
                flow.get("pvDayNrg"),
                op_mode,
                flow.get("stationStatus"),
                1 if flow.get("onGrid") else 0,
                json.dumps(flow, default=str),
            ),
        )


async def poll_loop():
    """Huvudloop. Reconnects vid behov, exponentiell backoff vid fel."""
    from sigen import Sigen

    sigen = None
    consecutive_errors = 0
    FORCE_REINIT_AFTER = 5   # efter 5 fel i rad, tvinga ny session

    while not shutdown_event.is_set():
        try:
            # Initiera/reinitiera vid behov
            if sigen is None:
                log.info(f"Connecting as {USERNAME}...")
                sigen = Sigen(username=USERNAME, password=PASSWORD, region=REGION)
                await sigen.async_initialize()
                log.info("✓ Sigen-session etablerad")

            # Hämta data
            flow = await sigen.get_energy_flow()

            # Defensiv typkontroll — API returnerar ibland str vid fel
            if not isinstance(flow, dict):
                raise TypeError(
                    f"get_energy_flow returnerade {type(flow).__name__}, "
                    f"förväntat dict. Värde: {str(flow)[:200]}"
                )

            try:
                op_mode = await sigen.get_operational_mode()
                if not isinstance(op_mode, str):
                    op_mode = str(op_mode) if op_mode is not None else None
            except Exception as e:
                log.warning(f"op_mode kunde ej hämtas: {e}")
                op_mode = None

            # Spara
            insert_sample(flow, op_mode)

            soc = flow.get("batterySoc")
            pv_kw = (flow.get("pvPower") or 0) + (flow.get("thirdPvPower") or 0)
            load_kw = flow.get("loadPower")
            log.info(
                f"✓ SOC={soc}%  PV={pv_kw:.2f}kW  Load={load_kw}kW  "
                f"Bat={flow.get('batteryPower')}kW  Mode={op_mode}"
            )
            consecutive_errors = 0

        except Exception as e:
            consecutive_errors += 1
            log.error(f"Polling error ({consecutive_errors}x): {type(e).__name__}: {e}")

            # Trigger-nyckel för reinit — mycket mer aggressivt nu
            err_str = str(e).lower()
            err_type = type(e).__name__.lower()
            needs_reinit = (
                "token" in err_str
                or "auth" in err_str
                or "401" in err_str
                or "attributeerror" in err_type
                or "typeerror" in err_type
                or "keyerror" in err_type
                or consecutive_errors >= FORCE_REINIT_AFTER
            )

            if needs_reinit and sigen is not None:
                log.info(f"Force-reinitierar Sigen-session (efter {consecutive_errors} fel)")
                sigen = None
                consecutive_errors = 0  # nollställ efter reinit
                # Kort paus mellan reinit-försök
                try:
                    await asyncio.wait_for(shutdown_event.wait(), 5)
                except asyncio.TimeoutError:
                    pass
                continue

            # Vid många fel — vänta längre
            if consecutive_errors >= 3:
                log.warning(f"Sleeping {ERROR_BACKOFF_SEC}s pga upprepade fel")
                try:
                    await asyncio.wait_for(shutdown_event.wait(), ERROR_BACKOFF_SEC)
                except asyncio.TimeoutError:
                    pass
                continue

        # Vänta till nästa poll (eller shutdown)
        try:
            await asyncio.wait_for(shutdown_event.wait(), POLL_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass

    log.info("✓ Pollaren stängd")


async def main():
    init_db()
    log.info(f"Startar polling — varje {POLL_INTERVAL_SEC}s")
    await poll_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Avbruten av tangentbord")
