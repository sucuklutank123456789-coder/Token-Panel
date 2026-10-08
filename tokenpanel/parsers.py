"""Incremental parsers for Claude Code and Codex JSONL logs.

One parser object is kept per file; as a file grows only the new lines are read.
Parsers write events and thread info into a shared `Sink`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Protocol

from .model import CodexLimits, Event, RateWindow, ThreadInfo, Usage
from .pricing import price_usage


class Sink(Protocol):
    def add_event(self, key: str, event: Event) -> Event | None:
        """Adds a new event; if the key was seen before, returns the existing event."""

    def thread(self, source: str, thread_id: str) -> ThreadInfo: ...

    def set_codex_limits(self, limits: CodexLimits) -> None: ...


def parse_ts(value) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _int(d: dict, key: str) -> int:
    v = d.get(key)
    return v if isinstance(v, int) else 0


def _prompt_text(text: str) -> str:
    """Title candidate from a user message, skipping system/context blocks."""
    text = text.strip()
    # The Codex VS Code extension prepends IDE context to the request.
    for marker in ("## My request for Codex:", "## My request:"):
        if marker in text:
            text = text.split(marker, 1)[1].strip()
    if text.startswith("<"):
        return ""
    return text.removeprefix("/goal ").strip()


def _blended(u: dict) -> tuple[int, int, int]:
    """Codex's counter as (non-cached input, cached input, output); input_tokens includes the cached part."""
    inp = _int(u, "input_tokens")
    non_cached = max(inp - _int(u, "cached_input_tokens"), 0)
    return non_cached, inp - non_cached, _int(u, "output_tokens")


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class JsonlFile:
    """Reads a file from where it left off; a half-written last line waits for the next round."""

    def __init__(self, path: str):
        self.path = path
        self.offset = 0
        self.size = -1
        self.mtime = -1.0

    def changed(self, st: os.stat_result) -> bool:
        if st.st_size == self.size and st.st_mtime == self.mtime:
            return False
        if st.st_size < self.offset:  # file was truncated/rewritten
            self.offset = 0
            self.reset()
        self.size, self.mtime = st.st_size, st.st_mtime
        return True

    def reset(self) -> None:
        pass

    def read_new(self):
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read()
        except OSError:
            return
        end = data.rfind(b"\n")
        if end < 0:
            return
        self.offset += end + 1
        for raw in data[: end + 1].splitlines():
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj

    def update(self, sink: Sink) -> None:
        """Reads what was appended since the last call; the caller has checked changed()."""
        for obj in self.read_new():
            self.handle(obj, sink)

    def handle(self, obj: dict, sink: Sink) -> None:
        raise NotImplementedError


