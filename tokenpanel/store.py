"""Log dosyalarını bulur, artımlı okur ve panel için özet üretir."""

from __future__ import annotations

import glob
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from .model import SOURCE_LABELS, CodexLimits, Event, ThreadInfo, Usage, client_label
from .parsers import ClaudeFile, CodexFile, JsonlFile

RANGES = {
    "today": "Bugün",
    "7d": "Son 7 gün",
    "30d": "Son 30 gün",
    "all": "Tümü",
}

NO_TOOL = "Yanıt (araçsız)"


def default_claude_dirs() -> list[str]:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    bases = [p for p in env.split(",") if p] if env else []
    home = os.path.expanduser("~")
    bases += [os.path.join(home, ".claude"), os.path.join(home, ".config", "claude")]
    seen, out = set(), []
    for b in bases:
        b = os.path.realpath(b)
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def default_codex_dir() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def range_start(key: str, now: float | None = None) -> float:
    now = time.time() if now is None else now
    if key == "today":
        d = datetime.fromtimestamp(now)
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    if key == "7d":
        return now - 7 * 86400
    if key == "30d":
        return now - 30 * 86400
    return 0.0


def project_name(cwd: str) -> str:
    if not cwd:
        return "—"
    cwd = cwd.rstrip("/")
    if cwd == os.path.expanduser("~") or cwd.count("/") <= 2 and cwd.startswith("/home/"):
        return "~ (ana dizin)"
    return os.path.basename(cwd) or cwd


@dataclass
class ThreadRow:
    source: str
    client: str
    thread_id: str
    title: str
    project: str
    cwd: str
    branch: str
    usage: Usage = field(default_factory=Usage)
    model_usage: dict[str, Usage] = field(default_factory=dict)
    efforts: set[str] = field(default_factory=set)
    tools: dict[str, float] = field(default_factory=dict)
    calls: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0


@dataclass
class ClientRow:
    source: str
    client: str
    label: str
    usage: Usage = field(default_factory=Usage)
    models: dict[str, int] = field(default_factory=dict)
    threads: list[ThreadRow] = field(default_factory=list)


@dataclass
class Summary:
    range_key: str
    total: Usage
    by_source: dict[str, Usage]
    clients: list[ClientRow]
    limits: CodexLimits | None
    file_count: int
    generated_at: float


class Store:
    def __init__(self, claude_dirs: list[str] | None = None, codex_dir: str | None = None):
        self.claude_dirs = claude_dirs if claude_dirs is not None else default_claude_dirs()
        self.codex_dir = codex_dir if codex_dir is not None else default_codex_dir()
        self.files: dict[str, JsonlFile] = {}
        self.events: dict[str, Event] = {}
        self.threads: dict[tuple[str, str], ThreadInfo] = {}
        self.limits: CodexLimits | None = None
        self._index_mtime = -1.0
        self._index_names: dict[str, str] = {}

    # --- Sink arayüzü -----------------------------------------------------
    def add_event(self, key: str, event: Event) -> Event | None:
        existing = self.events.get(key)
        if existing is not None:
            return existing
        self.events[key] = event
        return None

    def thread(self, source: str, thread_id: str) -> ThreadInfo:
        k = (source, thread_id)
        t = self.threads.get(k)
        if t is None:
            t = self.threads[k] = ThreadInfo(source=source, thread_id=thread_id)
        return t

    def set_codex_limits(self, limits: CodexLimits) -> None:
        if self.limits is None or limits.ts >= self.limits.ts:
            self.limits = limits

    # --- Tarama -----------------------------------------------------------
    def _discover(self) -> list[tuple[str, type]]:
        found = []
        for base in self.claude_dirs:
            for p in glob.glob(os.path.join(base, "projects", "**", "*.jsonl"), recursive=True):
                found.append((p, ClaudeFile))
        for sub in ("sessions", "archived_sessions"):
            for p in glob.glob(os.path.join(self.codex_dir, sub, "**", "*.jsonl"), recursive=True):
                found.append((p, CodexFile))
        return found

    def refresh(self) -> bool:
        """Yeni/değişen dosyaları okur. Değişiklik olduysa True döner."""
        changed = False
        # Önce eski dosyalar: aynı API çağrısı birden çok dosyada varsa ilk sahibine yazılsın.
        for path, cls in sorted(self._discover(), key=lambda x: _mtime(x[0])):
            f = self.files.get(path)
            if f is None:
                f = self.files[path] = cls(path)
            size_before = f.offset
            f.update(self)
            changed |= f.offset != size_before
        changed |= self._read_codex_index()
        return changed

    def _read_codex_index(self) -> bool:
        path = os.path.join(self.codex_dir, "session_index.jsonl")
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return False
        if mtime == self._index_mtime:
            return False
        self._index_mtime = mtime
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    tid = d.get("id") or d.get("thread_id")
                    name = d.get("thread_name") or d.get("name")
                    if tid and name:
                        self._index_names[tid] = name
        except OSError:
            return False
        for tid, name in self._index_names.items():
            self.thread("codex", tid).title = name
        return True

    # --- Özet -------------------------------------------------------------
    def summarize(self, range_key: str, now: float | None = None) -> Summary:
        start = range_start(range_key, now)
        total = Usage()
        by_source: dict[str, Usage] = {s: Usage() for s in SOURCE_LABELS}
        clients: dict[tuple[str, str], ClientRow] = {}
        threads: dict[tuple[str, str, str], ThreadRow] = {}

        for ev in self.events.values():
            if ev.ts < start:
                continue
            u = ev.usage
            total.add(u)
            by_source.setdefault(ev.source, Usage()).add(u)

            ck = (ev.source, ev.client)
            c = clients.get(ck)
            if c is None:
                c = clients[ck] = ClientRow(ev.source, ev.client, client_label(ev.source, ev.client))
            c.usage.add(u)
            model = ev.model or "bilinmiyor"
            c.models[model] = c.models.get(model, 0) + u.total

            tk = (ev.source, ev.client, ev.thread_id)
            t = threads.get(tk)
            if t is None:
                info = self.thread(ev.source, ev.thread_id)
                t = threads[tk] = ThreadRow(
                    source=ev.source,
                    client=ev.client,
                    thread_id=ev.thread_id,
                    title=info.display_title,
                    project=project_name(info.cwd),
                    cwd=info.cwd,
                    branch=info.branch,
                    first_ts=ev.ts,
                    last_ts=ev.ts,
                )
                c.threads.append(t)
            t.usage.add(u)
            t.model_usage.setdefault(model, Usage()).add(u)
            if ev.effort:
                t.efforts.add(ev.effort)
            t.calls += 1
            t.first_ts = min(t.first_ts, ev.ts)
            t.last_ts = max(t.last_ts, ev.ts)
            names = ev.tools or [NO_TOOL]
            share = u.total / len(names)
            for n in names:
                t.tools[n] = t.tools.get(n, 0.0) + share

        rows = sorted(clients.values(), key=lambda c: (c.source, -c.usage.total))
        for c in rows:
            c.threads.sort(key=lambda t: -t.usage.total)
        return Summary(
            range_key=range_key,
            total=total,
            by_source=by_source,
            clients=rows,
            limits=self.limits,
            file_count=len(self.files),
            generated_at=time.time(),
        )


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0
