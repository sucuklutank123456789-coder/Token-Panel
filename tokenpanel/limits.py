"""Claude subscription limits (5-hour session, weekly), from local files only.

Two sources, best first:
1. Claude Code caches the official usage percentages in ~/.claude.json ("cachedUsageUtilization") when it
   fetches them (for example for /usage). While that snapshot's window hasn't reset, it is shown as is.
2. Otherwise an estimate. Claude Code writes a message into the transcript when a limit is reached; the usage
   up to that moment measures the limit, and the current window is shown as a share of it. Usage is measured
   in API-price dollars rather than tokens, because the limits weigh models differently (Opus uses them up
   faster than Sonnet) and price is the closest local measure of that.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .model import Event

SESSION_SECONDS = 5 * 3600
WEEK_SECONDS = 7 * 86400
CALIBRATION_DAYS = 30  # older limit hits may be from another plan

# Names Claude Code uses (rateLimitType and the labels in "You've hit your ... limit").
KINDS = {
    "five_hour": "session",
    "seven_day": "weekly",
    "seven_day_opus": "weekly_opus",
    "seven_day_sonnet": "weekly_sonnet",
    "seven_day_overage_included": "weekly_fable",
}
KIND_LABELS = {
    "session": "5-hour session",
    "weekly": "Weekly (all models)",
    "weekly_opus": "Weekly Opus",
    "weekly_sonnet": "Weekly Sonnet",
    "weekly_fable": "Weekly Fable",
}
_TEXT_KINDS = {
    "session limit": "session",
    "5-hour limit": "session",
    "weekly limit": "weekly",
    "opus limit": "weekly_opus",
    "opus weekly limit": "weekly_opus",
    "sonnet limit": "weekly_sonnet",
    "sonnet weekly limit": "weekly_sonnet",
    "fable limit": "weekly_fable",
}


@dataclass
class LimitHit:
    ts: float
    kind: str  # one of KIND_LABELS
    resets_at: float = 0.0  # 0 when unknown


@dataclass
class Window:
    kind: str
    start: float
    end: float
    used_pct: float | None = None  # official, or estimated from a calibration
    cost: float = 0.0  # API-price dollars used in the window (estimates only)
    cap: float | None = None  # estimated dollars at which the limit is reached
    cap_from: float = 0.0  # when the limit hit used for the cap happened
    exact_end: bool = False  # end is a known reset time, not a guess
    official: bool = False  # used_pct is Claude Code's own figure


@dataclass
class ClaudeLimits:
    as_of: float  # when Claude Code fetched its figures (official windows), else when estimated
    windows: list[Window] = field(default_factory=list)
    blocked_until: float = 0.0  # a limit was reached and resets then (0 if not)
    blocked_kind: str = ""

    @property
    def official(self) -> bool:
        """Every window shown is Claude Code's own figure."""
        return bool(self.windows) and all(w.official for w in self.windows)

    @property
    def partly_official(self) -> bool:
        return any(w.official for w in self.windows)


# --- Limit messages in transcripts -------------------------------------------------------------------------

_RESET = re.compile(
    r"resets\s+(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\b(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _zone(name: str | None):
    if not name:
        return None  # local time
    if name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # unknown zone, or no time zone database (Windows without tzdata)
        return False


def parse_reset(text: str, msg_ts: float) -> float:
    """'resets 5am', 'resets 4:10pm (UTC)', 'resets Jun 3 at 4pm (Europe/Berlin)' -> epoch seconds, 0 if unknown."""
    m = _RESET.search(text)
    if not m:
        return 0.0
    tz = _zone(m["tz"])
    if tz is False:
        return 0.0
    hour = int(m["h"]) % 12 + (12 if m["ap"].lower() == "pm" else 0)
    minute = int(m["m"] or 0)
    sent = datetime.fromtimestamp(msg_ts, tz) if tz else datetime.fromtimestamp(msg_ts)
    if m["mon"]:
        if m["mon"].lower() not in _MONTHS:
            return 0.0
        month = _MONTHS.index(m["mon"].lower()) + 1
        try:
            day = date(sent.year, month, int(m["day"]))
        except ValueError:
            return 0.0
        if day < sent.date() - timedelta(days=1):
            day = day.replace(year=sent.year + 1)
        reset = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute, tzinfo=sent.tzinfo)
    else:
        reset = sent.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= sent:
            reset += timedelta(days=1)
    return reset.timestamp()


def parse_limit_message(d: dict, ts: float) -> LimitHit | None:
    """The synthetic transcript line Claude Code writes when a usage limit is reached, in any of its formats."""
    quota = d.get("quotaLimits")
    if isinstance(quota, dict) and quota.get("status") == "rejected":
        kind = KINDS.get(quota.get("rateLimitType") or "", "session")
        resets = quota.get("resetsAt")
        return LimitHit(ts, kind, float(resets) if isinstance(resets, (int, float)) else 0.0)
    content = (d.get("message") or {}).get("content")
    texts = [c.get("text") or "" for c in content if isinstance(c, dict)] if isinstance(content, list) else []
    text = " ".join(texts) if texts else (content if isinstance(content, str) else "")
    low = text.lower()
    if "not your usage limit" in low or low.startswith("api error"):
        return None  # server-side throttling, not the plan's limit
    # Up to mid-2025: "Claude AI usage limit reached|1749924000" (only 5-hour limits existed then).
    m = re.search(r"usage limit reached\|(\d+)", text, re.IGNORECASE)
    if m:
        return LimitHit(ts, "session", float(m.group(1)))
    # 2025: "5-hour limit reached ∙ resets 5am", "Opus weekly limit reached ∙ resets Oct 9, 5pm".
    # 2026: "You've hit your weekly limit · resets Jun 3 at 4pm (Europe/Berlin)".
    m = re.search(r"hit your\s+([\w -]*?)\s*limit\b", low) or re.search(r"([\w-]+(?: weekly)?) limit reached", low)
    if not m:
        return None
    resets = parse_reset(text, ts)
    name = " ".join(m.group(1).split())
    if not name:
        # "You've hit your limit": which one isn't named; a reset within 5 hours means the session limit.
        kind = "weekly" if resets and resets - ts > SESSION_SECONDS + 60 else "session"
    else:
        kind = _TEXT_KINDS.get(name + " limit")
        if kind is None:
            return None
    return LimitHit(ts, kind, resets)


