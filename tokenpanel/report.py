"""Terminal output: `tokenpanel --dump`."""

from __future__ import annotations

from . import fmt
from .limits import KIND_LABELS
from .model import METRICS, SOURCE_LABELS
from .store import RANGES, Summary


def render(s: Summary, max_threads: int = 5) -> str:
    def v(u):
        return u.value(s.metric)

    def cost(u):
        text = f"≈ {fmt.money(u.cost)}"
        return text + (f" + {fmt.short(u.unpriced)} tokens without a known price" if u.unpriced else "")

    inp, outp = s.total.split(s.metric)
    out = [
        f"{RANGES[s.range_key]}: {fmt.short(v(s.total))} tokens ({fmt.full(v(s.total))}) — metric: {METRICS[s.metric]}",
        f"  input {fmt.short(inp)} · output {fmt.short(outp)} · API cost {cost(s.total)}",
        "  " + " · ".join(f"{SOURCE_LABELS[k]} {fmt.short(v(u))}" for k, u in s.by_source.items()),
    ]
    est = s.claude_limits
    if est and est.windows:
        parts = []
        for w in est.windows:
            if w.used_pct is not None:
                used = f"{'' if w.official else '≈'}{w.used_pct:.0f}%"
            else:
                used = f"{fmt.money(w.cost)} used"
            when = "last 7 days" if w.kind == "weekly" and not w.exact_end else f"resets {fmt.clock(w.end)}"
            parts.append(f"{KIND_LABELS.get(w.kind, w.kind)} {used} ({when})")
        if est.official:
            source = f"from Claude Code, {fmt.ago(est.as_of)}"
        elif est.partly_official:
            source = f"from Claude Code, {fmt.ago(est.as_of)}; ≈ estimated"
        else:
            source = "estimate"
        out.append(f"  Claude limits ({source}): " + ", ".join(parts))
        if est.blocked_until:
            kind = KIND_LABELS.get(est.blocked_kind, "")
            out.append(f"  Claude {kind} limit reached, resets {fmt.clock(est.blocked_until)}")
    if s.limits and s.limits.primary:
        p, w = s.limits.primary, s.limits.secondary
        line = f"  Codex limits: 5-hour {p.used_percent:.0f}%"
        if w:
            line += f", weekly {w.used_percent:.0f}%"
        out.append(line)
    for c in s.clients:
        models = ", ".join(f"{m} {fmt.short(v(u))}" for m, u in sorted(c.models.items(), key=lambda x: -v(x[1])))
        out.append("")
        out.append(
            f"[{SOURCE_LABELS[c.source]} · {c.label}] {fmt.short(v(c.usage))}  {cost(c.usage)}  ({models})"
        )
        for t in c.threads[:max_threads]:
            out.append(
                f"   {fmt.short(v(t.usage)):>7}  {fmt.money(t.usage.cost):>8}  {t.title}  — {t.project}, "
                f"{fmt.ago(t.last_ts)}"
            )
            tools = sorted(t.tools.items(), key=lambda x: -v(x[1]))[:4]
            out.append("                      " + ", ".join(f"{n} {fmt.short(v(u))}" for n, u in tools))
        if len(c.threads) > max_threads:
            out.append(f"   … {len(c.threads) - max_threads} more threads")
    out.append("")
    out.append(f"Read {s.file_count} log files.")
    return "\n".join(out)
