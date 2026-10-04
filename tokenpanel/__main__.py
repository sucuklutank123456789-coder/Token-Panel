from __future__ import annotations

import argparse
import sys

from .model import DEFAULT_METRIC, METRICS
from .paths import default_claude_dirs, default_codex_dirs
from .store import RANGES, Store


def _windows_console() -> None:
    # The Windows .exe is a GUI program and starts without stdout; reuse the console it was started from.
    if sys.platform != "win32":
        return
    if sys.stdout is not None:
        # Redirected output would use the ANSI code page, which can't hold every thread title.
        if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        return
    import ctypes

    if ctypes.windll.kernel32.AttachConsole(-1):
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stderr = sys.stdout


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
