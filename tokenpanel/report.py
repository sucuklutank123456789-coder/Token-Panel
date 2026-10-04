"""Terminal çıktısı: `tokenpanel --dump`."""

from __future__ import annotations

from . import fmt
from .model import METRICS, SOURCE_LABELS
from .store import RANGES, Summary


def render(s: Summary, max_threads: int = 5) -> str:
    def v(u):
        return u.value(s.metric)

    out = [
        f"{RANGES[s.range_key]}: {fmt.short(v(s.total))} token ({fmt.full(v(s.total))}) — ölçü: {METRICS[s.metric]}",
        "  " + " · ".join(f"{SOURCE_LABELS[k]} {fmt.short(v(u))}" for k, u in s.by_source.items()),
    ]
    if s.limits and s.limits.primary:
        p, w = s.limits.primary, s.limits.secondary
        line = f"  Codex limit: 5 saatlik %{p.used_percent:.0f}"
        if w:
            line += f", haftalık %{w.used_percent:.0f}"
        out.append(line)
    for c in s.clients:
        models = ", ".join(f"{m} {fmt.short(v(u))}" for m, u in sorted(c.models.items(), key=lambda x: -v(x[1])))
        out.append("")
        out.append(f"[{SOURCE_LABELS[c.source]} · {c.label}] {fmt.short(v(c.usage))}  ({models})")
        for t in c.threads[:max_threads]:
            out.append(f"   {fmt.short(v(t.usage)):>7}  {t.title}  — {t.project}, {fmt.ago(t.last_ts)}")
            tools = sorted(t.tools.items(), key=lambda x: -v(x[1]))[:4]
            out.append("            " + ", ".join(f"{n} {fmt.short(v(u))}" for n, u in tools))
        if len(c.threads) > max_threads:
            out.append(f"   … {len(c.threads) - max_threads} thread daha")
    out.append("")
    out.append(f"{s.file_count} log dosyası okundu.")
    return "\n".join(out)