class ClaudeFile(JsonlFile):
    """~/.claude/projects/<project>/<session>.jsonl

    An assistant response can be split across several lines (one per content block, each
    carrying the same usage), so calls are deduplicated by message.id + requestId.
    """

    def __init__(self, path: str):
        super().__init__(path)
        # Claude Code's /stats reads projects/<project>/*.jsonl and <project>/<session>/subagents/agent-*.jsonl
        # only; workflow subagents (subagents/workflows/...) are left out of it.
        parent = os.path.dirname(path)
        if os.path.basename(parent) == "subagents":
            self.stats_kind = "subagent" if os.path.basename(path).startswith("agent-") else ""
        elif os.path.basename(os.path.dirname(parent)) == "projects":
            self.stats_kind = "main"
        else:
            self.stats_kind = ""

    def handle(self, d: dict, sink: Sink) -> None:
        kind = d.get("type")
        sid = d.get("sessionId")
        if not sid:
            return

        if kind == "assistant":
            self._assistant(d, sid, sink)
        elif kind == "custom-title" and d.get("customTitle"):
            sink.thread("claude", sid).title = _short(d["customTitle"])
        elif kind == "ai-title" and d.get("aiTitle"):
            # A better title than the first user message; overrides it.
            sink.thread("claude", sid).fallback_title = _short(d["aiTitle"])
        elif kind == "user":
            self._user(d, sid, sink)

    def _user(self, d: dict, sid: str, sink: Sink) -> None:
        t = sink.thread("claude", sid)
        if d.get("cwd"):
            t.cwd = d["cwd"]
        if d.get("gitBranch"):
            t.branch = d["gitBranch"]
        if t.fallback_title or d.get("isMeta") or d.get("isSidechain"):
            return
        content = (d.get("message") or {}).get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    break
        text = _prompt_text(text)
        if text:
            t.fallback_title = _short(text)

    def _assistant(self, d: dict, sid: str, sink: Sink) -> None:
        msg = d.get("message") or {}
        usage = msg.get("usage")
        model = msg.get("model") or ""
        if model == "<synthetic>" or d.get("isApiErrorMessage"):
            return  # not a model call (error and limit messages Claude Code writes itself)
        if not isinstance(usage, dict):
            return
        details = usage.get("output_tokens_details") or {}
        u = Usage(
            input=_int(usage, "input_tokens"),
            cache_read=_int(usage, "cache_read_input_tokens"),
            cache_write=_int(usage, "cache_creation_input_tokens"),
            output=_int(usage, "output_tokens"),
            reasoning=_int(details, "thinking_tokens") if isinstance(details, dict) else 0,
        )
        tools = [
            b.get("name") or "tool"
            for b in msg.get("content") or []
            if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use")
        ]
        # The app's stats skip sidechain lines in main files but count subagent files.
        counted = self.stats_kind == "subagent" or (self.stats_kind == "main" and not d.get("isSidechain"))
        if counted:
            u.app_in, u.app_out, u.app_cache = u.input, u.output, u.cache_read + u.cache_write
        creation = usage.get("cache_creation")
        key = f"claude:{msg.get('id')}:{d.get('requestId')}"
        ev = Event(
            source="claude",
            client=d.get("entrypoint") or "",
            thread_id=sid,
            ts=parse_ts(d.get("timestamp")),
            model=model,
            effort=d.get("effort") or "",
            usage=u,
            tools=tools,
            write_1h=_int(creation, "ephemeral_1h_input_tokens") if isinstance(creation, dict) else 0,
            fast=usage.get("speed") == "fast",
        )
        existing = sink.add_event(key, ev)
        if existing is None:
            price_usage(u, model, ev.write_1h, ev.fast)
        else:
            existing.usage.merge_max(u)
            existing.usage.app_in += u.app_in
            existing.usage.app_out += u.app_out
            existing.usage.app_cache += u.app_cache
            existing.tools.extend(tools)
            existing.write_1h = max(existing.write_1h, ev.write_1h)
            price_usage(existing.usage, existing.model, existing.write_1h, existing.fast)

        t = sink.thread("claude", sid)
        if d.get("cwd"):
            t.cwd = d["cwd"]
        if d.get("gitBranch"):
            t.branch = d["gitBranch"]


