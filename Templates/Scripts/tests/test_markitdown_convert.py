"""markitdown_convert: a conversion without the cleanup pass is refused.

The cleanup pass writes the pipeline's own frontmatter and tier. Without it
the converted body was written raw, so a document's own leading frontmatter
block -- classification: public included -- became the note's (review,
2026-10-04). The converters now refuse instead.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent


def _load_without_cleanup(monkeypatch):
    fake = types.ModuleType("markitdown")
    fake.MarkItDown = object
    monkeypatch.setitem(sys.modules, "markitdown", fake)
    monkeypatch.setitem(sys.modules, "markitdown_cleanup", None)   # import fails
    monkeypatch.delitem(sys.modules, "markitdown_convert", raising=False)
    return importlib.import_module("markitdown_convert")


class _Result:
    title = None
    text_content = "---\nclassification: public\n---\nbody from an outside document\n"


class _MD:
    def convert(self, path):
        return _Result()


def test_a_conversion_without_cleanup_is_refused(tmp_path, monkeypatch) -> None:
    mc = _load_without_cleanup(monkeypatch)
    assert mc.cleanup_clean is None
    src = tmp_path / "in.docx"
    src.write_bytes(b"x")
    out_dir = tmp_path / "Clippings"
    ok, msg = mc.convert_one(_MD(), str(src), out_dir)
    assert ok is False
    assert "not converted: markitdown_cleanup is unavailable" in msg
    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_the_dropper_refuses_the_same_way() -> None:
    """The dropper needs PySide6 to import; its refusal is checked in source."""
    src = (SCRIPTS / "markitdown_dropper.py").read_text(encoding="utf-8")
    body = src[src.index("def convert_one"):]
    refuse = body.index("if cleanup_clean is None:")
    assert "not converted: markitdown_cleanup is unavailable" in body[refuse:refuse + 300]
    assert refuse < body.index("out.write_text(")
