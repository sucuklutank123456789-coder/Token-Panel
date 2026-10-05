# PyInstaller spec for the single-file Windows build:  pyinstaller packaging/windows/tokenpanel.spec
# Expects tokenpanel.ico next to this file:  python packaging/make_icons.py ico packaging/windows/tokenpanel.ico
import os

root = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

a = Analysis(
    [os.path.join(SPECPATH, "..", "launch.py")],
    pathex=[root],
    excludes=["tkinter", "unittest", "pydoc", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtWebEngineCore"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TokenPanel",
    icon=os.path.join(SPECPATH, "tokenpanel.ico"),
    console=False,
    upx=False,
)
