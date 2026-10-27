"""Enda sanning för elpriser och tariffer (SE3). SEK/kWh, moms där den gäller.

Avtalsperioder:
  < 2026-01-01   (CONTRACT_2026)
  < 2026-04-02   (CONTRACT_CHANGE)
  < 2026-11-01   (CONTRACT_TOU)  — platt nät, 4-öring utanför moms, fast 39
  >= 2026-11-01  GodEl tidstariff: allt inom moms, fast 49, nätöverföring
                 som höglasttid (helgfri vardag 06–22, vinterhalvår nov–mar).
"""
from datetime import date, timedelta

CONTRACT_2026 = date(2026, 1, 1)
CONTRACT_CHANGE = date(2026, 4, 2)
CONTRACT_TOU = date(2026, 11, 1)

WINTER_MONTHS = {11, 12, 1, 2, 3}


def _easter(year):
    a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30; i = c // 4; k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7; m = (a + 11 * h + 22 * l) // 451
    mo = (h + l - 7 * m + 114) // 31; da = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, mo, da)


_HOLIDAY_CACHE = {}


def _holidays(year):
    """Röda dagar + aftnar som räknas som off-peak."""
    if year not in _HOLIDAY_CACHE:
        e = _easter(year)
        _HOLIDAY_CACHE[year] = {
            date(year, 1, 1), date(year, 1, 6),
            e - timedelta(days=2), e + timedelta(days=1),   # långfredag, annandag påsk
            date(year, 5, 1), e + timedelta(days=39),       # första maj, Kristi himmelsfärd
            date(year, 6, 6),                               # nationaldagen
            date(year, 12, 25), date(year, 12, 26),         # jul
            date(year, 12, 24), date(year, 12, 31),         # aftnar (off-peak)
        }
    return _HOLIDAY_CACHE[year]


def is_peak(ts):
    """Höglasttid: helgfri vardag 06:00–21:59 under vinterhalvåret (nov–mar)."""
    d = ts.date()
    if d.month not in WINTER_MONTHS:
        return False
    if ts.weekday() >= 5:
        return False
    if d in _holidays(d.year):
        return False
    return 6 <= ts.hour < 22


def _as_date(x):
    return x.date() if hasattr(x, "hour") else x


def _nat_pre_moms(ts):
    """Nätöverföring exkl moms (kr/kWh), tidsberoende efter CONTRACT_TOU."""
    d = _as_date(ts)
    if d < CONTRACT_CHANGE:
        return 0.445 / 1.25
    if d < CONTRACT_TOU:
        return 0.244
    if hasattr(ts, "hour") and is_peak(ts):
        return 0.612
    return 0.244


def grid_transfer_rate(ts):
    """Nätöverföring kr/kWh INKL moms (tidsberoende efter 2026-11-01)."""
    return _nat_pre_moms(ts) * 1.25


def buy_price(spot, ts):
    """Totalt köppris kr/kWh inkl allt. ts: datetime (tidstariff) eller date."""
    d = _as_date(ts)
    if d < CONTRACT_2026:
        return 1.25 * spot + 0.9532
    if d < CONTRACT_CHANGE:
        return 1.25 * spot + 0.935
    if d < CONTRACT_TOU:
        return 1.25 * (spot + 0.604) + 0.04
    return 1.25 * (spot + 0.36 + _nat_pre_moms(ts) + 0.04)


def sell_price(spot, d):
    d = _as_date(d)
    if d < CONTRACT_2026:
        return spot + 0.72
    return spot + 0.104


def energy_tax(d):
    """Energiskatt kr/kWh inkl moms (0,36 × 1,25)."""
    return 0.45


def elhandel_markup(d):
    """Elhandelspåslag kr/kWh inkl moms (0,04 momsas i nya avtalet → 0,05)."""
    return 0.05 if _as_date(d) >= CONTRACT_TOU else 0.04


def elhandel_fixed(d):
    """Fast elhandelsabonnemang kr/mån."""
    return 49.0 if _as_date(d) >= CONTRACT_TOU else 39.0


import json as _json
from pathlib import Path as _Path
from datetime import datetime as _dt


def load_spot_lookup(spot_cache_dir, pattern="*_SE3.json"):
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
    diso = ts.date().isoformat()
    v = lookup.get((diso, ts.hour, ts.minute // 15))
    if v is None:
        v = lookup.get((diso, ts.hour, 0))
    return v
