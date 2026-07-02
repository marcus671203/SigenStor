#!/usr/bin/env python3
"""
Validation: jämför energy_5min (mail-fetch) vs energy_5min_v2 (API-aggregat)
för överlappande tidsfönster.

Print:
   - Antal överlappande fönster
   - Per-kolumn: medel-diff, max-diff, korrelation
   - Lista över största outliers

Kör manuellt eller via cron nightly för continuous validation.
"""

import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "sigen.db"

COLUMNS = ['last_kw', 'bat_kw', 'evdc_kw', 'grid_kw', 'pv3_kw']


def main():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        
        # Hitta överlappande tidsstämplar
        rows = conn.execute("""
            SELECT 
                a.ts_local,
                a.last_kw AS mail_last, b.last_kw AS api_last,
                a.bat_kw  AS mail_bat,  b.bat_kw  AS api_bat,
                a.evdc_kw AS mail_evdc, b.evdc_kw AS api_evdc,
                a.grid_kw AS mail_grid, b.grid_kw AS api_grid,
                a.pv3_kw  AS mail_pv3,  b.pv3_kw  AS api_pv3,
                b.sample_count AS n
            FROM energy_5min a
            INNER JOIN energy_5min_v2 b ON a.ts_local = b.ts_local
            ORDER BY a.ts_local
        """).fetchall()

        if not rows:
            print("⚠️  Inga överlappande tidsfönster — mail har inte kommit ifatt API än.")
            print(f"   Mail-data senaste:")
            r = conn.execute("SELECT MAX(ts_local) FROM energy_5min").fetchone()
            print(f"     {r[0]}")
            print(f"   API-data första:")
            r = conn.execute("SELECT MIN(ts_local) FROM energy_5min_v2").fetchone()
            print(f"     {r[0]}")
            print("\n   Vänta tills mail-fetchen täcker samma tid som API-data,")
            print("   sedan kör scriptet igen.")
            return

        print(f"=== Jämförelse: {len(rows)} överlappande 5-min-fönster ===\n")
        print(f"Period: {rows[0]['ts_local']} → {rows[-1]['ts_local']}\n")

        for col in COLUMNS:
            mail_vals = [r[f'mail_{col.replace("_kw","")}'] or 0 for r in rows]
            api_vals  = [r[f'api_{col.replace("_kw","")}']  or 0 for r in rows]
            diffs     = [a - m for a, m in zip(api_vals, mail_vals)]
            
            mean_mail = statistics.mean(mail_vals)
            mean_api  = statistics.mean(api_vals)
            mean_diff = statistics.mean(diffs)
            max_abs_diff = max(abs(d) for d in diffs)
            
            # Pearson-korrelation (simpel)
            try:
                corr = statistics.correlation(mail_vals, api_vals)
            except statistics.StatisticsError:
                corr = float('nan')
            
            print(f"  {col}:")
            print(f"     mail medel:     {mean_mail:8.3f}")
            print(f"     API medel:      {mean_api:8.3f}")
            print(f"     diff medel:     {mean_diff:+8.3f}  (api - mail)")
            print(f"     max |diff|:     {max_abs_diff:8.3f}")
            print(f"     korrelation:    {corr:8.3f}  (1.0 = perfekt)")
            print()

        # Top 5 största outliers
        print("=== Topp-5 största total-diff ===")
        scored = []
        for r in rows:
            total_diff = sum(abs((r[f'api_{c.replace("_kw","")}'] or 0) - 
                                 (r[f'mail_{c.replace("_kw","")}'] or 0)) for c in COLUMNS)
            scored.append((r['ts_local'], total_diff, r['n']))
        scored.sort(key=lambda x: -x[1])
        for ts, d, n in scored[:5]:
            print(f"   {ts}:  Σ|diff|={d:.3f}  (n={n} polls)")


if __name__ == "__main__":
    main()
