"""markitdown_convert: the CLI twin of the dropper gets the same bounds.

Finding 5278110 bounded the dropper's MarkItDown call by size and time; the
review's follow-up noted markitdown_convert.py (the Windows "Send To" path)
still called md.convert() with no bound. It now uses the shared
markitdown_bounded engine.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import markitdown_bounded  # noqa: E402
import markitdown_convert  # noqa: E402


def _src(tmp_path: Path, size: int) -> Path:
    p = tmp_path / "in" / "report.pdf"
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(b"%PDF" + b"x" * (size - 4))
    return p


class _Never:
    def convert(self, path):
        raise AssertionError("convert() ran on an oversized file")


def test_an_oversized_file_is_refused_before_conversion(tmp_path, monkeypatch):
    monkeypatch.setattr(markitdown_bounded, "MAX_SOURCE_BYTES", 100)
    ok, msg = markitdown_convert.convert_one(_Never(), str(_src(tmp_path, 101)),
                                             tmp_path / "out")
    assert not ok
    assert msg == ("report.pdf: 101 bytes is over the 100-byte limit; "
                   "not converted")
    assert not list(tmp_path.rglob("*.md"))


def test_a_stopped_conversion_writes_nothing(tmp_path):
    class _Slow:
        def convert(self, path):
            raise markitdown_bounded.ConversionFailed(
                "conversion took longer than 1 s and was stopped")

    ok, msg = markitdown_convert.convert_one(_Slow(), str(_src(tmp_path, 10)),
                                             tmp_path / "out")
    assert not ok
    assert msg == ("report.pdf: ConversionFailed: conversion took longer "
                   "than 1 s and was stopped")
    assert not list(tmp_path.rglob("*.md"))


def test_main_uses_the_bounded_engine():
    src = (HERE / "markitdown_convert.py").read_text(encoding="utf-8")
    assert "md = markitdown_bounded.BoundedMarkItDown()" in src
    assert "MarkItDown()" not in src.replace("BoundedMarkItDown()", "")


def test_size_refusal_reports_an_unreadable_path(tmp_path):
    assert markitdown_bounded.size_refusal(tmp_path / "missing.pdf").startswith(
        "cannot read its size:")
    assert markitdown_bounded.size_refusal(_src(tmp_path, 10)) is None
