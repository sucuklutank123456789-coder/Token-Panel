"""Wording shared by the panel and `--dump`, so both say the same thing."""

from __future__ import annotations

from dataclasses import dataclass

from . import fmt
from .limits import KIND_LABELS, ClaudeLimits, Window
from .model import Usage


def cost_text(u: Usage) -> str:
    """'API cost ≈ $12.40', with a note when some tokens have no known price."""
    text = f"API cost ≈ {fmt.money(u.cost)}"
    if u.unpriced:
        text += f" (+ {fmt.short(u.unpriced)} tokens of models without a known price)"
    return text


@dataclass
class WindowText:
    name: str  # "5-hour session"
    when: str  # "resets 15:00", "resets ~15:00", "last 7 days"
    value: str  # "37%", "≈64%", "$3.20 used"
    detail: str  # a sentence explaining where the value comes from


def window_text(w: Window) -> WindowText:
    if w.kind == "weekly" and not w.exact_end:
        when = "last 7 days"
    else:
        when = f"resets {'' if w.exact_end else '~'}{fmt.clock(w.end)}"
    if w.official:
        value, detail = f"{w.used_pct:.0f}%", "Claude Code's own figure for this window."
    elif w.used_pct is not None:
        value = f"≈{w.used_pct:.0f}%"
        detail = (
            f"{fmt.money(w.cost)} used at API prices. You reached this limit on {fmt.clock(w.cap_from)} after "
            f"{fmt.money(w.cap)}, so that amount is taken as the limit."
        )
    else:
        value = f"{fmt.money(w.cost)} used"
        detail = (
            "Used so far, at API prices. No limit has been reached in the last 30 days, so there is nothing to "
            "compare against yet; the percentage appears after the first one."
        )
    return WindowText(KIND_LABELS.get(w.kind, w.kind), when, value, detail)


def limits_source(est: ClaudeLimits) -> str:
    if est.official:
        return f"From Claude Code, {fmt.ago(est.as_of)}"
    if est.partly_official:
        return f"From Claude Code, {fmt.ago(est.as_of)}; figures marked ≈ are estimated from this computer's logs."
    return "Estimated from this computer's logs. /usage in Claude Code shows the exact figures."


def blocked_text(est: ClaudeLimits) -> str:
    """'' unless a limit is currently reached."""
    if not est.blocked_until:
        return ""
    return f"{KIND_LABELS.get(est.blocked_kind, 'Usage')} limit reached · resets {fmt.clock(est.blocked_until)}"
