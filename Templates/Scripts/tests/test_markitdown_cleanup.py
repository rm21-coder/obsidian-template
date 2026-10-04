"""
test_markitdown_cleanup.py — the post-conversion pass treats a converted
document as outsider text.

A converted file is usually an emailed attachment dropped on the dropper, so
everything in it (its name, its text, its own frontmatter, its embedded media)
is chosen by whoever sent it. These tests pin the limits that keep such a file
from filling the vault (M-DASH #76, #81, #88), the quoting that keeps its name
from breaking the note's YAML (#79), and the rule that its own frontmatter
never becomes the note's (so it cannot declare its own classification).
"""
from __future__ import annotations

import base64
import zipfile
from pathlib import Path

import pytest
import yaml

import classification_tier
import markitdown_cleanup as mc

STUB = "![pic](data:image/png;base64...)"
TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _office(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _inline(n: int, payload: bytes = TINY_PNG) -> str:
    b64 = base64.b64encode(payload).decode()
    return "\n\n".join(f"![i{k}](data:image/png;base64,{b64})" for k in range(n))


def _fm(text: str) -> dict:
    block = classification_tier.frontmatter(text)
    assert block is not None
    return yaml.safe_load(block)


# ─── #76 / #88: Office-archive image recovery is bounded ─────────────────────

def test_archive_member_over_size_cap_is_skipped(tmp_path):
    """A media member that inflates past the per-member cap is not written;
    its stub gets the placeholder. Before the fix the whole member was read
    into memory and written into Z_attachments."""
    bomb = b"\x00" * (mc.MAX_ARCHIVE_MEMBER_BYTES + 1) if hasattr(
        mc, "MAX_ARCHIVE_MEMBER_BYTES") else b"\x00" * (25 * 1024 * 1024 + 1)
    src = _office(tmp_path / "report.docx", {
        "word/media/image1.png": bomb,
        "word/media/image2.png": TINY_PNG,
    })
    att = tmp_path / "att"
    out, summary = mc.clean(f"{STUB}\n\n{STUB}\n", src, att)
    names = sorted(p.name for p in att.iterdir())
    assert names == ["report-source-img-2.png"]
    assert summary["images_skipped"] == 1
    assert summary["stubs_placeheld"] == 1
    assert mc.PLACEHOLDER_TEXT in out
    assert (att / "report-source-img-2.png").read_bytes() == TINY_PNG


def test_archive_total_cap_stops_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "MAX_ARCHIVE_TOTAL_BYTES", 10_000, raising=False)
    src = _office(tmp_path / "deck.pptx", {
        f"ppt/media/image{k}.png": b"\x01" * 6000 for k in (1, 2, 3)
    })
    att = tmp_path / "att"
    _out, summary = mc.clean(STUB * 3, src, att)
    written = list(att.iterdir())
    assert [p.name for p in written] == ["deck-source-img-1.png"]
    assert sum(p.stat().st_size for p in written) <= 10_000
    assert summary["images_skipped"] == 2


def test_archive_member_count_cap(tmp_path, monkeypatch):
    """The xlsx route from the finding: a cell holding the stub text is
    enough to make the pass extract every xl/media member."""
    monkeypatch.setattr(mc, "MAX_ARCHIVE_MEMBERS", 5, raising=False)
    src = _office(tmp_path / "budget.xlsx", {
        f"xl/media/image{k:02d}.png": TINY_PNG for k in range(8)
    })
    att = tmp_path / "att"
    _out, summary = mc.clean(STUB, src, att)
    assert len(list(att.iterdir())) == 5
    assert summary["images_skipped"] == 3


def test_copy_bounded_stops_on_a_lying_stream(tmp_path):
    """The declared file_size is the archive's own claim: the copy counts
    what actually arrives, stops one byte past the limit, and leaves no
    partial file behind."""
    class Endless:
        reads = 0

        def read(self, n):
            Endless.reads += 1
            return b"\x00" * n

    out = tmp_path / "x.png"
    assert mc._copy_bounded(Endless(), out, 3 * 1024 * 1024) is None
    assert not out.exists()
    assert Endless.reads <= 4


# ─── #81: inline data: images are bounded ────────────────────────────────────

def test_inline_image_count_cap(tmp_path):
    n = 250
    att = tmp_path / "att"
    out, summary = mc.clean(_inline(n), tmp_path / "flood.md", att)
    cap = getattr(mc, "MAX_INLINE_IMAGES", 200)
    assert len(list(att.iterdir())) == cap
    assert len(summary["images_extracted"]) == cap
    assert summary["images_skipped"] == n - cap
    assert out.count(mc.OMITTED_TEXT) == n - cap


