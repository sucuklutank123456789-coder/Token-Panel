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
from urllib.parse import quote

from .model import Event, Usage
from .parsers import Sink, _short
from .pricing import lookup, price_usage

CLIENT = "opencode"


def _load(raw) -> dict:
    try:
        d = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _num(d: dict, key: str) -> int:
    v = d.get(key)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


class OpenCodeDb:
    """One opencode*.db file, read incrementally by row update time."""

    def __init__(self, path: str):
        self.path = path
        self.offset = 0  # kept for Store.refresh's change check; counts rows read
        self.stamp: tuple | None = None
        self.part_seen = 0  # largest part.time_updated read so far
        self.session_seen = 0
        self.messages: dict[str, tuple[str, str, str]] = {}  # message id -> (model, variant, cwd)
        self.pending_tools: dict[str, list[tuple[str, str]]] = {}  # message id -> [(part id, tool)]
        self.tool_parts: set[str] = set()
        self.steps: dict[str, list[tuple[str, Event]]] = {}  # message id -> [(step part id, its event)]
        self.roots: dict[str, str] = {}  # session id -> parent session id (subagents)

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
        # Read-only, so the panel can never change OpenCode's data. "C:/x" is a valid URI path on Windows.
        uri = "file:" + quote(os.path.abspath(self.path).replace("\\", "/"), safe="/:") + "?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=2)
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
            # Busy, or a schema this version doesn't know: try again next round. Rows read so far are kept, and
            # re-reading them is harmless.
            self.stamp = None
        finally:
            con.close()

    def _sessions(self, con: sqlite3.Connection, sink: Sink) -> None:
        # ">=": a row written in the same millisecond as the last one read is not lost; reading twice is harmless.
        rows = con.execute(
            "SELECT id, parent_id, directory, title, time_updated FROM session"
            " WHERE time_updated >= ? ORDER BY time_updated",
            (self.session_seen,),
        ).fetchall()
        for sid, parent, directory, title, updated in rows:
            self.session_seen = max(self.session_seen, updated or 0)
            self.offset += 1
            if parent:
                # A subagent's usage belongs to the conversation that started it.
                self.roots[sid] = parent
                continue
            t = sink.thread("opencode", sid)
            if directory:
                t.cwd = directory
            if title:
                t.title = _short(title)

    def _root(self, sid: str) -> str:
        seen = set()
        while sid in self.roots and sid not in seen:
            seen.add(sid)
            sid = self.roots[sid]
        return sid

    def _parts(self, con: sqlite3.Connection, sink: Sink) -> None:
        # Only the parts the panel uses; text and tool output parts can be large.
        rows = con.execute(
            "SELECT id, message_id, session_id, time_created, time_updated, data FROM part"
            " WHERE time_updated >= ? AND json_extract(data, '$.type') IN ('step-finish', 'tool')"
            " ORDER BY time_updated, id",
            (self.part_seen,),
        ).fetchall()
        # Rows come in update order, but a tool part can be updated after its step finished (when OpenCode
        # prunes old tool output). Part ids sort by creation, so handle each batch in creation order.
        rows.sort(key=lambda r: r[0])
        for pid, mid, sid, created, updated, pdata in rows:
            self.part_seen = max(self.part_seen, updated or 0)
            self.offset += 1
            part = _load(pdata)
            kind = part.get("type")
            if kind == "tool":
                if pid not in self.tool_parts:
                    self.tool_parts.add(pid)
                    self._tool(mid, pid, part.get("tool") or "tool")
            elif kind == "step-finish":
                self._step(pid, mid, sid, created, part, self._message(con, mid), sink)

    def _tool(self, mid: str, pid: str, name: str) -> None:
        # Seen only now, but its step may already be counted (read in an earlier round): add it there.
        step = next((ev for step_id, ev in self.steps.get(mid, []) if step_id > pid), None)
        if step is not None:
            step.tools.append(name)
        else:
            self.pending_tools.setdefault(mid, []).append((pid, name))

    def _message(self, con: sqlite3.Connection, mid: str) -> tuple[str, str, str]:
        msg = self.messages.get(mid)
        if msg is None:
            row = con.execute("SELECT data FROM message WHERE id = ?", (mid,)).fetchone()
            if row is None:
                return "", "", ""  # not visible yet; don't remember that
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
        # OpenCode shows input + output; there is no separate counter to match.
        u.app_in, u.app_out = u.input, u.output
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
