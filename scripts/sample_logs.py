"""Writes a small set of fake Claude Code and Codex logs, for smoke tests of the built app.

    python scripts/sample_logs.py OUT_DIR

creates OUT_DIR/claude, OUT_DIR/codex and OUT_DIR/opencode; pass them as --claude-dir, --codex-dir and
--opencode-dir.
The working directories are Windows paths, like logs written on Windows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone


def _write(path: str, rows: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def write_sample(out: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    cwd = r"C:\Users\sample\token-panel"

    def claude(mid: str, entry: str, tool: str, inp: int, out_: int) -> dict:
        return {
            "type": "assistant", "sessionId": f"s-{entry}", "entrypoint": entry, "cwd": cwd,
            "gitBranch": "main", "requestId": f"r-{mid}", "timestamp": now,
            "message": {
                "id": mid, "model": "claude-opus-5-5",
                "content": [{"type": "tool_use", "name": tool, "id": "t", "input": {}}],
                "usage": {"input_tokens": inp, "output_tokens": out_, "cache_read_input_tokens": 1000,
                          "cache_creation_input_tokens": 200},
            },
        }

    for entry in ("cli", "claude-desktop"):
        _write(os.path.join(out, "claude", "projects", "C--Users-sample-token-panel", f"s-{entry}.jsonl"), [
            {"type": "user", "sessionId": f"s-{entry}", "message": {"content": f"Sample thread ({entry})"}},
            claude(f"m1-{entry}", entry, "Bash", 120, 300),
            claude(f"m2-{entry}", entry, "Read", 80, 150),
        ])

    day = time.strftime("%Y/%m/%d").split("/")
    usage = {"input_tokens": 5000, "cached_input_tokens": 3000, "output_tokens": 400, "reasoning_output_tokens": 100}
    _write(os.path.join(out, "codex", "sessions", *day, "rollout-sample.jsonl"), [
        {"timestamp": now, "type": "session_meta",
         "payload": {"id": "codex-sample", "originator": "Codex Desktop", "cwd": cwd}},
        {"type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "Sample Codex thread: Yanıtla çiz"}],
        }},
        {"type": "turn_context", "payload": {"turn_id": "u1", "model": "gpt-5.5", "effort": "high"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
        {"timestamp": now, "type": "token_usage_record",
         "payload": {"response_id": "resp1", "turn_id": "u1", "usage": usage}},
        {"timestamp": now, "type": "event_msg",
         "payload": {"type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage}}},
    ])
    write_compressed_codex(out, cwd)
    write_opencode(os.path.join(out, "opencode"), cwd)


def write_compressed_codex(out: str, cwd: str) -> None:
    """A Codex CLI session from 10 days ago, compressed to .jsonl.zst as Codex does with files older than 7 days."""
    try:
        import zstandard
    except ImportError:
        return
    when = datetime.now(timezone.utc) - timedelta(days=10)
    ts = when.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    usage = {"input_tokens": 9000, "cached_input_tokens": 6000, "output_tokens": 900, "reasoning_output_tokens": 0}
    rows = [
        {"timestamp": ts, "type": "session_meta",
         "payload": {"id": "codex-old", "originator": "codex_cli_rs", "cwd": cwd}},
        {"type": "turn_context", "payload": {"turn_id": "u1", "model": "gpt-5.5", "effort": "medium"}},
        {"timestamp": ts, "type": "token_usage_record",
         "payload": {"response_id": "resp-old", "turn_id": "u1", "usage": usage}},
        {"timestamp": ts, "type": "event_msg",
         "payload": {"type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage}}},
    ]
    path = os.path.join(out, "codex", "sessions", *when.strftime("%Y/%m/%d").split("/"), "rollout-old.jsonl.zst")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = "".join(json.dumps(r) + "\n" for r in rows).encode()
    with open(path, "wb") as fh:
        fh.write(zstandard.ZstdCompressor().compress(data))


def write_opencode(directory: str, cwd: str) -> None:
    """A minimal opencode.db with the tables and columns Token Panel reads."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "opencode.db")
    if os.path.exists(path):
        os.remove(path)
    ms = int(time.time() * 1000)
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT,"
        " time_created INTEGER, time_updated INTEGER);"
        "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,"
        " time_updated INTEGER, data TEXT);"
        "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER,"
        " time_updated INTEGER, data TEXT);"
    )
    con.execute("INSERT INTO session VALUES ('ses_1', NULL, ?, 'Sample OpenCode session', ?, ?)", (cwd, ms, ms))
    msg = {"role": "assistant", "modelID": "claude-sonnet-5-5", "providerID": "anthropic", "path": {"cwd": cwd}}
    con.execute("INSERT INTO message VALUES ('msg_1', 'ses_1', ?, ?, ?)", (ms, ms, json.dumps(msg)))
    parts = [
        {"type": "tool", "tool": "bash", "state": {"status": "completed"}},
        {"type": "step-finish", "cost": 0, "tokens": {"input": 900, "output": 120, "reasoning": 30,
                                                      "cache": {"read": 2000, "write": 0}}},
    ]
    for i, part in enumerate(parts):
        con.execute("INSERT INTO part VALUES (?, 'msg_1', 'ses_1', ?, ?, ?)",
                    (f"prt_{i}", ms + i, ms + i, json.dumps(part)))
    con.commit()
    con.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    write_sample(sys.argv[1])
