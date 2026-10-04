"""Ortak veri tipleri ve etiketler."""

from __future__ import annotations

from dataclasses import dataclass, field

SOURCE_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
}

# Claude Code: her satırdaki "entrypoint" alanı.
CLAUDE_CLIENTS = {
    "cli": "CLI",
    "claude-desktop": "Desktop",
    "claude-vscode": "VS Code",
    "sdk-ts": "ACP (Zed) / SDK",
    "sdk-py": "SDK (Python)",
    "remote_desktop": "Uzak oturum",
}

# Codex: session_meta içindeki "originator" alanı.
CODEX_CLIENTS = {
    "codex-tui": "CLI",
    "codex_cli_rs": "CLI",
    "codex_exec": "CLI (exec)",
    "Codex Desktop": "Desktop",
    "codex_vscode": "VS Code",
    "zed": "ACP (Zed)",
}


# Panelde gösterilen "token" sayısının tanımı.
METRICS = {
    "app": "Claude uygulamasıyla aynı",
    "io": "Girdi + çıktı",
    "new": "Girdi + çıktı + önbelleğe yazma",
    "raw": "Ham (önbellekten okunan dahil)",
}
DEFAULT_METRIC = "app"


def client_label(source: str, client: str) -> str:
    table = CLAUDE_CLIENTS if source == "claude" else CODEX_CLIENTS
    return table.get(client, client or "Bilinmiyor")


@dataclass
class Usage:
    """Tek bir API çağrısının (veya toplamın) token dökümü.

    total = input + cache_read + cache_write + output.
    reasoning, output'un içinde yer alan düşünme token'larıdır (toplama ayrıca eklenmez).
    """

    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0
    # Uygulamaların kendi gösterdiği girdi + çıktı.
    # Claude: istatistik ekranı aynı yanıtın log'a bölünerek yazılan her satırını ayrı sayar.
    # Codex: kendi sayacı (token_count) sohbet sıkıştırma çağrılarını saymaz.
    app_io: float = 0

    @property
    def total(self) -> int:
        return self.input + self.cache_read + self.cache_write + self.output

    def value(self, metric: str) -> float:
        """Seçilen ölçüye göre değer.

        io : önbellek hariç girdi + çıktı, her model çağrısı bir kez (gerçek harcama)
        new: io + önbelleğe yazılan girdi
        raw: her şey; önbellekten tekrar tekrar okunan bağlam da dahil
        app: Claude uygulamasının gösterdiği "Total tokens" ile aynı yöntem
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
    """Bir model çağrısı."""

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
