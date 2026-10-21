"""
test_markitdown_dropper_bounds.py -- a dropped file cannot hold the dropper
without bound (finding 5278110).

The dropper ran MarkItDown in its own worker thread with no size or time
limit, so a hostile PDF could keep pdfminer busy indefinitely; a thread
cannot be stopped from outside. Now every file over MAX_SOURCE_BYTES is
refused and each conversion runs in a long-lived child process that is
killed, and replaced on the next file, when one conversion overruns.

The dropper needs PySide6 and markitdown to import, neither of which is in
the test venv, so both are stubbed. The child runs the shipped request loop
with MarkItDown swapped for a stand-in converter.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent

ENGINE = "from markitdown import MarkItDown\nmd = MarkItDown()\n"
FAKE_ENGINE = (
    "class _MD:\n"
    "    def convert(self, path):\n"
    "        p = os.path.basename(path)\n"
    "        import time\n"
    "        if 'slow' in p:\n"
    "            time.sleep(30)\n"
    "        if 'boom' in p:\n"
    "            raise ValueError('pdf is damaged')\n"
    "        if 'die' in p:\n"
    "            os._exit(3)\n"
    "        print('library noise on stdout')\n"
    "        os.write(1, b'more noise on fd 1\\n')\n"
    "        class R:\n"
    "            text_content = 'caf\\u00e9 ' + os.path.basename(p)\n"
    "            title = 'T'\n"
    "        return R()\n"
    "md = _MD()\n"
)


class _Anything:
    """Stands in for every Qt class and constant the module touches at import."""

    def __init__(self, *a, **k) -> None:
        pass

    def __getattr__(self, name):
        return _Anything()

    def __call__(self, *a, **k):
        return _Anything()


@pytest.fixture
def dropper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    core = types.ModuleType("PySide6.QtCore")
    core.Qt = _Anything()
    core.QObject = _Anything
    core.Signal = lambda *a, **k: None
    gui = types.ModuleType("PySide6.QtGui")
    gui.QFont = _Anything
    widgets = types.ModuleType("PySide6.QtWidgets")
    for name in ("QApplication", "QCheckBox", "QFileDialog", "QHBoxLayout",
                 "QLabel", "QMainWindow", "QMessageBox", "QPushButton",
                 "QTextEdit", "QVBoxLayout", "QWidget"):
        setattr(widgets, name, _Anything)
    pkg = types.ModuleType("PySide6")
    mid = types.ModuleType("markitdown")
    mid.MarkItDown = _Anything
    for name, mod in (("PySide6", pkg), ("PySide6.QtCore", core),
                      ("PySide6.QtGui", gui), ("PySide6.QtWidgets", widgets),
                      ("markitdown", mid)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setenv("OBSIDIAN_VAULT", str(HERE.parent.parent))
    monkeypatch.delitem(sys.modules, "markitdown_dropper", raising=False)
    mod = importlib.import_module("markitdown_dropper")
    assert ENGINE in mod._CHILD_CODE
    monkeypatch.setattr(mod, "_CHILD_CODE", mod._CHILD_CODE.replace(ENGINE, FAKE_ENGINE))
    yield mod
    sys.modules.pop("markitdown_dropper", None)


@pytest.fixture
def engine(dropper):
    md = dropper.BoundedMarkItDown(timeout=10)
    yield md
    md._stop()


def _src(tmp_path: Path, name: str = "report.pdf", size: int = 10) -> Path:
    p = tmp_path / "in" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(b"%PDF" + b"x" * (size - 4))
    return p


def test_a_file_over_the_size_cap_is_refused_before_conversion(
        dropper, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(dropper, "MAX_SOURCE_BYTES", 100)

    class _Never:
        def convert(self, path):
            raise AssertionError("convert() ran on an oversized file")

    ok, msg = dropper.convert_one(_Never(), str(_src(tmp_path, size=101)),
                                  tmp_path / "out")
    assert not ok
    assert msg == ("report.pdf: refused: 101 bytes is over the "
                   "100-byte limit; not converted")


def test_the_child_result_is_returned_despite_stdout_noise(
        engine, tmp_path: Path) -> None:
    first = engine.convert(str(_src(tmp_path)))
    pid = engine._proc.pid
    second = engine.convert(str(_src(tmp_path, "other.pdf")))
    assert (first.text_content, first.title) == ("caf\u00e9 report.pdf", "T")
    assert second.text_content == "caf\u00e9 other.pdf"
    assert engine._proc.pid == pid, "one child serves every file"


def test_a_conversion_that_runs_too_long_is_stopped_and_the_next_runs(
        dropper, engine, tmp_path: Path) -> None:
    engine.timeout = 1
    ok, msg = dropper.convert_one(engine, str(_src(tmp_path, "slow.pdf")),
                                  tmp_path / "out")
    assert not ok
    assert msg == ("slow.pdf: ConversionFailed: conversion took longer "
                   "than 1 s and was stopped")
    assert engine._proc is None, "the overrunning child is killed"
    assert not list(tmp_path.rglob("*.md")), "nothing written for a stopped conversion"

    engine.timeout = 10
    assert engine.convert(str(_src(tmp_path))).text_content == "caf\u00e9 report.pdf"


def test_a_converter_error_is_reported_and_the_child_kept(
        dropper, engine, tmp_path: Path) -> None:
    with pytest.raises(dropper.ConversionFailed, match="^ValueError: pdf is damaged$"):
        engine.convert(str(_src(tmp_path, "boom.pdf")))
    pid = engine._proc.pid
    engine.convert(str(_src(tmp_path)))
    assert engine._proc.pid == pid


def test_a_child_that_dies_is_reported_and_replaced(
        dropper, engine, tmp_path: Path) -> None:
    with pytest.raises(dropper.ConversionFailed, match="^the converter process exited$"):
        engine.convert(str(_src(tmp_path, "die.pdf")))
    assert engine.convert(str(_src(tmp_path))).text_content == "caf\u00e9 report.pdf"


def test_the_child_runs_isolated(dropper) -> None:
    compile(dropper._CHILD_CODE, "<child>", "exec")
    src = (HERE / "markitdown_dropper.py").read_text(encoding="utf-8")
    assert '[sys.executable, "-I", "-c", _CHILD_CODE]' in src
    assert "self.md = BoundedMarkItDown()" in src
