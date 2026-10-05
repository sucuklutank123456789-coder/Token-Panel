"""Finds log files, reads them incrementally and builds the panel summary."""

from __future__ import annotations

import glob
import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .limits import CALIBRATION_DAYS, WEEK_SECONDS, ClaudeLimits, LimitHit, estimate, read_official
from .model import DEFAULT_METRIC, SOURCE_LABELS, CodexLimits, Event, ThreadInfo, Usage, client_label
from .opencode import OpenCodeDb
from .parsers import ClaudeFile, CodexFile, JsonlFile
from .paths import claude_json_paths, default_claude_dirs, default_codex_dirs, default_opencode_dirs, opencode_dbs

RANGES = {
    "today": "Today",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    "all": "All time",
}

NO_TOOL = "Reply (no tools)"

# The overview chart always shows this many days, whatever the selected range.
DAILY_DAYS = 30

# Walking the log directories for new files is the expensive part, so it runs at most this often.
# In between, refresh() only stats the files it already knows.
DISCOVER_SECONDS = 60.0


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
    # Logs written on Windows use backslashes; read them the same way on any OS.
    norm = cwd.replace("\\", "/").rstrip("/")
    parts = [p for p in norm.split("/") if p]
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    if norm.lower() == home.lower() or _is_home(parts):
        return "~ (home)"
    return parts[-1] if parts else cwd


def _is_home(parts: list[str]) -> bool:
    if parts[:1] in (["home"], ["Users"]):
        return len(parts) <= 2  # /home/<user> on Linux, /Users/<user> on macOS
    # C:\Users\<user>
    return 2 <= len(parts) <= 3 and parts[0].endswith(":") and parts[1].lower() == "users"


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
    tools: dict[str, Usage] = field(default_factory=dict)
    calls: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0


@dataclass
class ClientRow:
    source: str
    client: str
    label: str
    usage: Usage = field(default_factory=Usage)
    models: dict[str, Usage] = field(default_factory=dict)
    threads: list[ThreadRow] = field(default_factory=list)


@dataclass
class DayRow:
    day: date
    by_source: dict[str, Usage]

    @property
    def total(self) -> Usage:
        u = Usage()
        for x in self.by_source.values():
            u.add(x)
        return u


@dataclass
class Summary:
    range_key: str
    metric: str
    total: Usage
    by_source: dict[str, Usage]
    clients: list[ClientRow]
    limits: CodexLimits | None
    file_count: int
    generated_at: float
    daily: list[DayRow] = field(default_factory=list)  # oldest first, the last DAILY_DAYS days
    claude_limits: ClaudeLimits | None = None


