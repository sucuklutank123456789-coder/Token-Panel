"""Writes a small set of fake Claude Code and Codex logs, for smoke tests of the built app.

    python scripts/sample_logs.py OUT_DIR

creates OUT_DIR/claude and OUT_DIR/codex; pass them as --claude-dir and --codex-dir.
The working directories are Windows paths, like logs written on Windows.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone


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


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    write_sample(sys.argv[1])
