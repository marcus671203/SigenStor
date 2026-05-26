"""
Daglig aggregering – körs varje natt.

Läser gårdagens JSONL-rådata, hämtar spotpriser från elprisetjustnu.se,
beräknar besparing enligt sol-prioritet-modellen, sparar i daily/YYYY-MM-DD.json
och uppdaterar månads-Excel.
"""

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("daily")

TZ = ZoneInfo("Europe/Stockholm")

# === Prisformler (nytt avtal fr.o.m. 2026-04-02) ===
NEW_PRICE_START = datetime(2026, 4, 2, tzinfo=TZ)


def inkop(spot: float, ts: datetime) -> float:
    """Inköpspris per kWh inkl moms."""
    if ts >= NEW_PRICE_START:
        return 1.25 * (spot + 0.604) + 0.04
    return 1.25 * spot + 0.935


def forsalj(spot: float) -> float:
    """Försäljningspris per kWh."""
    return spot + 0.104


def fetch_spot(date: datetime.date) -> dict:
    """Hämta spotpriser för SE3 från elprisetjustnu.se."""
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{date.year}/{date.month:02d}-{date.day:02d}_SE3.json"
    log.info("Hämtar spotpriser: %s", url)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    # Mappa till {datetime: SEK/kWh}
    spot = {}
    for item in data:
        dt = datetime.fromisoformat(item["time_start"])
        spot[dt.astimezone(TZ).replace(tzinfo=None)] = item["SEK_per_kWh"]
    return spot


