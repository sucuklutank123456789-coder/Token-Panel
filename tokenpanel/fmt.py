"""Sayı ve zaman biçimlendirme (Türkçe)."""

from __future__ import annotations

import time
from datetime import datetime


def short(n: float) -> str:
    """15317124 -> '15,3M'"""
    n = float(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            v = n / div
            s = f"{v:.0f}" if v >= 100 else f"{v:.1f}".removesuffix(".0")
            return s.replace(".", ",") + suffix
    return str(int(n))


def full(n: float) -> str:
    """15317124 -> '15.317.124'"""
    return f"{int(n):,}".replace(",", ".")


def percent(part: float, whole: float) -> str:
    if not whole:
        return "%0"
    return f"%{part * 100 / whole:.0f}"


def ago(ts: float, now: float | None = None) -> str:
    if not ts:
        return "—"
    d = (time.time() if now is None else now) - ts
    if d < 60:
        return "az önce"
    if d < 3600:
        return f"{int(d // 60)} dk önce"
    if d < 86400:
        return f"{int(d // 3600)} sa önce"
    if d < 7 * 86400:
        return f"{int(d // 86400)} gün önce"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def clock(ts: float) -> str:
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts)
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%d.%m %H:%M")
