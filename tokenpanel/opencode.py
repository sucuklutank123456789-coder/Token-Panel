"""OpenCode (opencode.ai): reads its SQLite database, ~/.local/share/opencode/opencode.db.

OpenCode 1.2 and later keep sessions in SQLite (older JSON storage was imported into it on upgrade).
Every model call ends with a "step-finish" part that carries that call's tokens and cost, so each one becomes
an event. Tool parts between the step's start and its finish are the tools it used. OpenCode does not record
which frontend (terminal, desktop app, ACP) started a session, so all of it is one client.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from urllib.parse import quote

from .model import Event, Usage
from .parsers import Sink, _short
from .pricing import lookup, price_usage

CLIENT = "opencode"
# Part ids sort by creation time. Several OpenCode processes can write at once, so a row with a slightly older
# id can be committed after a newer one was read; each round re-reads the ids of the last minute to catch them.
OVERLAP_SECONDS = 60


def _load(raw) -> dict:
    try:
        d = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _num(d: dict, key: str) -> int:
    v = d.get(key)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def sqlite_uri(path: str) -> str:
    """A read-only file: URI. Windows UNC paths (\\\\wsl.localhost\\...) need an empty authority: file:////host/..."""
    p = os.path.abspath(path).replace("\\", "/")
    prefix = "file://" if p.startswith("//") else "file:"
    return prefix + quote(p, safe="/:") + "?mode=ro"


