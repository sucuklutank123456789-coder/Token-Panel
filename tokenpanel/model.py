"""Shared data types and labels."""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCE_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
}

# Claude Code: the "entrypoint" field on every line.
CLAUDE_CLIENTS = {
    "cli": "CLI",
    "claude-desktop": "Desktop",
    "claude-vscode": "VS Code",
    "sdk-ts": "ACP (Zed) / SDK",
    "sdk-py": "SDK (Python)",
    "remote_desktop": "Remote session",
}

# Codex: the "originator" field in session_meta.
CODEX_CLIENTS = {
    "codex-tui": "CLI",
    "codex_cli_rs": "CLI",
    "codex_exec": "CLI (exec)",
    "Codex Desktop": "Desktop",
    "codex_vscode": "VS Code",
    "zed": "ACP (Zed)",
}


# What the panel counts as "tokens".
METRICS = {
    "app": "Same as the official apps",
    "io": "Input + output",
    "new": "Input + output + cache writes",
    "raw": "Raw (including cache reads)",
}
DEFAULT_METRIC = "app"


def client_label(source: str, client: str) -> str:
    table = CLAUDE_CLIENTS if source == "claude" else CODEX_CLIENTS
    return table.get(client, client or "Unknown")


@dataclass
class Usage:
    """Token breakdown of one API call (or of a sum of calls).

    total = input + cache_read + cache_write + output.
    reasoning is the thinking part of output (not added to the total again).
    """

    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0
    # Input + output as the official apps report it.
    # Claude: the stats screen counts every transcript line of a response that was split across lines.
    # Codex: its own counter (token_count) leaves out conversation compaction calls.
    app_io: float = 0

    @property
    def total(self) -> int:
        return self.input + self.cache_read + self.cache_write + self.output

    def value(self, metric: str) -> float:
        """Value under the given metric.

        app: what Claude Code's stats ("Total tokens") and Codex's own counter show
        io : non-cached input + output, every model call once (actual spend)
        new: io + input written to the cache
        raw: everything, including context re-read from the cache on every call
        """
        if metric == "app":
            return self.app_io
        if metric == "raw":
            return self.total
        if metric == "new":
            return self.input + self.cache_write + self.output
        return self.input + self.output

    def scaled(self, f: float) -> "Usage":
        return Usage(
            self.input * f, self.cache_read * f, self.cache_write * f, self.output * f, self.reasoning * f,
            self.app_io * f,
        )

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.output += other.output
        self.reasoning += other.reasoning
        self.app_io += other.app_io

    def merge_max(self, other: "Usage") -> None:
        self.input = max(self.input, other.input)
        self.cache_read = max(self.cache_read, other.cache_read)
        self.cache_write = max(self.cache_write, other.cache_write)
        self.output = max(self.output, other.output)
        self.reasoning = max(self.reasoning, other.reasoning)


@dataclass
class Event:
    """One model call."""

    source: str
    client: str
    thread_id: str
    ts: float
    model: str
    effort: str
    usage: Usage
    tools: list[str] = field(default_factory=list)


@dataclass
class ThreadInfo:
    source: str
    thread_id: str
    title: str = ""
    fallback_title: str = ""
    cwd: str = ""
    branch: str = ""

    @property
    def display_title(self) -> str:
        return self.title or self.fallback_title or self.thread_id[:8]


@dataclass
class RateWindow:
    used_percent: float
    window_minutes: int
    resets_at: float


@dataclass
class CodexLimits:
    ts: float
    plan: str
    primary: RateWindow | None
    secondary: RateWindow | None
