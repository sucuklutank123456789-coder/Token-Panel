"""Terminal output: `tokenpanel --dump`."""

from __future__ import annotations

from . import fmt
from .describe import blocked_text, cost_text, limits_source, window_text
from .model import METRICS, SOURCE_LABELS
from .store import RANGES, Summary


def render(s: Summary, max_threads: int = 5) -> str:
    def v(u):
        return u.value(s.metric)

    inp, outp = s.total.split(s.metric)
    out = [
        f"{RANGES[s.range_key]}: {fmt.short(v(s.total))} tokens ({fmt.full(v(s.total))}) — metric: {METRICS[s.metric]}",
        f"  input {fmt.short(inp)} · output {fmt.short(outp)} · {cost_text(s.total)}",
        "  " + " · ".join(f"{SOURCE_LABELS[k]} {fmt.short(v(u))}" for k, u in s.by_source.items()),
    ]
    est = s.claude_limits
    if est and est.windows:
        texts = [window_text(w) for w in est.windows]
        out.append("  Claude limits: " + ", ".join(f"{t.name} {t.value} ({t.when})" for t in texts))
        out.append(f"    {limits_source(est)}")
        if blocked_text(est):
            out.append(f"  Claude: {blocked_text(est)}")
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
            f"[{SOURCE_LABELS[c.source]} · {c.label}] {fmt.short(v(c.usage))}  ≈ {fmt.money(c.usage.cost)}  ({models})"
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