class OpenCodeDb:
    """One opencode*.db file, read incrementally."""

    def __init__(self, path: str):
        self.path = path
        self.offset = 0  # grows when something new was read; Store.refresh compares it
        self.stamp: tuple | None = None
        self.read_from = ""  # part ids at or above this are (re)read next round
        self.history: list[tuple[float, str]] = []  # (when, largest part id seen then)
        self.sessions: dict[str, tuple] = {}  # session id -> (parent, directory, title)
        self.messages: dict[str, tuple[str, str, str]] = {}  # message id -> (model, variant, cwd)
        self.pending_tools: dict[str, list[tuple[str, str]]] = {}  # message id -> [(part id, tool)]
        self.tool_parts: set[str] = set()
        self.step_parts: set[str] = set()
        self.steps: dict[str, list[tuple[str, Event]]] = {}  # message id -> [(step part id, its event)]
        self.waiting: set[str] = set()  # step parts whose message row wasn't visible yet

    def changed(self, st: os.stat_result) -> bool:
        # Writes land in the -wal file first; the main file can stay untouched for a long time.
        try:
            wal = os.stat(self.path + "-wal")
            wal_stamp = (wal.st_size, wal.st_mtime)
        except OSError:
            wal_stamp = None
        stamp = (st.st_size, st.st_mtime, wal_stamp)
        if stamp == self.stamp:
            return False
        self.stamp = stamp
        return True

    def _connect(self) -> sqlite3.Connection | None:
        # Read-only, so the panel can never change OpenCode's data.
        try:
            con = sqlite3.connect(sqlite_uri(self.path), uri=True, timeout=2)
        except sqlite3.Error:
            return None
        try:
            con.execute("PRAGMA query_only = 1")
        except sqlite3.Error:
            con.close()
            return None
        return con

    def update(self, sink: Sink) -> None:
        con = self._connect()
        if con is None:
            self.stamp = None  # try again next round even if the file doesn't change
            return
        try:
            self._sessions(con, sink)
            self._parts(con, sink)
        except sqlite3.Error:
            # Busy, or a schema this version doesn't know: try again next round. The read position only moves
            # after a whole batch went through, and re-reading is harmless.
            self.stamp = None
        finally:
            con.close()
        if self.waiting:
            self.stamp = None  # some steps wait for their message row; look again next round

    def _sessions(self, con: sqlite3.Connection, sink: Sink) -> None:
        # The session table is small; read it whole and pick out what changed (titles arrive later).
        for sid, parent, directory, title in con.execute("SELECT id, parent_id, directory, title FROM session"):
            row = (parent, directory, title)
            if self.sessions.get(sid) == row:
                continue
            self.sessions[sid] = row
            self.offset += 1
            if parent:
                continue  # a subagent: its usage belongs to the conversation that started it
            t = sink.thread("opencode", sid)
            if directory:
                t.cwd = directory
            if title:
                t.title = _short(title)

    def _root(self, sid: str) -> str:
        seen = set()
        while sid in self.sessions and self.sessions[sid][0] and sid not in seen:
            seen.add(sid)
            sid = self.sessions[sid][0]
        return sid

    def _parts(self, con: sqlite3.Connection, sink: Sink) -> None:
        # Only the parts the panel uses; text and tool output parts can be large. "id > ?" walks the primary key.
        rows = con.execute(
            "SELECT id, message_id, session_id, time_created, data FROM part"
            " WHERE id >= ? AND json_extract(data, '$.type') IN ('step-finish', 'tool') ORDER BY id",
            (self.read_from,),
        ).fetchall()
        if self.waiting:
            marks = ",".join("?" * len(self.waiting))
            rows += con.execute(
                f"SELECT id, message_id, session_id, time_created, data FROM part WHERE id IN ({marks})",
                sorted(self.waiting),
            ).fetchall()
            rows.sort(key=lambda r: r[0])
        for pid, mid, sid, created, pdata in rows:
            part = _load(pdata)
            kind = part.get("type")
            if kind == "tool" and pid not in self.tool_parts:
                self.tool_parts.add(pid)
                self.offset += 1
                self._tool(mid, pid, part.get("tool") or "tool")
            elif kind == "step-finish" and pid not in self.step_parts:
                msg = self._message(con, mid)
                if msg is None:
                    self.waiting.add(pid)
                    continue
                self.waiting.discard(pid)
                self.step_parts.add(pid)
                self.offset += 1
                self._step(pid, mid, sid, created, part, msg, sink)
        # The whole batch went through: move the read position, keeping the last minute.
        now = time.monotonic()
        if rows:
            self.history.append((now, max(self.read_from, rows[-1][0])))
        older = [mark for when, mark in self.history if when <= now - OVERLAP_SECONDS]
        if older:
            self.read_from = older[-1]
            self.history = [(w, m) for w, m in self.history if w > now - OVERLAP_SECONDS]

    def _tool(self, mid: str, pid: str, name: str) -> None:
        # Seen only now, but its step may already be counted (read in an earlier round): add it there.
        step = next((ev for step_id, ev in self.steps.get(mid, []) if step_id > pid), None)
        if step is not None:
            step.tools.append(name)
        else:
            self.pending_tools.setdefault(mid, []).append((pid, name))

    def _message(self, con: sqlite3.Connection, mid: str) -> tuple[str, str, str] | None:
        msg = self.messages.get(mid)
        if msg is None:
            row = con.execute("SELECT data FROM message WHERE id = ?", (mid,)).fetchone()
            if row is None:
                return None  # not visible yet
            d = _load(row[0])
            path = d.get("path") if isinstance(d.get("path"), dict) else {}
            msg = self.messages[mid] = (d.get("modelID") or "", d.get("variant") or "", path.get("cwd") or "")
        return msg

    def _step(self, pid: str, mid: str, sid: str, created, part: dict, msg: tuple, sink: Sink) -> None:
        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        reasoning = _num(tokens, "reasoning")
        u = Usage(
            input=_num(tokens, "input"),
            cache_read=_num(cache, "read"),
            cache_write=_num(cache, "write"),
            # OpenCode keeps reasoning out of "output"; Claude Code and Codex count it in, and so does the panel.
            output=_num(tokens, "output") + reasoning,
            reasoning=reasoning,
        )
        # OpenCode has no running total of its own to match; counted like the other apps.
        u.app_in, u.app_out, u.app_cache = u.input, u.output, u.cache_read + u.cache_write
        model, variant, cwd = msg
        if lookup(model) is not None:
            price_usage(u, model)
        else:
            # Unknown to the panel: use the cost OpenCode itself computed from models.dev, when it has one.
            cost = part.get("cost")
            if isinstance(cost, (int, float)) and cost > 0:
                u.cost = float(cost)
            else:
                price_usage(u, model)
        # Tools used in this step: those created before its finish (part ids sort by creation).
        tools = [name for tool_id, name in self.pending_tools.get(mid, []) if tool_id < pid]
        self.pending_tools[mid] = [(t, n) for t, n in self.pending_tools.get(mid, []) if t >= pid]
        thread = self._root(sid)
        ev = Event(
            source="opencode",
            client=CLIENT,
            thread_id=thread,
            ts=(created or 0) / 1000,
            model=model,
            effort=variant,
            usage=u,
            tools=tools,
        )
        if sink.add_event(f"opencode:{pid}", ev) is None:
            self.steps.setdefault(mid, []).append((pid, ev))
        t = sink.thread("opencode", thread)
        if not t.cwd and cwd:
            t.cwd = cwd
