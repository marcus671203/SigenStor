#!/usr/bin/env python3
"""
Test-script för Sigen Open API.
Auth via /openapi/auth/login/key med base64(AppKey:AppSecret) i body.
"""

import os
import sys
import json
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── Setup ──────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

BASE       = os.environ["SIGEN_API_BASE"].rstrip("/")
APP_KEY    = os.environ["SIGEN_APP_KEY"]
APP_SECRET = os.environ["SIGEN_APP_SECRET"]
SYSTEM_ID  = os.environ["SIGEN_SYSTEM_ID"]

OUT_DIR = ROOT / "data" / "api_test"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 1. Skaffa access_token ─────────────────────────────────────────
def get_token() -> str:
    """
    OAuth2 Client Credentials enligt Sigen-spec:
    POST /openapi/auth/login/key
    Body: { "key": base64(AppKey:AppSecret) }
    """
    raw = f"{APP_KEY}:{APP_SECRET}"
    key_b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")

    url = f"{BASE}/openapi/auth/login/key"
    print(f"→ POST {url}")
    print(f"   AppKey: {APP_KEY}")
    print(f"   key (b64, första 20): {key_b64[:20]}...")

    r = requests.post(
        url,
        json={"key": key_b64},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    print(f"   status={r.status_code}")
    print(f"   body={r.text[:600]}")
    r.raise_for_status()
    data = r.json()

    if data.get("code") not in (0, 200, None):
        print(f"⚠️ API code={data.get('code')} msg={data.get('msg')}")

    # Försök hitta token i vanligaste placeringar
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    token = (
        payload.get("accessToken")
        or payload.get("access_token")
        or payload.get("token")
    )
    if not token:
        print("⚠️ Hittade inget accessToken. Full body:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
        sys.exit(1)
    print(f"✓ Fick token ({len(token)} tecken): {token[:20]}...")

    # Spara hela token-svaret för senare inspektion (expiry etc.)
    (OUT_DIR / "token_response.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False)
    )
    return token

# ── 2. Anropa /history ─────────────────────────────────────────────
def fetch_history(token: str, day: datetime) -> dict:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=1) - timedelta(seconds=1)
    start_ms = int(start.timestamp() * 1000)
    end_ms   = int(end.timestamp() * 1000)

    url = f"{BASE}/openapi/systems/{SYSTEM_ID}/v1/history"
    params = {
        "startTime": start_ms,
        "endTime":   end_ms,
    }
    print(f"\n→ GET {url}")
    print(f"   params={params}")
    r = requests.get(
        url,
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=30,
    )
    print(f"   status={r.status_code}")
    if r.status_code >= 400 or len(r.text) < 200:
        print(f"   body={r.text[:1500]}")
    r.raise_for_status()
    return r.json()

# ── 3. Inspektera & dumpa ──────────────────────────────────────────
def inspect(payload: dict, day: datetime):
    out_file = OUT_DIR / f"history_{day:%Y-%m-%d}.json"
    out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"\n✓ Skrev {out_file}")
    print(f"  Storlek: {out_file.stat().st_size:,} bytes")

    print("\n── TOP-LEVEL FÄLT ──")
    for k, v in payload.items():
        kind = type(v).__name__
        size = f" (len={len(v)})" if hasattr(v, "__len__") else ""
        print(f"  {k}: {kind}{size}")

    # Hitta rad-listan
    rows = None
    for cand in ("data", "list", "rows", "result", "items"):
        v = payload.get(cand)
        if isinstance(v, list) and v:
            rows = v
            print(f"\n  → Rad-lista i '{cand}' ({len(rows)} rader)")
            break
        if isinstance(v, dict):
            for cand2 in ("list", "rows", "records", "items"):
                v2 = v.get(cand2)
                if isinstance(v2, list) and v2:
                    rows = v2
                    print(f"\n  → Rad-lista i '{cand}.{cand2}' ({len(rows)} rader)")
                    break
            if rows:
                break

    if rows:
        print("\n── FÖRSTA RADEN ──")
        print(json.dumps(rows[0], indent=2, ensure_ascii=False))
        print("\n── SISTA RADEN ──")
        print(json.dumps(rows[-1], indent=2, ensure_ascii=False))
        print(f"\n── FÄLTNAMN ── ({len(rows[0])} fält)")
        for k in rows[0].keys():
            print(f"  {k}")
    else:
        print("\n⚠️ Hittade ingen tydlig rad-lista. Hela payload sparad.")

# ── Main ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1:
        day = datetime.strptime(sys.argv[1], "%Y-%m-%d")
    else:
        day = datetime.now() - timedelta(days=1)
    day = day.replace(tzinfo=timezone.utc)

    print(f"=== Sigen API test — dag: {day:%Y-%m-%d} ===")
    token = get_token()
    payload = fetch_history(token, day)
    inspect(payload, day)
