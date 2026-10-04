"""
test_archive_limits.py — a zip container is refused before MarkItDown opens it.

MarkItDown's ZipConverter reads every member whole and recurses into nested
archives, and mammoth reads each .docx image whole, so an archive whose
member inflates to gigabytes exhausted memory inside md.convert(), before
markitdown_cleanup's limits ran. The archives here only DECLARE huge sizes:
zipfile reads members up to their declared size, so the declaration is what
the converters would act on.
"""
from __future__ import annotations

import io
import re
import sys
import types
import zipfile
from pathlib import Path

import archive_limits

HERE = Path(__file__).resolve().parent.parent


def _declare_size(raw: bytes, name: str, size: int) -> bytes:
    """Rewrite one member's uncompressed size in the central directory."""
    buf = bytearray(raw)
    sig = b"PK\x01\x02"
    pos = buf.find(sig)
    while pos != -1:
        n_len = int.from_bytes(buf[pos + 28:pos + 30], "little")
        if buf[pos + 46:pos + 46 + n_len].decode() == name:
            buf[pos + 24:pos + 28] = size.to_bytes(4, "little")
            return bytes(buf)
        pos = buf.find(sig, pos + 4)
    raise AssertionError(name)


def _zip(members: dict[str, bytes]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_STORED) as zf:
        for k, v in members.items():
            zf.writestr(k, v)
    return bio.getvalue()


def _bomb(path: Path, member: str = "word/media/image1.png") -> Path:
    raw = _zip({"[Content_Types].xml": b"<Types/>", member: b"x" * 64})
    path.write_bytes(_declare_size(raw, member, 0xF0000000))
    return path


def test_member_declaring_gigabytes_is_refused(tmp_path):
    msg = archive_limits.refusal(_bomb(tmp_path / "report.docx"))
    assert msg is not None
    assert msg.startswith(
        "refused: archive member 'word/media/image1.png' inflates to 4,026,531,840 bytes")
    assert msg.endswith("; not converted")


def test_total_over_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_limits, "MAX_CONTAINER_TOTAL_BYTES", 1000)
    p = tmp_path / "deck.pptx"
    p.write_bytes(_zip({f"ppt/media/i{k}.png": b"y" * 400 for k in range(3)}))
    assert archive_limits.refusal(p) == \
        "refused: archive members inflate to more than 1,000 bytes; not converted"


def test_member_count_over_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_limits, "MAX_CONTAINER_MEMBERS", 10)
    p = tmp_path / "many.zip"
    p.write_bytes(_zip({f"f{k}.txt": b"z" for k in range(11)}))
    assert archive_limits.refusal(p) == \
        "refused: archive more than 10 members; not converted"


def test_nested_bomb_is_refused(tmp_path):
    inner = _declare_size(_zip({"big.bin": b"q" * 64}), "big.bin", 0xF0000000)
    p = tmp_path / "outer.zip"
    p.write_bytes(_zip({"inner.zip": inner}))
    msg = archive_limits.refusal(p)
    assert msg is not None
    assert msg.startswith("refused: archive member 'big.bin' inflates to")


def test_nesting_depth_is_refused(tmp_path):
    z = _zip({"leaf.txt": b"hi"})
    for k in range(3):
        z = _zip({f"level{k}.zip": z})
    p = tmp_path / "deep.zip"
    p.write_bytes(z)
    assert archive_limits.refusal(p) == \
        "refused: archive archives nested more than 2 deep; not converted"


def test_ordinary_files_pass(tmp_path):
    docx = tmp_path / "memo.docx"
    docx.write_bytes(_zip({"[Content_Types].xml": b"<Types/>",
                           "word/document.xml": b"<w:document/>",
                           "word/media/image1.png": b"\x89PNG" + b"\x00" * 100}))
    assert archive_limits.refusal(docx) is None
    one_level = tmp_path / "bundle.zip"
    one_level.write_bytes(_zip({"memo.docx": docx.read_bytes()}))
    assert archive_limits.refusal(one_level) is None
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + b"0" * 1000)
    assert archive_limits.refusal(pdf) is None


def test_markitdown_convert_refuses_before_convert(tmp_path, monkeypatch):
    fake = types.ModuleType("markitdown")

    class MarkItDown:
        def convert(self, _path):
            raise AssertionError("md.convert reached")

    fake.MarkItDown = MarkItDown
    monkeypatch.setitem(sys.modules, "markitdown", fake)
    monkeypatch.delitem(sys.modules, "markitdown_convert", raising=False)
    import markitdown_convert

    src = _bomb(tmp_path / "budget.xlsx", "xl/media/image1.png")
    ok, msg = markitdown_convert.convert_one(MarkItDown(), str(src), tmp_path / "out")
    assert ok is False
    assert msg.startswith("budget.xlsx: refused: archive member 'xl/media/image1.png'")
    assert not (tmp_path / "out").exists()


def test_markitdown_dropper_refuses_before_convert():
    """The dropper needs PySide6 to import, so its order is checked in the
    source: the refusal sits in convert_one, ahead of md.convert()."""
    src = (HERE / "markitdown_dropper.py").read_text(encoding="utf-8")
    body = re.search(r"\ndef convert_one\(.*?(?=\ndef |\nclass )", src, re.S).group(0)
    assert "import archive_limits" in src
    refusal = body.index("refused = archive_limits.refusal(src)")
    assert refusal < body.index("md.convert(")
    assert 'return False, f"{src.name}: {refused}"' in body
