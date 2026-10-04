"""Start on login, for Windows (Linux uses the .desktop file written by install.sh)."""

from __future__ import annotations

import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "TokenPanel"


def supported() -> bool:
    return sys.platform == "win32"


def command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    # Running from source: pythonw has no console window.
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return f'"{pythonw if os.path.exists(pythonw) else exe}" -m tokenpanel --autostart'


def _get() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, NAME)[0]
    except OSError:
        return None


def enabled() -> bool:
    return supported() and _get() is not None


def set_enabled(on: bool) -> None:
    if not supported():
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
    if current is not None and current != command():
        set_enabled(True)
