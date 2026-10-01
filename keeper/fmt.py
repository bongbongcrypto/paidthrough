"""Money and time formatting. Integer math only; rounding direction is stated per function."""
from __future__ import annotations

from datetime import datetime, timezone

USDC_DECIMALS = 6          # ERC-20 view of USDC on Arc (bill amounts)
NATIVE_DECIMALS = 18       # native USDC balance / gas on Arc (wei)
GWEI = 10 ** 9


def usdc6(units: int) -> str:
    """Bill amount in USDC base units (6 decimals) -> exact decimal string, no rounding."""
    units = int(units)
    sign = "-" if units < 0 else ""
    q, r = divmod(abs(units), 10 ** USDC_DECIMALS)
    return "%s%d.%06d" % (sign, q, r)


def fee_usdc(wei: int) -> str:
    """Native gas cost in wei (18 decimals) -> USDC string with 6 decimals, rounded UP
    (a fee is never shown smaller than it was). Exact wei is printed next to it where it matters."""
    wei = int(wei)
    if wei < 0:
        raise ValueError("negative fee")
    step = 10 ** (NATIVE_DECIMALS - USDC_DECIMALS)   # 1e12 wei = 0.000001 USDC
    micro = -(-wei // step)                          # ceil division
    return usdc6(micro)


def gwei(wei: int) -> str:
    """wei -> gwei string, exact up to 9 decimals, trailing zeros trimmed."""
    q, r = divmod(int(wei), GWEI)
    return str(q) if r == 0 else ("%d.%09d" % (q, r)).rstrip("0")


def utc(ts: int) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def short(addr: str) -> str:
    if not addr or int(addr, 16) == 0:
        return "-"
    return addr[:6] + ".." + addr[-4:]