class CodexFile(JsonlFile):
    """~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

    Newer versions write a `token_usage_record` per API call. Older versions only have
    cumulative `token_count` events, which are used as a fallback.
    """

    def __init__(self, path: str):
        super().__init__(path)
        self.reset()

    def reset(self) -> None:
        self.thread_id = ""
        self.client = ""
        self.model = ""
        self.effort = ""
        self.turn_models: dict[str, tuple[str, str]] = {}
        self.pending_tools: list[str] = []
        self.has_records = False
        self.last_total = -1
        # Codex's own counter (token_count.total_token_usage), for the "app" metric.
        self.last_event: Event | None = None
        self.app_total: tuple[int, int, int] | None = None

    def handle(self, d: dict, sink: Sink) -> None:
        kind = d.get("type")
        p = d.get("payload")
        if not isinstance(p, dict):
            return

        if kind == "session_meta":
            self.thread_id = p.get("id") or p.get("session_id") or self.thread_id
            self.client = p.get("originator") or ""
            t = sink.thread("codex", self.thread_id)
            t.cwd = p.get("cwd") or t.cwd
            git = p.get("git")
            if isinstance(git, dict) and git.get("branch"):
                t.branch = git["branch"]
            return
        if not self.thread_id:
            return

        if kind == "turn_context":
            self._settings(p, p.get("turn_id"))
        elif kind == "event_msg":
            self._event_msg(d, p, sink)
        elif kind == "response_item":
            self._response_item(p, sink)
        elif kind == "token_usage_record":
            self.has_records = True
            u = p.get("usage") or {}
            turn = p.get("turn_id")
            model, effort = self.turn_models.get(turn, (self.model, self.effort))
            self._emit(
                f"codex:{p.get('response_id') or (self.thread_id, d.get('ordinal'))}",
                d.get("timestamp"),
                u,
                model,
                effort,
                sink,
            )

    def _settings(self, p: dict, turn_id: str | None) -> None:
        mode = (p.get("collaboration_mode") or {}).get("settings") or {}
        self.model = p.get("model") or mode.get("model") or self.model
        self.effort = (
            p.get("effort") or p.get("reasoning_effort") or mode.get("reasoning_effort") or self.effort
        )
        if turn_id:
            self.turn_models[turn_id] = (self.model, self.effort)

    def _event_msg(self, d: dict, p: dict, sink: Sink) -> None:
        sub = p.get("type")
        if sub == "thread_settings_applied":
            self._settings(p.get("thread_settings") or {}, None)
        elif sub == "thread_name_updated" and p.get("thread_name"):
            sink.thread("codex", self.thread_id).title = _short(p["thread_name"])
        elif sub == "thread_goal_updated":
            goal = p.get("goal") or {}
            t = sink.thread("codex", self.thread_id)
            if not t.fallback_title and goal.get("objective"):
                t.fallback_title = _short(goal["objective"])
        elif sub == "user_message":
            t = sink.thread("codex", self.thread_id)
            msg = _prompt_text(p.get("message") or "")
            if not t.fallback_title and msg:
                t.fallback_title = _short(msg)
        elif sub == "token_count":
            self._limits(d, p, sink)
            info = p.get("info")
            if not isinstance(info, dict):
                return
            if self.has_records:
                self._app_counter(info)
                return
            total = (info.get("total_token_usage") or {}).get("total_tokens")
            last = info.get("last_token_usage")
            if not isinstance(last, dict) or total is None or total == self.last_total:
                return
            self.last_total = total
            self._emit(f"codex:{self.thread_id}:{total}", d.get("timestamp"), last, self.model, self.effort, sink)

    def _app_counter(self, info: dict) -> None:
        """Credits the growth of the total Codex shows to the latest model call.

        Codex logs the summary call it makes when compacting a conversation as a
        token_usage_record but leaves it out of its own counter; this keeps the metric equal
        to what Codex shows.
        """
        total = info.get("total_token_usage")
        if not isinstance(total, dict):
            return
        blended = _blended(total)
        if self.app_total is None:
            # In resumed sessions the counter carries over from the previous file; start from there.
            last = info.get("last_token_usage")
            prev = _blended(last) if isinstance(last, dict) else (0, 0, 0)
            self.app_total = tuple(b - p for b, p in zip(blended, prev, strict=True))
        d_in, d_cache, d_out = (b - a for b, a in zip(blended, self.app_total, strict=True))
        self.app_total = blended
        if self.last_event is not None:
            # The counter only grows; each part is credited separately.
            u = self.last_event.usage
            u.app_in += max(d_in, 0)
            u.app_cache += max(d_cache, 0)
            u.app_out += max(d_out, 0)

    def _response_item(self, p: dict, sink: Sink) -> None:
        sub = p.get("type")
        if sub in ("function_call", "custom_tool_call", "local_shell_call"):
            self.pending_tools.append(p.get("name") or "shell")
        elif sub == "web_search_call":
            self.pending_tools.append("web_search")
        elif sub == "message" and p.get("role") == "user":
            t = sink.thread("codex", self.thread_id)
            if t.fallback_title:
                return
            for c in p.get("content") or []:
                text = _prompt_text(c.get("text") or "") if isinstance(c, dict) else ""
                if text:
                    t.fallback_title = _short(text)
                    break

    def _emit(self, key, ts, u: dict, model: str, effort: str, sink: Sink) -> None:
        inp = _int(u, "input_tokens")
        cached = _int(u, "cached_input_tokens")
        write = _int(u, "cache_write_input_tokens")
        usage = Usage(
            input=max(inp - cached - write, 0),
            cache_read=cached,
            cache_write=write,
            output=_int(u, "output_tokens"),
            reasoning=_int(u, "reasoning_output_tokens"),
        )
        # With records, the app counter is filled in by the token_count that follows.
        if not self.has_records:
            usage.app_in, usage.app_out = usage.input, usage.output
            usage.app_cache = usage.cache_read + usage.cache_write
        price_usage(usage, model)
        ev = Event(
            source="codex",
            client=self.client,
            thread_id=self.thread_id,
            ts=parse_ts(ts),
            model=model,
            effort=effort,
            usage=usage,
            tools=self.pending_tools,
        )
        self.pending_tools = []
        # If this call was already counted from another file (e.g. an archived copy), skip the counter.
        self.last_event = ev if sink.add_event(str(key), ev) is None else None

    def _limits(self, d: dict, p: dict, sink: Sink) -> None:
        rl = p.get("rate_limits")
        if not isinstance(rl, dict):
            return

        def window(w) -> RateWindow | None:
            if not isinstance(w, dict) or w.get("used_percent") is None:
                return None
            return RateWindow(
                used_percent=float(w["used_percent"]),
                window_minutes=int(w.get("window_minutes") or 0),
                resets_at=float(w.get("resets_at") or 0),
            )

        sink.set_codex_limits(
            CodexLimits(
                ts=parse_ts(d.get("timestamp")),
                plan=rl.get("plan_type") or "",
                primary=window(rl.get("primary")),
                secondary=window(rl.get("secondary")),
            )
        )
