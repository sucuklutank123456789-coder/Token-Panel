"""Compares the panel's Codex numbers with Codex's own records (prints numbers only, no log contents).

Usage: python3 scripts/compare_codex.py

Codex keeps a running total per thread in two places: the last token_count event of each session file
(total_token_usage.total_tokens) and the tokens_used column of its state database (state_*.sqlite).
"""

import collections
import datetime
import glob
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tokenpanel import compressed  # noqa: E402
from tokenpanel.paths import default_codex_dirs  # noqa: E402
from tokenpanel.store import Store, range_start  # noqa: E402


def g(n):
    return f"{n / 1e6:,.1f}M"


RANGES = ("7d", "30d", "all")
starts = {r: range_start(r) for r in RANGES}
print("Python:", sys.version.split()[0], "| zstd decoder:", "yes" if compressed.available() else "NO")

for base in default_codex_dirs():
    print(f"\n== {base}")
    if not os.path.isdir(base):
        print("  (missing)")
        continue
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name)
        if os.path.isdir(p):
            n = sum(len(fs) for _, _, fs in os.walk(p))
            print(f"  {name}/  {n} files")
        else:
            print(f"  {name}  {os.path.getsize(p) / 1e6:.1f} MB")

    kinds = collections.Counter()
    by_range = {r: collections.Counter() for r in RANGES}
    paths = set()
    for sub in ("sessions", "archived_sessions"):
        for p in glob.glob(os.path.join(base, sub, "**", "*"), recursive=True):
            if os.path.isdir(p):
                continue
            ext = ".jsonl.zst" if p.endswith(".jsonl.zst") else os.path.splitext(p)[1] or "(none)"
            kinds[f"{sub}/*{ext}"] += 1
            if ext not in (".jsonl", ".jsonl.zst"):
                continue
            paths.add(os.path.realpath(p))
            if ext == ".jsonl":
                with open(p, "rb") as fh:
                    data = fh.read()
            else:
                data = compressed.read_zst(p)
                if data is None:
                    kinds["unreadable .zst"] += 1
                    continue
            last, last_ts = None, ""
            for line in data.splitlines():
                if b'"token_count"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                info = (d.get("payload") or {}).get("info")
                if isinstance(info, dict) and isinstance(info.get("total_token_usage"), dict):
                    last, last_ts = info["total_token_usage"], d.get("timestamp") or ""
            if not last:
                continue
            try:
                ts = datetime.datetime.fromisoformat(last_ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = 0
            for r in RANGES:
                if ts >= starts[r]:
                    by_range[r]["files"] += 1
                    by_range[r]["total"] += int(last.get("total_tokens") or 0)
    for k, n in sorted(kinds.items()):
        print(f"  {k}: {n}")
    for r in RANGES:
        c = by_range[r]
        print(
            f"  {r:>3}: Codex counters in session files (last activity in range): {g(c['total'])} in {c['files']} files"
        )

    for db in sorted(glob.glob(os.path.join(base, "state_*.sqlite"))):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            cols = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
            rows = con.execute("SELECT rollout_path, updated_at, tokens_used FROM threads").fetchall()
            con.close()
        except sqlite3.Error as e:
            print(f"  {os.path.basename(db)}: could not be read ({e})")
            continue
        print(f"  {os.path.basename(db)}: {len(rows)} threads, columns: {'tokens_used' in cols}")
        missing = collections.Counter()
        for r in RANGES:
            total = n = 0
            for path, updated, used in rows:
                # updated_at is in seconds, or milliseconds in newer versions.
                t = updated / 1000 if updated and updated > 1e11 else (updated or 0)
                if t < starts[r]:
                    continue
                total += used or 0
                n += 1
                exists = path and (os.path.exists(path) or os.path.exists(path + ".zst"))
                if r == "all":
                    missing["file found" if exists else "file MISSING"] += 1
                    if exists and os.path.realpath(path) not in paths and os.path.realpath(path + ".zst") not in paths:
                        missing["file outside sessions/"] += 1
            print(f"  {r:>3}: tokens_used in the state database: {g(total)} in {n} threads")
        print("  thread files:", dict(missing))

print("\n== The panel")
store = Store()
store.refresh()
for r in RANGES:
    s = store.summarize(r, "app")
    u = s.by_source.get("codex")
    print(
        f"  {r:>3}: codex {g(u.value('app'))} (same as the official apps), raw {g(u.total)}; "
        f"unreadable compressed files: {s.unreadable}"
    )
