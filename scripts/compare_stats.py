"""Compares the panel's numbers with what Claude Code's /stats computes (prints numbers only, no log contents).

Usage: python3 scripts/compare_stats.py

/stats reads <config>/projects/<project>/*.jsonl and <project>/<session>/subagents/agent-*.jsonl, skips sidechain
lines in main files, counts every assistant line with usage (no deduplication) as
input + output + cache reads + cache writes, and picks days by their UTC date (7 days = today and the 6 before).
"""

import collections
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tokenpanel.paths import default_claude_dirs  # noqa: E402
from tokenpanel.store import Store  # noqa: E402


def g(n):
    return f"{n / 1e6:,.1f}M"


def utc_day(ts: str) -> str:
    try:
        when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return when.astimezone(datetime.timezone.utc).date().isoformat()
    except (ValueError, AttributeError):
        return ""


today = datetime.datetime.now(datetime.timezone.utc).date()
starts = {
    "7d": (today - datetime.timedelta(days=6)).isoformat(),
    "30d": (today - datetime.timedelta(days=29)).isoformat(),
    "all": "",
}

dirs = default_claude_dirs()
print("Claude directories the panel reads:", dirs)
env = os.environ.get("CLAUDE_CONFIG_DIR")
stats_dir = os.path.realpath(os.path.expanduser(env or "~/.claude"))
print("Directory /stats reads:", stats_dir)

for base in dirs:
    proj = os.path.join(base, "projects")
    panel_files = set(glob.glob(os.path.join(proj, "**", "*.jsonl"), recursive=True))
    stats_files = set()
    for p in glob.glob(os.path.join(proj, "*") + os.sep):
        stats_files |= set(glob.glob(os.path.join(p, "*.jsonl")))
        stats_files |= set(glob.glob(os.path.join(p, "*", "subagents", "agent-*.jsonl")))
    extra = panel_files - stats_files
    print(f"\n== {base}  ({'read by /stats' if os.path.realpath(base) == stats_dir else 'NOT read by /stats'})")
    print(f"  files: panel {len(panel_files)}, /stats {len(stats_files)}, only the panel {len(extra)}")
    kinds = collections.Counter()
    for f in extra:
        rel = os.path.relpath(f, proj).split(os.sep)
        kinds["/".join(["<p>"] + ["<s>" if len(x) > 30 else x for x in rel[1:-1]] + ["*.jsonl"])] += 1
    for k, n in kinds.most_common(8):
        print(f"    only the panel: {k}  x{n}")

    tot = {r: collections.Counter() for r in starts}
    for f in panel_files:
        sub = f"{os.sep}subagents{os.sep}" in f
        kind = "stats" if f in stats_files else "panel-only"
        with open(f, "rb") as fh:
            for line in fh:
                if b'"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("type") != "assistant":
                    continue
                if not sub and d.get("isSidechain"):
                    continue
                msg = d.get("message") or {}
                u = msg.get("usage")
                if not isinstance(u, dict) or msg.get("model") == "<synthetic>":
                    continue
                n = sum(
                    int(u.get(k) or 0)
                    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                )
                day = utc_day(d.get("timestamp") or "")
                for r, start in starts.items():
                    if day and day >= start:
                        tot[r][kind] += n
                        tot[r]["subagents" if sub else "main"] += n
    for r in starts:
        t = tot[r]
        print(
            f"  {r:>3}: /stats method {g(t['stats'])} | panel-only files {g(t['panel-only'])} "
            f"| main {g(t['main'])}, subagents {g(t['subagents'])}"
        )

print("\n== The panel (metric: same as the official apps)")
store = Store()
store.refresh()
for r in ("7d", "30d", "all"):
    s = store.summarize(r, "app")
    parts = ", ".join(f"{k} {g(u.value('app'))}" for k, u in s.by_source.items())
    print(f"  {r:>3}: total {g(s.total.value('app'))}  ({parts})")

p = os.path.join(stats_dir, "stats-cache.json")
try:
    with open(p, encoding="utf-8") as fh:
        sc = json.load(fh)
    total = sum(
        sum(
            int(x.get(k) or 0)
            for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
        )
        for x in (sc.get("modelUsage") or {}).values()
    )
    print(f"\nstats-cache.json: {g(total)} up to {sc.get('lastComputedDate')}")
except (OSError, ValueError):
    print("\nno readable stats-cache.json")
