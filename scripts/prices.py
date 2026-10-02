"""Enda sanning för elpriser och tariffer (SE3). SEK/kWh, moms inkluderad där den gäller.

Avtalsperioder:
  < 2026-01-01  (CONTRACT_2026)
  < 2026-04-02  (CONTRACT_CHANGE)
  >= 2026-04-02 (nuvarande)
"""
from datetime import date

CONTRACT_2026 = date(2026, 1, 1)
CONTRACT_CHANGE = date(2026, 4, 2)


def buy_price(spot, d):
    """Totalt köppris per kWh inkl. elhandel, nätöverföring, energiskatt, moms."""
    if d < CONTRACT_2026:
        return 1.25 * spot + 0.9532
    if d < CONTRACT_CHANGE:
        return 1.25 * spot + 0.935
    return 1.25 * (spot + 0.604) + 0.04


def sell_price(spot, d):
    """Säljpris per kWh (vad elhandeln betalar)."""
    if d < CONTRACT_2026:
        return spot + 0.72
    return spot + 0.104


def grid_transfer_rate(d):
    """Nätöverföring kr/kWh inkl moms."""
    if d < CONTRACT_CHANGE:
        return 0.445
    return 0.244 * 1.25            # 30,5 öre


def energy_tax(d):
    """Energiskatt kr/kWh inkl moms."""
    return 0.45


def elhandel_markup(d):
    """Fast elhandelspåslag kr/kWh."""
    return 0.04


import json as _json
from pathlib import Path as _Path
from datetime import datetime as _dt


def load_spot_lookup(spot_cache_dir, pattern="*_SE3.json"):
    """Bygg {(datum_iso, timme, kvart): SEK_per_kWh} från spot_cache.
    Fyller saknade kvarter med timmens q0-pris (historisk timupplöst data)."""
    lookup = {}
    for f in _Path(spot_cache_dir).glob(pattern):
        try:
            for entry in _json.loads(_Path(f).read_text()):
                d = _dt.fromisoformat(entry["time_start"].replace("Z", "+00:00"))
                lookup[(d.date().isoformat(), d.hour, d.minute // 15)] = entry["SEK_per_kWh"]
        except Exception:
            pass
    for (dd, hh, q) in list(lookup.keys()):
        if q == 0:
            base = lookup[(dd, hh, 0)]
            for qq in (1, 2, 3):
                lookup.setdefault((dd, hh, qq), base)
    return lookup


def lookup_spot(lookup, ts):
    """ts: datetime (lokal). Returnerar kvartspris, fallback till timmens q0, annars None."""
    diso = ts.date().isoformat()
    v = lookup.get((diso, ts.hour, ts.minute // 15))
    if v is None:
        v = lookup.get((diso, ts.hour, 0))
    return v
