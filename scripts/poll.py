"""Pollar Sigen Cloud API och appendar en rad till dagens råfil."""

import asyncio
import json
import os
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
        log.error("SIGEN_USERNAME och SIGEN_PASSWORD maste vara satta")
        return 1

    try:
        from sigen import Sigen
    except ImportError:
        log.error("sigen-paketet saknas")
        return 1

    log.info("Loggar in (region=%s)...", region)
    sigen = Sigen(username=username, password=password, region=region)
    try:
        await sigen.async_initialize()
    except Exception as e:
        log.error("Inloggning misslyckades: %s", e)
        return 2

    log.info("Hamtar energy_flow...")
    try:
        flow = await sigen.get_energy_flow()
    except Exception as e:
        log.error("get_energy_flow misslyckades: %s", e)
        return 3

    try:
        mode = await sigen.get_operational_mode()
    except Exception:
        mode = None

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TZ)
    record = {
        "ts_utc": now_utc.isoformat(timespec="seconds"),
        "ts_local": now_local.isoformat(timespec="seconds"),
        "flow": flow,
        "mode": mode,
    }

    out_dir = Path("data") / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{now_local.strftime('%Y-%m-%d')}.jsonl"

    with open(out_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    log.info(
        "Sparat: %s | SoC=%.1f%% Sol=%.2fkW Last=%.2fkW Nat=%.2fkW Bat=%.2fkW",
        out_file.name,
        flow.get("batterySoc", 0),
        (flow.get("pvPower") or 0) + (flow.get("thirdPvPower") or 0),
        flow.get("loadPower", 0),
        flow.get("buySellPower", 0),
        flow.get("batteryPower", 0),
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
