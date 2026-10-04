"""Computes Claude Code token counts under different definitions (prints numbers only).

Usage: python3 scripts/diagnose.py
Helps explain differences between the panel and the numbers the Claude app shows.
"""

import collections
import datetime
import glob
import json
import os
import sys
import time

base = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
files = glob.glob(base + "/projects/**/*.jsonl", recursive=True)
print(f"Found {len(files)} log files ({base}/projects), reading…", flush=True)

seen = set()
io = collections.Counter()
io_by_model = collections.Counter()
new = collections.Counter()
nodedup = 0
cost = {}
dates = []
start = time.time()

for i, f in enumerate(files, 1):
    dates.append(os.path.getmtime(f))
    with open(f, "rb") as fh:
        for line in fh:
            if b'"cost-state"' in line:
                try:
                    d = json.loads(line)
                    cost[d.get("sessionId") or f] = d.get("modelUsage") or {}
                except ValueError:
                    pass
                continue
            if b'"assistant"' not in line or b'"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            u = msg.get("usage") or {}
            v = u.get("input_tokens", 0) + u.get("output_tokens", 0)
            nodedup += v
            k = (msg.get("id"), d.get("requestId"))
            if k in seen:
                continue
            seen.add(k)
            ep = d.get("entrypoint") or "?"
            io[ep] += v
            io_by_model[msg.get("model") or "?"] += v
            new[ep] += v + u.get("cache_creation_input_tokens", 0)
    if i % 200 == 0:
        print(f"  {i}/{len(files)} files…", flush=True)


def m(n):
    return f"{n / 1e6:.2f}M"


bg = sum(
    x.get("inputTokens", 0) + x.get("outputTokens", 0)
    for mu in cost.values()
    for mdl, x in mu.items()
    if "haiku" in mdl
)
print(f"\nTook {time.time() - start:.1f} s")
if dates:
    print("Oldest log:", datetime.date.fromtimestamp(min(dates)))
print("\nInput + output, deduplicated (by client):")
for k, v in io.most_common():
    print(f"  {k:20} {m(v)}")
print(f"  {'TOTAL':20} {m(sum(io.values()))}")
print("\nInput + output, deduplicated (by model):")
for k, v in io_by_model.most_common():
    print(f"  {k:28} {m(v)}")
print("\nIncluding cache writes:", m(sum(new.values())))
print("Input + output without deduplication:", m(nodedup))
print("Background Haiku (cost-state):", m(bg))

# --- The method used by Claude Code's stats screen --------------------------------
# projects/<p>/*.jsonl + projects/<p>/<session>/subagents/agent-*.jsonl;
# sidechain lines in main files are skipped, lines are not deduplicated.
stat_files = []
for proj in glob.glob(base + "/projects/*/"):
    stat_files += glob.glob(proj + "*.jsonl")
    stat_files += glob.glob(proj + "*/subagents/agent-*.jsonl")
sessions = 0
types = collections.Counter()
st = collections.Counter()
for f in stat_files:
    sub = "/subagents/" in f
    had = False
    with open(f, "rb") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or "timestamp" not in d:
                continue
            if not sub and d.get("isSidechain"):
                continue
            had = True
            if not sub:
                types[d.get("type")] += 1
            if d.get("type") != "assistant":
                continue
            msg = d.get("message") or {}
            u = msg.get("usage")
            if not u or msg.get("model") == "<synthetic>":
                continue
            st["input"] += u.get("input_tokens") or 0
            st["output"] += u.get("output_tokens") or 0
            st["cache_read"] += u.get("cache_read_input_tokens") or 0
            st["cache_write"] += u.get("cache_creation_input_tokens") or 0
    if had and not sub:
        sessions += 1
print("\nClaude stats method (no deduplication):")
print("  sessions:", sessions, "| line types:", dict(types.most_common(6)))
print("  user+assistant lines (messages):", types["user"] + types["assistant"])
print("  input:", m(st["input"]), " output:", m(st["output"]), " cache reads:", m(st["cache_read"]),
      " cache writes:", m(st["cache_write"]))
print("  input+output:", m(st["input"] + st["output"]),
      " | +cache writes:", m(st["input"] + st["output"] + st["cache_write"]))

p = base + "/stats-cache.json"
if os.path.exists(p):
    try:
        s = json.load(open(p))
        print("\nstats-cache.json keys:", list(s))
        for mdl, x in (s.get("modelUsage") or {}).items():
            print("  ", mdl, {k: v for k, v in x.items() if isinstance(v, (int, float))})
    except ValueError:
        print("\nstats-cache.json could not be read")
else:
    print("\nno stats-cache.json")

sp = base + "/settings.json"
try:
    print("cleanupPeriodDays:", json.load(open(sp)).get("cleanupPeriodDays", "not set (default 30 days)"))
except (OSError, ValueError):
    print("cleanupPeriodDays: no settings.json (default 30 days)")
sys.exit(0)
