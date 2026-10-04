"""Where Claude Code and Codex keep their logs, on Linux and Windows."""

from __future__ import annotations

import glob
import os
import sys


def _unique(paths: list[str]) -> list[str]:
    seen, out = set(), []
    for p in paths:
        p = os.path.realpath(p)
        key = os.path.normcase(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def wsl_distros() -> list[str]:
    """Names of the WSL distributions registered for the current Windows user."""
    if sys.platform != "win32":
        return []
    import winreg

    names = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Lxss")
    except OSError:
        return []
    with root:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, sub) as k:
                    names.append(winreg.QueryValueEx(k, "DistributionName")[0])
            except OSError:
                continue
    return names


def wsl_homes() -> list[str]:
    """Home directories inside WSL, reached over \\\\wsl.localhost. Reading them starts the distribution."""
    homes = []
    for name in wsl_distros():
        base = rf"\\wsl.localhost\{name}"
        homes += glob.glob(os.path.join(base, "home", "*"))
        homes.append(os.path.join(base, "root"))
    return homes


def default_claude_dirs(include_wsl: bool = False) -> list[str]:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    bases = [p for p in env.split(",") if p] if env else []
    home = os.path.expanduser("~")
    bases += [os.path.join(home, ".claude"), os.path.join(home, ".config", "claude")]
    if include_wsl:
        for h in wsl_homes():
            bases += [os.path.join(h, ".claude"), os.path.join(h, ".config", "claude")]
    return _unique(bases)


def default_codex_dirs(include_wsl: bool = False) -> list[str]:
    dirs = [os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")]
    if include_wsl:
        dirs += [os.path.join(h, ".codex") for h in wsl_homes()]
    return _unique(dirs)
