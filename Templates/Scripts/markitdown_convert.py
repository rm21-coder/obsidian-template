#!/usr/bin/env python3
"""markitdown_convert.py — headless file->Markdown conversion into the vault.

The CLI counterpart of markitdown_dropper.py (a PySide6 GUI). Converts each
input file with MarkItDown, runs the shared markitdown_cleanup pass (image
extraction, bullet/heading normalization, frontmatter), and writes
<vault>/Clippings/<name>.md; extracted images go to <vault>/Z_attachments/.

Used by the Windows "Send To" shortcut (windows/Install-SendTo.ps1) so you can
right-click a .docx/.pptx/.pdf/... -> Send to -> convert it into the vault.
Also runs directly:

    markitdown_convert.py FILE [FILE ...] [--out DIR]

Vault is $OBSIDIAN_VAULT or ~/Obsidian.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from markitdown import MarkItDown

try:
    from markitdown_cleanup import clean as cleanup_clean
except Exception:  # cleanup is best-effort; convert still works without it
    cleanup_clean = None

VAULT = Path(os.environ.get("OBSIDIAN_VAULT", str(Path.home() / "Obsidian"))).expanduser()
DEFAULT_OUT = VAULT / "Clippings"


def _unique(dest_dir: Path, stem: str) -> Path:
    out = dest_dir / f"{stem}.md"
    if not out.exists():
        return out
    return dest_dir / f"{stem}-{datetime.now().strftime('%Y-%m-%d-%H%M%S')}.md"


def convert_one(md: MarkItDown, src_path: str, dest_dir: Path) -> tuple[bool, str]:
    src = Path(src_path)
    if not src.exists():
        return False, f"not found: {src}"
    if src.is_dir():
        return False, f"skipped folder: {src.name}"

    out = _unique(dest_dir, src.stem)
    try:
        result = md.convert(str(src))
    except Exception as e:
        return False, f"{src.name}: {type(e).__name__}: {e}"

    text = getattr(result, "text_content", None) or getattr(result, "markdown", None) or ""
    title = getattr(result, "title", None)
    body = f"# {title}\n\n{text}" if title else text

    # Shared cleanup pass; attachments live in <vault>/Z_attachments (sibling of
    # the Clippings destination), matching markitdown_dropper.py.
    if cleanup_clean is not None:
        try:
            body, _summary = cleanup_clean(body, src, dest_dir.parent / "Z_attachments")
        except Exception as e:
            return False, f"{src.name}: cleanup failed: {type(e).__name__}: {e}"

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
    except OSError as e:
        return False, f"{src.name}: write failed: {e}"

    return True, f"{src.name} -> {out}"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Convert files to Markdown in the Obsidian vault.")
    p.add_argument("files", nargs="+", help="file(s) to convert")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help=f"output directory (default: {DEFAULT_OUT})")
    args = p.parse_args(argv)

    dest = Path(os.path.expanduser(args.out)).resolve()
    md = MarkItDown()
    rc = 0
    for f in args.files:
        ok, msg = convert_one(md, f, dest)
        print(("ok   " if ok else "FAIL ") + msg)
        if not ok:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
