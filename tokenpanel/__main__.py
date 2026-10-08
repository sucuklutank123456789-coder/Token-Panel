from __future__ import annotations

import argparse
import codecs
import os
import sys
from datetime import date

from .model import DEFAULT_METRIC, METRICS
from .paths import default_claude_dirs, default_codex_dirs, default_opencode_dirs
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


def _attach_console(kernel32) -> bool:
    """Attaches to the console of the shell that started us.

    The single-file .exe runs as two processes: a launcher that unpacks the program, then the program itself.
    The direct parent is that windowless launcher, so look further up the process tree.
    """
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    parents: dict[int, int] = {}
    if snapshot and snapshot != wintypes.HANDLE(-1).value:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(wintypes.HANDLE(snapshot), ctypes.byref(entry))
        while ok:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = kernel32.Process32NextW(wintypes.HANDLE(snapshot), ctypes.byref(entry))
        kernel32.CloseHandle(wintypes.HANDLE(snapshot))
    pid = os.getpid()
    for _ in range(4):
        pid = parents.get(pid, 0)
        if not pid:
            break
        if kernel32.AttachConsole(pid):
            return True
    return bool(kernel32.AttachConsole(-1))


def _windows_console() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if sys.stdout is None:
        # The .exe is a GUI program and starts without stdout; write to the console it was started from.
        if _attach_console(kernel32):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
        return
    if sys.stdout.isatty() or not hasattr(sys.stdout, "reconfigure"):
        return
    # Piped or redirected: the shell decodes the bytes with its console's code page (857 on Turkish Windows).
    code_page = kernel32.GetConsoleOutputCP()
    if not code_page and _attach_console(kernel32):
        code_page = kernel32.GetConsoleOutputCP()
    sys.stdout.reconfigure(encoding=console_encoding(code_page), errors="tokenpanel")


def main(argv: list[str] | None = None) -> int:
    _windows_console()
    ap = argparse.ArgumentParser(prog="tokenpanel", description="Tray panel for Claude Code and Codex token usage")
    ap.add_argument(
        "--dump", action="store_true", help="Print the summary to the terminal instead of opening the panel"
    )
    ap.add_argument("--range", choices=list(RANGES), default="all", help="Time range for --dump")
    ap.add_argument("--day", type=date.fromisoformat, help="One day for --dump instead of a range (YYYY-MM-DD)")
    ap.add_argument(
        "--metric",
        choices=list(METRICS),
        default=DEFAULT_METRIC,
        help="Metric for --dump: app=same as the official apps, app_nc=the official apps without cached input, "
        "io=input+output, new=+cache writes, raw=including cache reads",
    )
    ap.add_argument("--show", action="store_true", help="Also open the panel on startup")
    ap.add_argument("--claude-dir", action="append", help="Claude config directory (default ~/.claude)")
    ap.add_argument("--codex-dir", action="append", help="Codex directory (default ~/.codex)")
    ap.add_argument(
        "--opencode-dir", action="append", help="OpenCode data directory (default ~/.local/share/opencode)"
    )
    if sys.platform == "win32":
        ap.add_argument("--wsl", action="store_true", help="Also read logs inside WSL distributions (for --dump)")
    ap.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)  # started on login
    if argv is None:
        argv = sys.argv[1:]
    # Older macOS versions pass a process serial number to apps opened from the Finder.
    argv = [a for a in argv if not a.startswith("-psn_")]
    args = ap.parse_args(argv)

    def make_store(include_wsl: bool = False) -> Store:
        return Store(
            claude_dirs=args.claude_dir or default_claude_dirs(include_wsl),
            codex_dirs=args.codex_dir or default_codex_dirs(include_wsl),
            opencode_dirs=args.opencode_dir or default_opencode_dirs(include_wsl),
        )

    if args.dump:
        from .report import render

        store = make_store(getattr(args, "wsl", False))
        store.refresh()
        print(render(store.summarize(args.range, args.metric, day=args.day)))
        return 0

    from .ui import run

    # Opening the program by hand shows the panel: Windows hides new tray icons in the overflow area, and
    # on a MacBook the menu bar icon can sit behind the notch. Started on login it stays in the tray.
    show = args.show or (sys.platform in ("win32", "darwin") and not args.autostart)
    return run(make_store, show=show, self_test=args.self_test)


if __name__ == "__main__":
    sys.exit(main())
