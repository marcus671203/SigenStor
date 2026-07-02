"""Polls Sigen Cloud API and writes to SQLite database."""

import asyncio
import json
import os
import sys
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
from db import init_db, insert_polling

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("poll")

TZ = ZoneInfo("Europe/Stockholm")


async def main() -> int:
    username = os.environ.get("SIGEN_USERNAME")
    password = os.environ.get("SIGEN_PASSWORD")
    region = os.environ.get("SIGEN_REGION", "eu")

    if not username or not password:
        log.error("SIGEN_USERNAME and SIGEN_PASSWORD must be set")
        return 1

    init_db()

    try:
        from sigen import Sigen
    except ImportError:
        log.error("sigen package missing")
        return 1

    log.info("Logging in (region=%s)...", region)
    sigen = Sigen(username=username, password=password, region=region)
    try:
        await sigen.async_initialize()
    except Exception as e:
        log.error("Login failed: %s", e)
        return 2

    log.info("Fetching energy_flow...")
    try:
        flow = await sigen.get_energy_flow()
    except Exception as e:
        log.error("get_energy_flow failed: %s", e)
        return 3

    try:
        mode = await sigen.get_operational_mode()
    except Exception:
        mode = None

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TZ)

    ts_utc = now_utc.isoformat(timespec="seconds")
    ts_local = now_local.isoformat(timespec="seconds")
    raw_json = json.dumps({"flow": flow, "mode": mode}, ensure_ascii=False, default=str)

    insert_polling(ts_utc, ts_local, flow, mode, raw_json)

    log.info(
        "Saved | SoC=%.1f%% Sol=%.2fkW Last=%.2fkW Grid=%.2fkW Bat=%.2fkW",
        flow.get("batterySoc", 0),
        (flow.get("pvPower") or 0) + (flow.get("thirdPvPower") or 0),
        flow.get("loadPower", 0),
        flow.get("buySellPower", 0),
        flow.get("batteryPower", 0),
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
