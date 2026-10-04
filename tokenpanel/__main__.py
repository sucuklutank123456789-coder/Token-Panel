from __future__ import annotations

import argparse
import sys

from .model import DEFAULT_METRIC, METRICS
from .store import RANGES, Store


def main(argv: list[str] | None = None) -> int:
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
    ap.add_argument("--codex-dir", help="Codex directory (default ~/.codex)")
    args = ap.parse_args(argv)

    def make_store() -> Store:
        return Store(claude_dirs=args.claude_dir, codex_dir=args.codex_dir)

    if args.dump:
        from .report import render

        store = make_store()
        store.refresh()
        print(render(store.summarize(args.range, args.metric)))
        return 0

    from .ui import run

    return run(make_store, show=args.show)


if __name__ == "__main__":
    sys.exit(main())
