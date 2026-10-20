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


# ---- converted text is sanitized (F-20261020 5278628) -----------------------

class _EvilResult:
    title = None
    text_content = ("# Doc\n\n![x](https://attacker.example/p.png?d=1)\n\n"
                    '<img src="https://attacker.example/t.gif">\n\n'
                    "[o](obsidian://open?file=x) [w](https://example.com/ok)\n\n"
                    "![chart](data:image/png;base64,aGVsbG8=)\n\n"
                    "```mermaid\nA@{ img: \"https://attacker.example/i.png\" }\n```\n")


class _EvilMD:
    def convert(self, path):
        return _EvilResult()


def _load_with_cleanup(monkeypatch):
    fake = types.ModuleType("markitdown")
    fake.MarkItDown = object
    monkeypatch.setitem(sys.modules, "markitdown", fake)
    monkeypatch.delitem(sys.modules, "markitdown_convert", raising=False)
    return importlib.import_module("markitdown_convert")


def test_a_documents_remote_images_html_and_schemes_are_inert(tmp_path, monkeypatch) -> None:
    mc = _load_with_cleanup(monkeypatch)
    assert mc.cleanup_clean is not None
    src = tmp_path / "in.pdf"
    src.write_bytes(b"x")
    out_dir = tmp_path / "Clippings"
    ok, msg = mc.convert_one(_EvilMD(), str(src), out_dir)
    assert ok is True, msg
    [note] = list(out_dir.iterdir())
    text = note.read_text(encoding="utf-8")
    z = "​"
    assert "!" + z + "[x](https://attacker.example/p.png?d=1)" in text
    assert "<" + z + "img" in text
    assert "[o](obsidian" + z + "://open?file=x)" in text
    assert "```" + z + "mermaid" in text
    # the image cleanup extracted into the vault keeps its embed, and the
    # web link keeps working
    assert "\n![[in-img-1.png]]" in text
    assert (tmp_path / "Z_attachments" / "in-img-1.png").exists()
    assert "[w](https://example.com/ok)" in text


def test_the_dropper_sanitizes_before_it_writes() -> None:
    src = (SCRIPTS / "markitdown_dropper.py").read_text(encoding="utf-8")
    body = src[src.index("def convert_one"):]
    sanitize = body.index("output_sanitizer.sanitize_keeping_embeds(body, _embed_names(summary))")
    assert sanitize < body.index("out.write_text(templater_guard.neutralize(body)")
