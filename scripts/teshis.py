"""Claude Code token sayılarını farklı tanımlarla hesaplar (yalnızca sayı yazdırır).

Kullanım: python3 scripts/teshis.py
Panelin gösterdiği sayı ile Claude uygulamasının gösterdiği sayı arasındaki farkı bulmak için.
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
print(f"{len(files)} log dosyası bulundu ({base}/projects), okunuyor…", flush=True)

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
        print(f"  {i}/{len(files)} dosya…", flush=True)


def m(n):
    return f"{n / 1e6:.2f}M"


bg = sum(
    x.get("inputTokens", 0) + x.get("outputTokens", 0)
    for mu in cost.values()
    for mdl, x in mu.items()
    if "haiku" in mdl
)
print(f"\nSüre: {time.time() - start:.1f} sn")
if dates:
    print("En eski log:", datetime.date.fromtimestamp(min(dates)))
print("\nGirdi + çıktı (istemci bazında):")
for k, v in io.most_common():
    print(f"  {k:20} {m(v)}")
print(f"  {'TOPLAM':20} {m(sum(io.values()))}")
print("\nGirdi + çıktı (model bazında):")
for k, v in io_by_model.most_common():
    print(f"  {k:28} {m(v)}")
print("\n+ önbelleğe yazma dahil:", m(sum(new.values())))
print("Tekilleştirmesiz girdi + çıktı:", m(nodedup))
print("Arka plan Haiku (cost-state):", m(bg))

p = base + "/stats-cache.json"
if os.path.exists(p):
    try:
        s = json.load(open(p))
        print("\nstats-cache.json anahtarları:", list(s))
        for mdl, x in (s.get("modelUsage") or {}).items():
            print("  ", mdl, {k: v for k, v in x.items() if isinstance(v, (int, float))})
    except ValueError:
        print("\nstats-cache.json okunamadı")
else:
    print("\nstats-cache.json yok")

sp = base + "/settings.json"
try:
    print("cleanupPeriodDays:", json.load(open(sp)).get("cleanupPeriodDays", "ayarlanmamış (varsayılan 30 gün)"))
except (OSError, ValueError):
    print("cleanupPeriodDays: settings.json yok (varsayılan 30 gün)")
sys.exit(0)
