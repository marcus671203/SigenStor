#!/usr/bin/env python3
"""
Probing-script för att hitta Sigens historiska data-endpoints.

Använder samma auth-token som sigen-paketet redan hämtat, och testar
sannolika URL-mönster med olika parameter-kombinationer.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import aiohttp

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OUT_DIR = ROOT / "data" / "sigen_history_probe"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Sannolika endpoints att testa
ENDPOINTS_TO_PROBE = [
    # Data-process / statistik
    "data-process/sigen/station/statistics/energy-flow-curve",
    "data-process/sigen/station/statistics/history",
    "data-process/sigen/station/statistics/day-curve",
    "data-process/sigen/station/statistics/energyflow-curve",
    "data-process/sigen/station/statistics/power-curve",
    "data-process/sigen/station/energy-flow/history",
    "data-process/sigen/station/energy-flow/curve",
    "data-process/sigen/station/statistics/day-energy-flow",
    # Device-mönster
    "device/sigen/station/statistics/history",
    "device/sigen/station/statistics/day",
    "device/sigen/station/energyflow/history",
    "device/sigen/station/energyflow/curve",
    "device/sigen/station/statistics/energy-flow",
    "device/sigen/station/day-statistics",
    # Openapi-mönster (från developer.sigencloud.com)
    "openapi/v1/station/energy-flow/history",
]

# Parameter-uppsättningar att testa
PARAM_SETS = [
    # Set 1: bara stationId + date
    lambda sid, date_str: {"stationId": sid, "date": date_str},
    # Set 2: id + date
    lambda sid, date_str: {"id": sid, "date": date_str},
    # Set 3: stationId + startTime + endTime (ISO)
    lambda sid, date_str: {
        "stationId": sid,
        "startTime": f"{date_str}T00:00:00",
        "endTime": f"{date_str}T23:59:59",
    },
    # Set 4: dagens dag + upplösning
    lambda sid, date_str: {
        "stationId": sid,
        "date": date_str,
        "granularity": "5min",
    },
    # Set 5: som Set 3 men med "resolution"
    lambda sid, date_str: {
        "stationId": sid,
        "startTime": f"{date_str}T00:00:00",
        "endTime": f"{date_str}T23:59:59",
        "resolution": "MINUTE_5",
    },
]


async def probe_url(session, base_url, endpoint, params, headers):
    """Testa en enskild URL, returnera (status, body_kort)."""
    url = f"{base_url}{endpoint}"
    try:
        async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            body = await r.text()
            return r.status, body[:500]
    except asyncio.TimeoutError:
        return "TIMEOUT", ""
    except Exception as e:
        return f"ERR", str(e)[:200]


async def main():
    from sigen import Sigen
    username = os.environ["SIGEN_USERNAME"]
    password = os.environ["SIGEN_PASSWORD"]
    region = os.environ.get("SIGEN_REGION", "eu")

    print(f"→ Ansluter som {username}...")
    sigen = Sigen(username=username, password=password, region=region)
    await sigen.async_initialize()
    print(f"✓ Auth OK. station_id={sigen.station_id}")

    # Använd gårdagens datum (garanterat att det finns data)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"→ Testar för datum {yesterday}\n")

    hits = []

    async with aiohttp.ClientSession() as session:
        for endpoint in ENDPOINTS_TO_PROBE:
            for i, param_fn in enumerate(PARAM_SETS):
                params = param_fn(sigen.station_id, yesterday)
                status, body = await probe_url(
                    session, sigen.BASE_URL, endpoint, params, sigen.headers
                )
                marker = "?"
                if status == 200:
                    # Check if body actually contains data
                    try:
                        j = json.loads(body)
                        if j.get("code") == 0 and j.get("data"):
                            marker = "✅ HIT!"
                            hits.append((endpoint, params, body))
                            # Spara även fulla svaret
                            safe_name = endpoint.replace("/", "_")
                            (OUT_DIR / f"{safe_name}__set{i}.json").write_text(body)
                        else:
                            marker = "○"
                    except Exception:
                        marker = "!"
                elif status == 404:
                    marker = "×"
                elif status in (401, 403):
                    marker = "🔒"
                else:
                    marker = str(status)
                
                params_short = ",".join(f"{k}={v}" for k, v in list(params.items())[:2])
                print(f"  [{marker}] set{i} {endpoint:70s}  ({params_short})")

    print(f"\n=== SAMMANFATTNING ===")
    print(f"Testade: {len(ENDPOINTS_TO_PROBE) * len(PARAM_SETS)} kombinationer")
    print(f"Träffar: {len(hits)}")
    if hits:
        print("\n=== FÖRSTA TRÄFF ===")
        endpoint, params, body = hits[0]
        print(f"Endpoint:  {endpoint}")
        print(f"Params:    {params}")
        print(f"Body (500 första tecken):\n{body}")
        print(f"\n→ Sparat alla träffar i: {OUT_DIR}")

if __name__ == "__main__":
    asyncio.run(main())