def test_inline_total_bytes_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "MAX_INLINE_BYTES", 100, raising=False)
    att = tmp_path / "att"
    out, summary = mc.clean(_inline(2, b"\x89PNG" + b"\x00" * 76),
                            tmp_path / "big.md", att)
    assert len(list(att.iterdir())) == 1
    assert summary["images_skipped"] == 1
    assert out.count(mc.OMITTED_TEXT) == 1


def test_ordinary_images_still_extracted(tmp_path):
    src = _office(tmp_path / "memo.docx", {
        "word/media/image1.png": TINY_PNG,
        "word/media/image2.jpeg": b"\xff\xd8\xff" + b"\x00" * 10,
    })
    att = tmp_path / "att"
    body = _inline(2) + f"\n\n{STUB}\n\n{STUB}\n"
    out, summary = mc.clean(body, src, att)
    assert summary["images_skipped"] == 0
    assert len(summary["images_extracted"]) == 2
    assert summary["stubs_replaced"] == 2
    assert "![[memo-img-1.png]]" in out
    assert "![[memo-source-img-2.jpg]]" in out
    assert (att / "memo-img-1.png").read_bytes() == TINY_PNG
    assert (att / "memo-source-img-1.png").read_bytes() == TINY_PNG


# ─── #79: title and source_file are valid YAML ───────────────────────────────

@pytest.mark.parametrize("name", [
    "[DRAFT] FY27 Budget.docx",
    "Q3: plan.docx",
    "*star.docx",
    "& co.docx",
    "Report #3.docx",
    'He said "yes".docx',
    "true.docx",
])
def test_frontmatter_quotes_outside_filename(tmp_path, name):
    out, _ = mc.clean("Body text\n", tmp_path / name, tmp_path / "att")
    fm = _fm(out)
    assert fm["source_file"] == name
    assert fm["title"] == Path(name).stem
    assert fm["classification"] == "internal-use-only"
    assert classification_tier.effective(out) == ("internal-use-only", [])


def test_frontmatter_strips_line_breaks_from_filename(tmp_path):
    name = "a classification: public\x85b.docx"
    out, _ = mc.clean("Body\n", tmp_path / name, tmp_path / "att")
    assert classification_tier.declared(out) == ["internal-use-only"]
    assert _fm(out)["source_file"] == "a classification: public b.docx"


def test_ordinary_title_unchanged(tmp_path):
    out, summary = mc.clean("Hello\n", tmp_path / "weekly_status-notes.docx",
                            tmp_path / "att")
    assert summary["frontmatter_added"] is True
    fm = _fm(out)
    assert fm["title"] == "weekly status notes"
    assert fm["source"] == "markitdown"
    assert fm["tags"] == []
    assert out.endswith("Hello\n")


# ─── The document's own frontmatter never becomes the note's ─────────────────

def test_document_frontmatter_cannot_set_classification(tmp_path):
    doc = (
        "---\n"
        "title: Totally Harmless\n"
        "classification: public\n"
        "tags: [export-me]\n"
        "---\n\n"
        "Quarterly numbers.\n"
    )
    out, summary = mc.clean(doc, tmp_path / "numbers.docx", tmp_path / "att")
    assert classification_tier.effective(out) == ("internal-use-only", [])
    assert classification_tier.declared(out) == ["internal-use-only"]
    fm = _fm(out)
    assert fm["title"] == "numbers"
    assert fm["tags"] == []
    assert summary["frontmatter_added"] is True
    assert summary["source_frontmatter"] is True
    # Kept visibly, as inert text in the body.
    assert "Frontmatter from the source document (kept as text, not applied):" in out
    assert "```yaml\n---\ntitle: Totally Harmless\nclassification: public\n" in out
    assert out.rstrip().endswith("Quarterly numbers.")


def test_document_frontmatter_with_bom_is_fenced(tmp_path):
    doc = "﻿---\nclassification: public\n---\nBody\n"
    out, summary = mc.clean(doc, tmp_path / "bom.md", tmp_path / "att")
    assert summary["source_frontmatter"] is True
    assert classification_tier.declared(out) == ["internal-use-only"]


def test_document_frontmatter_cannot_break_out_of_its_fence(tmp_path):
    doc = "---\nnote: ```\n````\nclassification: public\n---\nBody\n"
    out, _ = mc.clean(doc, tmp_path / "fence.md", tmp_path / "att")
    assert "`````yaml\n" in out
    assert classification_tier.declared(out) == ["internal-use-only"]


def test_cli_keeps_an_existing_notes_frontmatter(tmp_path):
    """Re-cleaning a note already in the vault keeps its own block."""
    note = "---\ntitle: Mine\nclassification: confidential\n---\n\nText\n"
    out, summary = mc.clean(note, tmp_path / "Mine.md", tmp_path / "att",
                            keep_frontmatter=True)
    assert out.startswith(note.split("\n\n")[0])
    assert summary["frontmatter_added"] is False
    assert summary["source_frontmatter"] is False
