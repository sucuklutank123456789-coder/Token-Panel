from __future__ import annotations

import argparse
import codecs
import sys

from .model import DEFAULT_METRIC, METRICS
from .paths import default_claude_dirs, default_codex_dirs
from .store import RANGES, Store


# Stand-ins for characters a legacy console code page may lack.
_FALLBACK = {"—": "-", "…": "...", "‹": "<", "›": ">", "·": "|"}


def _fallback(err: UnicodeEncodeError) -> tuple[str, int]:
    return "".join(_FALLBACK.get(c, "?") for c in err.object[err.start:err.end]), err.end


codecs.register_error("tokenpanel", _fallback)


def console_encoding(code_page: int) -> str:
    """Python encoding for a Windows console code page; UTF-8 when there is none or it is unknown."""
    if not code_page:
        return "utf-8"
    try:
        return codecs.lookup(f"cp{code_page}").name
    except LookupError:
        return "utf-8"


def _windows_console() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if sys.stdout is None:
        # The .exe is a GUI program and starts without stdout; write to the console it was started from.
        if kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
        return
    if sys.stdout.isatty() or not hasattr(sys.stdout, "reconfigure"):
        return
    # Piped or redirected: the shell decodes the bytes with its console's code page (857 on Turkish Windows).
    code_page = kernel32.GetConsoleOutputCP()
    if not code_page and kernel32.AttachConsole(-1):
        code_page = kernel32.GetConsoleOutputCP()
    sys.stdout.reconfigure(encoding=console_encoding(code_page), errors="tokenpanel")


def main(argv: list[str] | None = None) -> int:
    _windows_console()
    ap = argparse.ArgumentParser(prog="tokenpanel", description="Tray panel for Claude Code and Codex token usage")
    ap.add_argument("--dump", action="store_true", help="Print the summary to the terminal instead of opening the panel")
    ap.add_argument("--range", choices=list(RANGES), default="all", help="Time range for --dump")
    ap.add_argument(
        "--metric",
        choices=list(METRICS),
        default=DEFAULT_METRIC,
        help="Metric for --dump: app=same as the official apps, io=input+output, new=+cache writes, "
        "raw=including cache reads",
    )
    ap.add_argument("--show", action="store_true", help="Also open the panel on startup")
    ap.add_argument("--claude-dir", action="append", help="Claude config directory (default ~/.claude)")
    ap.add_argument("--codex-dir", action="append", help="Codex directory (default ~/.codex)")
    if sys.platform == "win32":
        ap.add_argument("--wsl", action="store_true", help="Also read logs inside WSL distributions (for --dump)")
    ap.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)  # started on login
    args = ap.parse_args(argv)

    def make_store(include_wsl: bool = False) -> Store:
        return Store(
            claude_dirs=args.claude_dir or default_claude_dirs(include_wsl),
            codex_dirs=args.codex_dir or default_codex_dirs(include_wsl),
        )

    if args.dump:
        from .report import render

        store = make_store(getattr(args, "wsl", False))
        store.refresh()
        print(render(store.summarize(args.range, args.metric)))
        return 0

    from .ui import run

    # On Windows new tray icons start hidden in the overflow area, so opening the program shows the panel.
    show = args.show or (sys.platform == "win32" and not args.autostart)
    return run(make_store, show=show, self_test=args.self_test)


if __name__ == "__main__":
    sys.exit(main())
