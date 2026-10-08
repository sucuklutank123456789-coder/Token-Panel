"""Reads zstd-compressed logs: Codex compresses session files older than 7 days to .jsonl.zst.

Python 3.14 reads zstd itself (compression.zstd); older versions need the zstandard package.
"""

from __future__ import annotations


def _decoder():
    try:
        from compression import zstd  # Python 3.14+

        return lambda fh: zstd.ZstdFile(fh).read()
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError:
        return None
    # The frames Codex writes may not record their size, so decompress as a stream.
    return lambda fh: zstandard.ZstdDecompressor().stream_reader(fh, read_across_frames=True).read()


_read = _decoder()


def available() -> bool:
    return _read is not None


def read_zst(path: str) -> bytes | None:
    """The decompressed file, or None when it can't be read (no decoder, damaged, or gone)."""
    if _read is None:
        return None
    try:
        with open(path, "rb") as fh:
            return _read(fh)
    except Exception:  # zstandard.ZstdError, compression.zstd.ZstdError, OSError
        return None
