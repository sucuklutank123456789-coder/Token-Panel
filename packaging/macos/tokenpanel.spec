# PyInstaller spec for the macOS app bundle:  pyinstaller packaging/macos/tokenpanel.spec
# Expects tokenpanel.icns next to this file:
#   python packaging/make_icons.py iconset tokenpanel.iconset
#   iconutil -c icns tokenpanel.iconset -o packaging/macos/tokenpanel.icns
import os
import tomllib

root = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
    version = tomllib.load(fh)["project"]["version"]

a = Analysis(
    [os.path.join(SPECPATH, "..", "launch.py")],
    pathex=[root],
    excludes=["tkinter", "unittest", "pydoc", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtWebEngineCore"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TokenPanel",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="TokenPanel", upx=False)
app = BUNDLE(
    coll,
    name="TokenPanel.app",
    icon=os.path.join(SPECPATH, "tokenpanel.icns"),
    bundle_identifier="com.github.tokenpanel",
    version=version,
    info_plist={
        "CFBundleDisplayName": "Token Panel",
        "LSUIElement": True,  # menu bar only, no Dock icon
        "NSHighResolutionCapable": True,
    },
)
