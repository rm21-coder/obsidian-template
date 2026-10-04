"""
archive_limits.py — refuse a zip container before MarkItDown opens it.

markitdown_dropper.py and markitdown_convert.py hand outsider files (emailed
attachments) to MarkItDown. For a zip container -- .zip, and the Office
formats .docx/.xlsx/.pptx, which are zips -- MarkItDown's converters read
members whole: its ZipConverter reads every member into memory and recurses
into nested archives, and mammoth reads each .docx image whole. A small file
whose member inflates to gigabytes exhausts memory inside md.convert(), before
markitdown_cleanup's own extraction limits ever run.

So the converters call refusal() first. It reads only the archive's central
directory (declared sizes) plus four bytes of each member to spot a nested
zip, which it checks the same way. Declared sizes are enough: Python's
zipfile stops decompressing a member at its declared size, and MarkItDown,
python-docx, python-pptx, openpyxl and mammoth all read through zipfile.

What this does NOT cover: non-zip formats (PDF streams, images, audio), and
memory a converter needs beyond the raw bytes (e.g. a decoded bitmap). The
limits are deliberately looser than markitdown_cleanup's per-image ones: a
real deck can carry hundreds of parts and an embedded video.

Stdlib only, imported unconditionally by both converters.
"""
from __future__ import annotations

import zipfile
import zlib
from pathlib import Path

MAX_CONTAINER_MEMBERS = 5000                     # all levels together
MAX_CONTAINER_MEMBER_BYTES = 100 * 1024 * 1024   # one member, decompressed
MAX_CONTAINER_TOTAL_BYTES = 500 * 1024 * 1024    # all members, all levels
MAX_NESTING = 2                                  # zip inside zip inside zip

_ZIP_MAGIC = b"PK\x03\x04"


class _Refused(Exception):
    pass


def _check(zf: zipfile.ZipFile, depth: int, tally: list[int]) -> None:
    infos = zf.infolist()
    tally[0] += len(infos)
    if tally[0] > MAX_CONTAINER_MEMBERS:
        raise _Refused(f"more than {MAX_CONTAINER_MEMBERS} members")
    for info in infos:
        if info.file_size > MAX_CONTAINER_MEMBER_BYTES:
            raise _Refused(
                f"member {info.filename!r} inflates to {info.file_size:,} bytes "
                f"(limit {MAX_CONTAINER_MEMBER_BYTES:,})")
        tally[1] += info.file_size
        if tally[1] > MAX_CONTAINER_TOTAL_BYTES:
            raise _Refused(
                f"members inflate to more than {MAX_CONTAINER_TOTAL_BYTES:,} bytes")
    for info in infos:
        if info.is_dir() or info.file_size < len(_ZIP_MAGIC):
            continue
        with zf.open(info) as f:
            if f.read(len(_ZIP_MAGIC)) != _ZIP_MAGIC:
                continue
        if depth >= MAX_NESTING:
            raise _Refused(f"archives nested more than {MAX_NESTING} deep")
        with zf.open(info) as f, zipfile.ZipFile(f) as inner:
            _check(inner, depth + 1, tally)


def refusal(path: Path) -> str | None:
    """None if `path` is not a zip container or is within the limits;
    otherwise a one-line reason starting "refused: "."""
    try:
        if not zipfile.is_zipfile(path):
            return None
        with zipfile.ZipFile(path) as zf:
            _check(zf, 0, [0, 0])
    except _Refused as e:
        return f"refused: archive {e}; not converted"
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError,
            zlib.error, RuntimeError, NotImplementedError, ValueError) as e:
        return f"refused: unreadable archive ({type(e).__name__}); not converted"
    return None
