"""Start on login, for Windows and macOS (Linux uses the .desktop file written by install.sh).

Windows: a value under the HKCU Run key. macOS: a LaunchAgent in ~/Library/LaunchAgents.
"""

from __future__ import annotations

import os
import plistlib
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "TokenPanel"
AGENT_LABEL = "com.github.tokenpanel"


def supported() -> bool:
    return sys.platform in ("win32", "darwin")


def label() -> str:
    return "Start with Windows" if sys.platform == "win32" else "Start at login"


def arguments() -> list[str]:
    """The command that starts this copy of the program, silently."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--autostart"]
    exe = sys.executable
    if sys.platform == "win32":
        # Running from source: pythonw has no console window.
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(pythonw):
            exe = pythonw
    return [exe, "-m", "tokenpanel", "--autostart"]


def command() -> str:
    first, *rest = arguments()
    return " ".join([f'"{first}"', *rest])


def agent_path() -> str:
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", AGENT_LABEL + ".plist")


def _get() -> str | None:
    """The registered start command, or None when autostart is off."""
    if sys.platform == "darwin":
        try:
            with open(agent_path(), "rb") as fh:
                return " ".join(plistlib.load(fh).get("ProgramArguments", []))
        except (OSError, ValueError):
            return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, NAME)[0]
    except OSError:
        return None


def _current() -> str:
    return " ".join(arguments()) if sys.platform == "darwin" else command()


def enabled() -> bool:
    return supported() and _get() is not None


def set_enabled(on: bool) -> None:
    if not supported():
        return
    if sys.platform == "darwin":
        path = agent_path()
        if on:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            agent = {"Label": AGENT_LABEL, "ProgramArguments": arguments(), "RunAtLoad": True,
                     "ProcessType": "Interactive"}
            with open(path, "wb") as fh:
                plistlib.dump(agent, fh)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(k, NAME)
            except OSError:
                pass


def sync() -> None:
    """Points the entry at this copy of the program if it was moved since autostart was turned on."""
    if not supported() or not getattr(sys, "frozen", False):
        return
    current = _get()
    if current is not None and current != _current():
        set_enabled(True)
