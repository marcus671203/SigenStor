#!/usr/bin/env python3
"""
Utforskningsscript för pip install sigen v0.1.9.
Anropar alla tillgängliga read-metoder och dumpar resultaten.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("explore")

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OUT_DIR = ROOT / "data" / "sigen_api_explore"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def dump(name, data):
    print(f"\n=== {name} ===")
    print(json.dumps(data, indent=2, default=str, ensure_ascii=False))
    (OUT_DIR / f"{name}.json").write_text(
        json.dumps(data, indent=2, default=str, ensure_ascii=False)
    )


async def main():
    from sigen import Sigen

    username = os.environ["SIGEN_USERNAME"]
    password = os.environ["SIGEN_PASSWORD"]

    log.info(f"Connecting as {username}...")
    sigen = Sigen(username=username, password=password, region="eu")

    # Initialize
    await sigen.async_initialize()
    log.info("✓ Initialized")

    # === Försök varje read-metod, fånga fel individuellt ===
    methods_to_try = [
        ('fetch_station_info', None),
        ('get_energy_flow', None),
        ('get_operational_mode', None),
        ('get_operational_modes', None),
        ('fetch_operational_modes', None),
        ('get_signals', None),
        ('get_ac_ev_charge_mode', None),
        ('get_ac_ev_current', None),
    ]

    for i, (method_name, args) in enumerate(methods_to_try, start=1):
        try:
            fn = getattr(sigen, method_name)
            if args is None:
                result = await fn()
            else:
                result = await fn(*args)
            dump(f"{i:02d}_{method_name}", result)
            log.info(f"✓ {method_name}")
        except Exception as e:
            log.warning(f"⚠️ {method_name} FAILED: {type(e).__name__}: {e}")

    log.info(f"\n✓ Klart. JSON-dumpar sparade i: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
