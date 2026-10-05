"""Claude subscription limit estimate, from local logs only.

Claude Code doesn't log how much of the plan's limits is used (Codex does). What the logs do have:
- every model call with its time, so the current 5-hour session window and the last 7 days can be summed;
- the message Claude Code writes when a limit is reached. The usage up to that moment is a measurement of the
  limit, so later windows can be shown as a percentage of it.

Usage is measured in API-price dollars, not tokens: the limits weigh models differently (Opus uses them up
faster than Sonnet), and price is the closest local measure of that.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .model import Event

SESSION_SECONDS = 5 * 3600
WEEK_SECONDS = 7 * 86400


@dataclass
class LimitHit:
    ts: float
    kind: str  # "session" (5-hour), "weekly", "weekly_opus" ... or "unknown"
    resets_at: float = 0.0  # 0 when the message didn't say


@dataclass
class Window:
    start: float
    end: float
    cost: float = 0.0
    tokens: float = 0.0  # input + output


@dataclass
class ClaudeLimits:
    session: Window | None  # the 5-hour window in progress, if any
    week: Window  # the last 7 days
    session_cap: float | None  # estimated cost at which the 5-hour limit is reached
    week_cap: float | None
    session_cap_from: float  # time of the limit message the cap was measured from
    week_cap_from: float
    blocked_until: float  # a limit was reached and resets at this time (0 if not)
    blocked_kind: str

    @staticmethod
    def pct(used: float, cap: float | None) -> float | None:
        return used * 100 / cap if cap else None


def session_windows(events: list[Event]) -> list[Window]:
    """5-hour windows as Claude counts them: one starts with the first message after the previous one ended.

    The start is rounded down to the hour, as Claude's reset times are."""
    windows: list[Window] = []
    for ev in events:
        if not windows or ev.ts >= windows[-1].end:
            start = ev.ts - ev.ts % 3600
            windows.append(Window(start, start + SESSION_SECONDS))
        w = windows[-1]
        w.cost += ev.usage.cost
        w.tokens += ev.usage.input + ev.usage.output
    return windows


def _sum(events: list[Event], start: float, end: float) -> Window:
    w = Window(start, end)
    for ev in events:
        if start <= ev.ts <= end:
            w.cost += ev.usage.cost
            w.tokens += ev.usage.input + ev.usage.output
    return w


def estimate(events: list[Event], hits: list[LimitHit], now: float | None = None) -> ClaudeLimits | None:
    """events: Claude events, any order. Returns None when there is no Claude usage at all."""
    now = time.time() if now is None else now
    events = sorted((e for e in events if e.source == "claude" and e.ts), key=lambda e: e.ts)
    if not events:
        return None
    windows = session_windows(events)
    current = windows[-1] if windows[-1].end > now else None
    week = _sum(events, now - WEEK_SECONDS, now)

    session_cap = week_cap = None
    session_from = week_from = 0.0
    blocked_until, blocked_kind = 0.0, ""
    for hit in sorted(hits, key=lambda h: h.ts):
        if hit.kind == "session":
            w = next((w for w in windows if w.start <= hit.ts < w.end), None)
            if w is not None:
                used = _sum(events, w.start, hit.ts).cost
                if used > 0:
                    session_cap, session_from = used, hit.ts
        elif hit.kind.startswith("weekly") and hit.kind == "weekly":
            used = _sum(events, hit.ts - WEEK_SECONDS, hit.ts).cost
            if used > 0:
                week_cap, week_from = used, hit.ts
        if hit.resets_at > now:
            blocked_until, blocked_kind = hit.resets_at, hit.kind
    return ClaudeLimits(current, week, session_cap, week_cap, session_from, week_from, blocked_until, blocked_kind)
