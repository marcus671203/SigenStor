#!/usr/bin/env python3
"""
Simulerar vad ett extra batteri hade sparat på din faktiska historik.

Metod: dina femminutersvärden (energy_5min_v2) och spotpriser (data/spot_cache)
används som de är. Det befintliga batteriet får göra exakt det det gjorde.
Det extra batteriet styrs ovanpå, mot nätflödet som blev kvar:
  - det kan ladda från överskott som annars såldes, eller från nätet,
  - det kan ladda ur, men bara så mycket som annars köptes (inget säljs från det).
Styrningen optimeras med dynamisk programmering och två dygns framförhållning.
Det är perfekt framförhållning, alltså en ÖVRE GRÄNS för vad som går att tjäna.

Kör från repots rot:  venv/bin/python scripts/sim_extra_battery.py
Valfritt:  --kwh 10  --kw 5  --pris 22000  --verkningsgrad 0.92
"""
import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from prices import buy_price, sell_price, is_peak, load_spot_lookup, lookup_spot  # noqa: E402

DT = 5 / 60


def current_buy(spot, ts):
    """Nuvarande avtal (tidstariff från 2026-11-01) tillämpat på alla datum."""
    nat = 0.612 if is_peak(ts) else 0.244
    return 1.25 * (spot + 0.36 + nat + 0.04)


def load(db):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT ts_local, grid_kw FROM energy_5min_v2 WHERE grid_kw IS NOT NULL ORDER BY ts_local"
    ).fetchall()
    con.close()
    spot = load_spot_lookup(ROOT / "data" / "spot_cache")
    days = defaultdict(list)
    skipped = 0
    for ts_s, g in rows:
        ts = datetime.fromisoformat(ts_s[:19].replace("T", " "))
        p = lookup_spot(spot, ts)
        if p is None:
            skipped += 1
            continue
        days[ts.date()].append((ts, g, p))
    return dict(sorted(days.items())), skipped, len(rows)


def slot_cost(g, buy, sell):
    return (buy * g if g > 0 else sell * g) * DT


def optimise(slots, start, n_states, step, k, eta_c, eta_d, n_commit):
    """Bakåtriktad DP över slots. Returnerar (kostnad för de n_commit första, slut-SOC efter dem)."""
    T = len(slots)
    INF = float("inf")
    nxt = [0.0] * n_states  # inget värde på kvarvarande energi efter fönstret (försiktigt)
    choice = [None] * T
    for t in range(T - 1, -1, -1):
        g, buy, sell = slots[t]
        cur = [INF] * n_states
        ch = [0] * n_states
        for s in range(n_states):
            best, bd = INF, 0
            for d in range(-k, k + 1):
                s2 = s + d
                if s2 < 0 or s2 >= n_states:
                    continue
                if d > 0:
                    p = d * step / eta_c / DT              # laddeffekt mot nätet
                elif d < 0:
                    p = d * step * eta_d / DT              # negativ = levererar till huset
                    if -p > max(g, 0) + 1e-9:              # ladda inte ur mer än som köps
                        continue
                else:
                    p = 0.0
                c = slot_cost(g + p, buy, sell) + nxt[s2]
                if c < best:
                    best, bd = c, d
            cur[s], ch[s] = best, bd
        choice[t] = ch
        nxt = cur
    s, cost = start, 0.0
    for t in range(n_commit):
        g, buy, sell = slots[t]
        d = choice[t][s]
        p = d * step / eta_c / DT if d > 0 else d * step * eta_d / DT
        cost += slot_cost(g + p, buy, sell)
        s += d
    return cost, s


def run(days, price_fn, kwh, kw, eff):
    eta = eff ** 0.5
    step = 0.2
    n_states = int(round(kwh / step)) + 1
    k = max(1, int(kw * DT / step))
    keys = list(days)
    s = 0
    per_month = defaultdict(lambda: [0.0, 0.0])
    for i, day in enumerate(keys):
        win = days[day] + (days[keys[i + 1]] if i + 1 < len(keys) else [])
        slots = [(g, price_fn(p, ts), sell_price(p, ts.date())) for ts, g, p in win]
        n = len(days[day])
        base = sum(slot_cost(*x) for x in slots[:n])
        cost, s = optimise(slots, s, n_states, step, k, eta, eta, n)
        m = day.strftime("%Y-%m")
        per_month[m][0] += base
        per_month[m][1] += base - cost
        print(f"\r  {day}", end="", file=sys.stderr)
    print("\r" + " " * 20 + "\r", end="", file=sys.stderr)
    return dict(per_month)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kwh", type=float, default=10)
    ap.add_argument("--kw", type=float, default=5)
    ap.add_argument("--pris", type=float, default=22000)
    ap.add_argument("--verkningsgrad", type=float, default=0.92, help="tur och retur")
    ap.add_argument("--db", default=str(ROOT / "data" / "sigen.db"))
    a = ap.parse_args()

    days, skipped, total = load(a.db)
    print(f"Data: {len(days)} dygn, {total - skipped} femminutersvärden ({skipped} saknade spotpris)")
    print(f"Extra batteri: {a.kwh:g} kWh, {a.kw:g} kW, verkningsgrad {a.verkningsgrad:.0%}\n")

    res = {
        "Avtal som gällde då": run(days, buy_price, a.kwh, a.kw, a.verkningsgrad),
        "Nuvarande tidstariff": run(days, current_buy, a.kwh, a.kw, a.verkningsgrad),
    }
    months = sorted(next(iter(res.values())))
    print(f"{'Månad':<9}" + "".join(f"{n:>24}" for n in res))
    for m in months:
        print(f"{m:<9}" + "".join(f"{r[m][1]:>21.0f} kr" for r in res.values()))

    print()
    full = [m for m in months if sum(1 for d in days if d.strftime('%Y-%m') == m) >= 27]
    last12 = full[-12:]
    for name, r in res.items():
        year = sum(r[m][1] for m in last12)
        if len(last12) < 12:
            year *= 12 / max(1, len(last12))
        pb = a.pris / year if year > 0 else float("inf")
        # 2 % kapacitetsförlust per år
        acc, yrs = 0.0, 0
        while acc < a.pris and yrs < 40:
            acc += year * (0.98 ** yrs)
            yrs += 1
        print(f"{name}: {year:,.0f} kr/år (senaste {len(last12)} hela månader)".replace(",", " "))
        print(f"   återbetalning {pb:.1f} år utan åldring, ca {yrs} år med 2 % kapacitetsförlust/år\n")
    print("Obs: perfekt framförhållning. Verklig styrning tjänar mindre än detta.")


if __name__ == "__main__":
    main()
