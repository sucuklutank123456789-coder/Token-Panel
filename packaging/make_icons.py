"""Renders data/tokenpanel.svg into app icons.

    python packaging/make_icons.py ico OUT.ico          Windows: multi-size, PNG-compressed .ico
    python packaging/make_icons.py iconset OUT.iconset  macOS: PNG set for `iconutil -c icns`
"""

from __future__ import annotations

import os
import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def png(renderer: QSvgRenderer, size: int) -> bytes:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p)
    p.end()
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(data)


def write_ico(renderer: QSvgRenderer, out: str) -> None:
    images = [png(renderer, s) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in zip(SIZES, images, strict=True):
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        blobs += data
    with open(out, "wb") as fh:
        fh.write(header + entries + blobs)


def write_iconset(renderer: QSvgRenderer, out: str) -> None:
    os.makedirs(out, exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
            with open(os.path.join(out, name), "wb") as fh:
                fh.write(png(renderer, size * scale))


def main(kind: str, out: str) -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    svg = os.path.join(root, "data", "tokenpanel.svg")
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    renderer = QSvgRenderer(svg)
    if not renderer.isValid():
        sys.exit(f"cannot read {svg}")
    {"ico": write_ico, "iconset": write_iconset}[kind](renderer, out)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("ico", "iconset"):
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
