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