def quarter_floor(dt: datetime) -> datetime:
    """Golv till närmaste 15-min för spot-uppslag."""
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def process_day(date_str: str) -> dict | None:
    """Kör hela kr-beräkningen för en dag."""
    raw_file = Path("data") / "raw" / f"{date_str}.jsonl"
    if not raw_file.exists():
        log.warning("Ingen rådata för %s", date_str)
        return None

    # Läs alla pollningar för dagen
    records = []
    with open(raw_file) as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))

    if len(records) < 100:
        log.warning("Bara %d pollningar för %s – för få för analys", len(records), date_str)
        return None

    log.info("Läste %d pollningar för %s", len(records), date_str)

    # Konvertera till DataFrame
    rows = []
    for rec in records:
        ts = datetime.fromisoformat(rec["ts_local"]).replace(tzinfo=None)
        flow = rec["flow"]
        rows.append({
            "ts": ts,
            "sol": (flow.get("pvPower") or 0) + (flow.get("thirdPvPower") or 0),
            "last": flow.get("loadPower") or 0,
            "bat": flow.get("batteryPower") or 0,
            "nat": flow.get("buySellPower") or 0,
            "evdc": -(flow.get("evPower") or 0),  # EVDC < 0 = laddas i din modell
            "soc": flow.get("batterySoc") or 0,
        })

    df = pd.DataFrame(rows).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)

    # Beräkna delta-t mellan pollningar (de kan vara ojämna)
    df["next_ts"] = df["ts"].shift(-1)
    df["dt_h"] = (df["next_ts"] - df["ts"]).dt.total_seconds() / 3600
    # Sista raden saknar next - sätt till medianvärde
    median_dt = df["dt_h"].median()
    df["dt_h"] = df["dt_h"].fillna(median_dt)

    # Klipp till dagens datum (filtrera bort om något smitit in från annan dag)
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    df = df[df["ts"].dt.date == target_date].copy().reset_index(drop=True)

    if len(df) == 0:
        return None

    # Hämta spotpriser
    try:
        spot = fetch_spot(target_date)
    except Exception as e:
        log.error("Kunde inte hämta spotpriser: %s", e)
        return None

    df["quarter"] = df["ts"].apply(quarter_floor)
    df["spot"] = df["quarter"].map(spot)
    if df["spot"].isna().any():
        log.warning("Saknade spotpriser för %d rader", df["spot"].isna().sum())
        df = df.dropna(subset=["spot"]).reset_index(drop=True)

    # Inkop/forsalj
    df["inkop"] = df.apply(lambda r: inkop(r["spot"], r["ts"].replace(tzinfo=TZ)), axis=1)
    df["forsalj"] = df["spot"].apply(forsalj)

    # Energi (kWh) per intervall
    df["last_e"] = df["last"] * df["dt_h"]
    df["evdc_e"] = df["evdc"] * df["dt_h"]
    df["bat_e"] = df["bat"] * df["dt_h"]
    df["sol_e"] = df["sol"] * df["dt_h"]

    df["bat_url"] = df["bat_e"].clip(lower=0)
    df["bat_lad"] = (-df["bat_e"]).clip(lower=0)
    df["evdc_in"] = (-df["evdc_e"]).clip(lower=0)
    df["load"] = df["last_e"] + df["evdc_in"]

    # Sol-prioritet
    df["stl"] = df[["sol_e", "load"]].min(axis=1)
    df["s_rem"] = df["sol_e"] - df["stl"]
    df["stb"] = df[["s_rem", "bat_lad"]].min(axis=1)
    df["stg"] = df["s_rem"] - df["stb"]
    df["load_rem"] = df["load"] - df["stl"]
    df["btl"] = df[["bat_url", "load_rem"]].min(axis=1)
    df["btg"] = df["bat_url"] - df["btl"]
    df["gtb"] = df["bat_lad"] - df["stb"]
    df["gtl"] = df["load_rem"] - df["btl"]

    # Kr per kategori
    df["bat_lad_nat"] = -df["gtb"] * df["inkop"]
    df["bat_lad_sol"] = df["stb"] * df["forsalj"]
    df["bat_lad_sol_avdr"] = -df["stb"] * df["forsalj"]
    df["bat_url_nat"] = df["btg"] * df["forsalj"]
    df["bat_url_last"] = df["btl"] * df["inkop"]
    df["sol_last"] = df["stl"] * df["inkop"]
    df["sol_nat"] = df["stg"] * df["forsalj"]

    sol_kr = (df["bat_lad_sol"] + df["sol_last"] + df["sol_nat"]).sum()
    bat_kr = (df["bat_lad_nat"] + df["bat_lad_sol_avdr"] + df["bat_url_nat"] + df["bat_url_last"]).sum()
    total = sol_kr + bat_kr

    result = {
        "date": date_str,
        "rows": len(df),
        "energy_kwh": {
            "sol": float(df["sol_e"].sum()),
            "last": float(df["last_e"].sum()),
            "evdc": float(df["evdc_in"].sum()),
            "import": float((df["nat"].clip(lower=0) * df["dt_h"]).sum()),
            "export": float((-df["nat"].clip(upper=0) * df["dt_h"]).sum()),
            "bat_lad": float(df["bat_lad"].sum()),
            "bat_url": float(df["bat_url"].sum()),
        },
        "spot": {
            "min": float(df["spot"].min()),
            "max": float(df["spot"].max()),
            "mean": float(df["spot"].mean()),
        },
        "savings_kr": {
            "sol": float(sol_kr),
            "bat": float(bat_kr),
            "total": float(total),
        },
        "details": {
            "bat_lad_nat": float(df["bat_lad_nat"].sum()),
            "bat_lad_sol": float(df["bat_lad_sol"].sum()),
            "bat_url_nat": float(df["bat_url_nat"].sum()),
            "bat_url_last": float(df["bat_url_last"].sum()),
            "sol_last": float(df["sol_last"].sum()),
            "sol_nat": float(df["sol_nat"].sum()),
        },
    }

    out_dir = Path("data") / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{date_str}.json"
    with open(out_file, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    log.info(
        "%s: Sol=%.2f kr, Bat=%.2f kr, TOTAL=%.2f kr",
        date_str, sol_kr, bat_kr, total,
    )
    return result


def update_summary():
    """Sammanfatta alla dagar i data/summary.json + en CSV."""
    daily_dir = Path("data") / "daily"
    if not daily_dir.exists():
        return

    rows = []
    for f in sorted(daily_dir.glob("*.json")):
        with open(f) as fh:
            d = json.load(fh)
        rows.append({
            "date": d["date"],
            "sol_kr": d["savings_kr"]["sol"],
            "bat_kr": d["savings_kr"]["bat"],
            "total_kr": d["savings_kr"]["total"],
            "sol_kwh": d["energy_kwh"]["sol"],
            "import_kwh": d["energy_kwh"]["import"],
            "export_kwh": d["energy_kwh"]["export"],
        })

    if not rows:
        return

    df = pd.DataFrame(rows)
    df["month"] = df["date"].str[:7]

    summary = {
        "generated": datetime.now(TZ).isoformat(timespec="seconds"),
        "total_days": len(df),
        "grand_total_kr": float(df["total_kr"].sum()),
        "monthly": df.groupby("month").agg({
            "total_kr": "sum",
            "sol_kr": "sum",
            "bat_kr": "sum",
        }).round(2).reset_index().to_dict(orient="records"),
        "last_30_days": df.sort_values("date").tail(30).to_dict(orient="records"),
    }

    with open(Path("data") / "summary.json", "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    # CSV för enkel nedladdning
    df.to_csv(Path("data") / "all_days.csv", index=False)

    log.info("Sammanfattning: %d dagar, totalt %.2f kr", len(df), df["total_kr"].sum())


def main() -> int:
    # Gårdagen i svensk tid
    now = datetime.now(TZ)
    yesterday = (now - timedelta(days=1)).date()
    date_str = yesterday.strftime("%Y-%m-%d")
    log.info("Aggregerar dag: %s", date_str)

    process_day(date_str)
    update_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
