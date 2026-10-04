from __future__ import annotations

import argparse
import sys

from .store import RANGES, Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tokenpanel", description="Claude Code ve Codex token kullanım paneli")
    ap.add_argument("--dump", action="store_true", help="Panel açmadan özeti terminale yaz")
    ap.add_argument("--range", choices=list(RANGES), default="all", help="--dump için zaman aralığı")
    ap.add_argument("--show", action="store_true", help="Açılışta paneli de göster")
    ap.add_argument("--claude-dir", action="append", help="Claude yapılandırma dizini (varsayılan ~/.claude)")
    ap.add_argument("--codex-dir", help="Codex dizini (varsayılan ~/.codex)")
    args = ap.parse_args(argv)

    def make_store() -> Store:
        return Store(claude_dirs=args.claude_dir, codex_dir=args.codex_dir)

    if args.dump:
        from .report import render

        store = make_store()
        store.refresh()
        print(render(store.summarize(args.range)))
        return 0

    from .ui import run

    return run(make_store, show=args.show)


if __name__ == "__main__":
    sys.exit(main())