# --- Official snapshot (~/.claude.json) ---------------------------------------------------------------------


def _epoch(value) -> float:
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e11 else 1)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def read_official(path: str) -> tuple[float, dict[str, tuple[float, float]]] | None:
    """(fetched at, {kind: (used percent, resets at)}) from Claude Code's cached usage, or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    cached = data.get("cachedUsageUtilization") if isinstance(data, dict) else None
    if not isinstance(cached, dict):
        return None
    util = cached.get("utilization")
    if not isinstance(util, dict):
        return None
    out = {}
    for key, kind in KINDS.items():
        w = util.get(key)
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            out[kind] = (float(w["utilization"]), _epoch(w.get("resets_at")))
    if not out:
        return None
    return _epoch(cached.get("fetchedAtMs")), out


# --- Estimate ----------------------------------------------------------------------------------------------


def _cost(events: list[Event], start: float, end: float) -> float:
    return sum(e.usage.cost for e in events if start <= e.ts < end)


def _session_start(events: list[Event], now: float) -> float | None:
    """Start of the 5-hour window in progress: the first message after the previous window ended."""
    start = None
    for e in events:
        if start is None or e.ts >= start + SESSION_SECONDS:
            start = e.ts
    return start if start is not None and start + SESSION_SECONDS > now else None


def estimate(
    events: list[Event],
    hits: list[LimitHit],
    official: tuple[float, dict[str, tuple[float, float]]] | None = None,
    now: float | None = None,
) -> ClaudeLimits | None:
    """events: Claude Code events (any order). None when there is nothing to show."""
    now = time.time() if now is None else now
    events = sorted((e for e in events if e.ts), key=lambda e: e.ts)
    hits = sorted(hits, key=lambda h: h.ts)
    blocked = next((h for h in reversed(hits) if h.resets_at > now), None)
    blocked_until, blocked_kind = (blocked.resets_at, blocked.kind) if blocked else (0.0, "")

    fetched, kinds = official if official is not None else (0.0, {})
    # Official figures whose window hasn't reset yet; other windows fall back to the estimate.
    live = {k: v for k, v in kinds.items() if v[1] > now}
    if not events and not hits and not live:
        return None
    recent = [h for h in hits if h.ts > now - CALIBRATION_DAYS * 86400]
    windows = []

    # 5-hour window
    start = _session_start(events, now)
    w = Window("session", start or now, (start or now) + SESSION_SECONDS)
    last_session = next((h for h in reversed(recent) if h.kind == "session"), None)
    if last_session is not None:
        if last_session.resets_at:
            hit_start = last_session.resets_at - SESSION_SECONDS
        else:
            hit_start = _session_start([e for e in events if e.ts <= last_session.ts], last_session.ts) or 0
        cap = _cost(events, hit_start, last_session.ts + 1)
        if cap > 0:
            w.cap, w.cap_from = cap, last_session.ts
        if last_session.resets_at > now:  # still in the window that hit the limit
            w.start, w.end, w.exact_end = hit_start, last_session.resets_at, True
    if start is not None or w.exact_end:
        w.cost = _cost(events, w.start, now + 1)
        w.used_pct = w.cost * 100 / w.cap if w.cap else None
        windows.append(w)

    # Weekly: the account's weekly reset time repeats every 7 days; known from any weekly limit message.
    weekly = [h for h in hits if h.kind == "weekly" and h.resets_at]
    if weekly:
        end = weekly[-1].resets_at
        while end <= now:
            end += WEEK_SECONDS
        w = Window("weekly", end - WEEK_SECONDS, end, exact_end=True)
    else:
        w = Window("weekly", now - WEEK_SECONDS, now)  # unknown reset day: the last 7 days
    last_week = next((h for h in reversed(recent) if h.kind == "weekly"), None)
    if last_week is not None:
        cap_start = last_week.resets_at - WEEK_SECONDS if last_week.resets_at else last_week.ts - WEEK_SECONDS
        cap = _cost(events, cap_start, last_week.ts + 1)
        if cap > 0:
            w.cap, w.cap_from = cap, last_week.ts
    w.cost = _cost(events, w.start, now + 1)
    w.used_pct = w.cost * 100 / w.cap if w.cap else None
    windows.append(w)

    if live:
        windows = [w for w in windows if w.kind not in live]
        for kind, (pct, resets) in live.items():
            length = SESSION_SECONDS if kind == "session" else WEEK_SECONDS
            windows.append(Window(kind, resets - length, resets, used_pct=pct, exact_end=True, official=True))
        windows.sort(key=lambda w: list(KIND_LABELS).index(w.kind))
    return ClaudeLimits(fetched if live else now, windows, blocked_until, blocked_kind)
