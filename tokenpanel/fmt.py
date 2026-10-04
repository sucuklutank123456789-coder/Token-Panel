"""Number and time formatting."""

from __future__ import annotations

import time
from datetime import datetime


def short(n: float) -> str:
    """15317124 -> '15.3M'"""
    n = float(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            v = n / div
            return (f"{v:.0f}" if v >= 100 else f"{v:.1f}".removesuffix(".0")) + suffix
    return str(int(n))


def full(n: float) -> str:
    """15317124 -> '15,317,124'"""
    return f"{int(n):,}"


def percent(part: float, whole: float) -> str:
    if not whole:
        return "0%"
    return f"{part * 100 / whole:.0f}%"


def ago(ts: float, now: float | None = None) -> str:
    if not ts:
        return "—"
    d = (time.time() if now is None else now) - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    if d < 7 * 86400:
        days = int(d // 86400)
        return f"{days} day{'s' if days > 1 else ''} ago"
    return datetime.fromtimestamp(ts).strftime("%b %d, %Y")


def clock(ts: float) -> str:
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts)
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%b %d %H:%M")