class Store:
    def __init__(
        self,
        claude_dirs: list[str] | None = None,
        codex_dirs: list[str] | str | None = None,
        opencode_dirs: list[str] | None = None,
    ):
        self.claude_dirs = claude_dirs if claude_dirs is not None else default_claude_dirs()
        if isinstance(codex_dirs, str):
            codex_dirs = [codex_dirs]
        self.codex_dirs = codex_dirs if codex_dirs is not None else default_codex_dirs()
        self.opencode_dirs = opencode_dirs if opencode_dirs is not None else default_opencode_dirs()
        self.files: dict[str, JsonlFile | OpenCodeDb] = {}
        self.events: dict[str, Event] = {}
        self.threads: dict[tuple[str, str], ThreadInfo] = {}
        self.limits: CodexLimits | None = None
        self._index_mtimes: dict[str, float] = {}
        self._discovered_at: float | None = None
        self.limit_hits: dict[tuple[float, str], LimitHit] = {}
        # Claude Code's own cached usage percentages (~/.claude.json, or inside CLAUDE_CONFIG_DIR).
        self.claude_json = claude_json_paths(self.claude_dirs)
        self._official_stamp: dict[str, float] = {}
        self._official: dict[str, tuple] = {}
        # Results that don't depend on the range or metric, computed once per data change and minute.
        self._generation = 0
        self._extras_key: tuple | None = None
        self._extras: tuple[list[DayRow], ClaudeLimits | None] = ([], None)
        self._index_names: dict[str, str] = {}

    # --- Sink interface -----------------------------------------------------
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

    def add_limit_hit(self, hit: LimitHit) -> None:
        self.limit_hits[(hit.ts, hit.kind)] = hit

    def _read_official(self) -> bool:
        changed = False
        for path in self.claude_json:
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                if self._official.pop(path, None) is not None:
                    changed = True
                continue
            if self._official_stamp.get(path) == mtime:
                continue
            self._official_stamp[path] = mtime
            snap = read_official(path)
            if snap is None:
                # Rewritten without the cached usage (logged out, partial write): forget the old figures.
                changed |= self._official.pop(path, None) is not None
            elif snap != self._official.get(path):
                self._official[path] = snap
                changed = True
        return changed

    def official_limits(self):
        """The newest official snapshot among the candidate files."""
        snaps = list(self._official.values())
        return max(snaps, key=lambda s: s[0]) if snaps else None

    # --- Scanning -----------------------------------------------------------
    def _discover(self) -> list[tuple[str, type]]:
        found = []
        for base in self.claude_dirs:
            for p in glob.glob(os.path.join(base, "projects", "**", "*.jsonl"), recursive=True):
                found.append((p, ClaudeFile))
        for base in self.codex_dirs:
            for sub in ("sessions", "archived_sessions"):
                for p in glob.glob(os.path.join(base, sub, "**", "*.jsonl"), recursive=True):
                    found.append((p, CodexFile))
        for p in opencode_dbs(self.opencode_dirs):
            found.append((p, OpenCodeDb))
        return found

    def _sync_files(self) -> None:
        found = self._discover()
        paths = {p for p, _ in found}
        # Files that disappeared (e.g. a Codex session moved to archived_sessions) keep their events.
        for gone in [p for p in self.files if p not in paths]:
            del self.files[gone]
        for path, cls in found:
            if path not in self.files:
                self.files[path] = cls(path)

    def refresh(self, discover: bool = False) -> bool:
        """Reads new/changed files. Returns True if anything changed.

        New files are looked for on the first call, every DISCOVER_SECONDS, or when discover is True.
        """
        now = time.monotonic()
        if discover or self._discovered_at is None or now - self._discovered_at >= DISCOVER_SECONDS:
            self._sync_files()
            self._discovered_at = now
        pending = []
        for f in self.files.values():
            try:
                st = os.stat(f.path)
            except OSError:
                continue
            if f.changed(st):
                pending.append((st.st_mtime, f))
        changed = False
        # Oldest files first: an API call present in several files belongs to the first one.
        pending.sort(key=lambda x: x[0])
        for _, f in pending:
            offset_before = f.offset
            f.update(self)
            changed |= f.offset != offset_before
        for base in self.codex_dirs:
            changed |= self._read_codex_index(os.path.join(base, "session_index.jsonl"))
        changed |= self._read_official()
        if changed:
            self._generation += 1
        return changed

    def _daily_and_limits(self, now: float) -> tuple[list[DayRow], ClaudeLimits | None]:
        """The 30-day table and Claude's limits: the same for every range and metric, so computed once."""
        key = (self._generation, int(now // 60), len(self.events))
        if key == self._extras_key:
            return self._extras
        today = datetime.fromtimestamp(now).date()
        days = [today - timedelta(days=i) for i in range(DAILY_DAYS - 1, -1, -1)]
        daily = {d: DayRow(d, {s: Usage() for s in SOURCE_LABELS}) for d in days}
        daily_start = datetime.combine(days[0], datetime.min.time()).timestamp()
        limits_start = now - CALIBRATION_DAYS * 86400 - WEEK_SECONDS
        claude_recent: list[Event] = []
        for ev in self.events.values():
            if ev.source == "claude" and ev.ts >= limits_start:
                claude_recent.append(ev)
            if ev.ts >= daily_start:
                row = daily.get(datetime.fromtimestamp(ev.ts).date())
                if row is not None:
                    row.by_source.setdefault(ev.source, Usage()).add(ev.usage)
        limits = estimate(claude_recent, list(self.limit_hits.values()), self.official_limits(), now)
        self._extras_key, self._extras = key, (list(daily.values()), limits)
        return self._extras

    def _read_codex_index(self, path: str) -> bool:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return False
        if mtime == self._index_mtimes.get(path):
            return False
        self._index_mtimes[path] = mtime
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

    # --- Summary -------------------------------------------------------------
    def summarize(self, range_key: str, metric: str = DEFAULT_METRIC, now: float | None = None) -> Summary:
        start = range_start(range_key, now)
        total = Usage()
        by_source: dict[str, Usage] = {s: Usage() for s in SOURCE_LABELS}
        clients: dict[tuple[str, str], ClientRow] = {}
        threads: dict[tuple[str, str, str], ThreadRow] = {}
        daily, claude_limits = self._daily_and_limits(time.time() if now is None else now)

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
            c.models.setdefault(model, Usage()).add(u)

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
            share = u.scaled(1 / len(names))
            for n in names:
                t.tools.setdefault(n, Usage()).add(share)

        rows = sorted(clients.values(), key=lambda c: (c.source, -c.usage.value(metric)))
        for c in rows:
            c.threads.sort(key=lambda t: -t.usage.value(metric))
        return Summary(
            range_key=range_key,
            metric=metric,
            total=total,
            by_source=by_source,
            clients=rows,
            limits=self.limits,
            file_count=len(self.files),
            generated_at=time.time(),
            daily=daily,
            claude_limits=claude_limits,
        )
