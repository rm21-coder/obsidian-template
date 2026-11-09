"""No converted document leaves the machine (F36, v2.6 assessment, scan
01SybsfjWL2).

markitdown[all] registers an AudioConverter whose transcription calls
speech_recognition's recognize_google: the whole recording is uploaded to
Google over plain http://. The converter child now removes that converter
before converting anything (which also covers audio inside a zip), and
refuses every conversion if it cannot; the callers refuse audio and video by
name with a reason. No MarkItDown is needed here: the child's setup code is
run in a separate interpreter against a stand-in package.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import markitdown_bounded  # noqa: E402
import markitdown_convert  # noqa: E402


# A stand-in markitdown package, written to disk and imported by a separate
# `python -I` like the real child, so nothing here exec()s code in-process.
_FAKE_PKG = """
class MarkItDown:
    def __init__(self):
        from markitdown.converters import AudioConverter, PdfConverter
        if {registry}:
            self._converters = [_Reg(PdfConverter()), _Reg(AudioConverter()),
                                _Reg(AudioConverter())]

class _Reg:
    def __init__(self, converter):
        self.converter = converter
"""
_FAKE_CONVERTERS = """
class PdfConverter:
    pass
{audio}
"""


def _run_setup(tmp_path: Path, *, registry: bool = True,
               audio_class: bool = True) -> dict:
    pkg = tmp_path / "fake" / "markitdown"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(_FAKE_PKG.format(registry=registry))
    (pkg / "converters.py").write_text(_FAKE_CONVERTERS.format(
        audio="class AudioConverter:\n    pass\n" if audio_class else ""))
    script = tmp_path / "probe.py"
    script.write_text(
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        + markitdown_bounded._CHILD_SETUP +
        "kinds = None if md is None else "
        "[type(r.converter).__name__ for r in md._converters]\n"
        "print(json.dumps({'refuse': refuse, 'kinds': kinds}))\n")
    p = subprocess.run([sys.executable, "-I", str(script), str(pkg.parent)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_the_child_removes_every_audio_converter(tmp_path, allow_subprocess):
    out = _run_setup(tmp_path)
    assert out == {"refuse": None, "kinds": ["PdfConverter"]}


@pytest.mark.parametrize("registry,audio_class", [(False, True), (True, False)])
def test_the_child_refuses_everything_when_it_cannot_remove_it(
        tmp_path, allow_subprocess, registry, audio_class):
    # A MarkItDown that renamed its registry or the converter must not fall
    # back to converting with transcription on.
    out = _run_setup(tmp_path, registry=registry, audio_class=audio_class)
    assert out["kinds"] is None
    assert out["refuse"].startswith(
        "audio transcription (which uploads audio to Google) could not be "
        "disabled in this MarkItDown")


def test_the_child_code_runs_the_setup_before_any_conversion():
    code = markitdown_bounded._CHILD_CODE
    assert markitdown_bounded._CHILD_SETUP in code
    assert code.index(markitdown_bounded._CHILD_SETUP) < code.index("md.convert(")
    assert "if refuse:\n            raise RuntimeError(refuse)" in code


@pytest.mark.parametrize("name", ["memo.wav", "call.M4A", "talk.mp4",
                                  "x.mp3", "clip.mov", "v.webm"])
def test_audio_and_video_are_refused_by_name(name):
    assert markitdown_bounded.media_refusal(name).startswith(
        "audio and video are not converted here")


@pytest.mark.parametrize("name", ["report.pdf", "deck.pptx", "a.docx", "b.zip"])
def test_documents_are_not_refused_as_media(name):
    assert markitdown_bounded.media_refusal(name) is None


def test_send_to_refuses_a_recording_without_converting_it(tmp_path):
    class _Never:
        def convert(self, path):
            raise AssertionError("convert() ran on a recording")

    src = tmp_path / "meeting.m4a"
    src.write_bytes(b"\x00" * 64)
    ok, msg = markitdown_convert.convert_one(_Never(), str(src), tmp_path / "out")
    assert not ok
    assert msg.startswith("meeting.m4a: audio and video are not converted here")
    assert not list(tmp_path.rglob("*.md"))


def test_a_markitdown_that_will_not_import_is_named_as_such(tmp_path, allow_subprocess):
    """Review R14: a missing or broken MarkItDown used to be reported as
    "audio transcription could not be disabled", the wrong cause."""
    script = tmp_path / "probe.py"
    script.write_text(
        "import json, sys\n"
        "sys.modules['markitdown'] = None\n"        # import raises ImportError
        + markitdown_bounded._CHILD_SETUP +
        "print(json.dumps({'refuse': refuse, 'md': md is not None}))\n")
    p = subprocess.run([sys.executable, "-I", str(script)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["md"] is False
    assert out["refuse"].startswith("MarkItDown could not be loaded (")
    assert out["refuse"].endswith("); not converting")
    assert "audio transcription" not in out["refuse"]


@pytest.mark.parametrize("name", ["book.m4b", "clip.mpeg", "a.mpga", "b.wave"])
def test_more_audio_names_are_refused_with_the_reason(name):
    assert markitdown_bounded.media_refusal(name) is not None


# ---- the real library (review R14) --------------------------------------------
#
# Everything above runs against a stand-in package. This runs the child's
# setup against the real MarkItDown in the Dropper venv, with every socket
# connect and DNS lookup refused and recorded, so a MarkItDown upgrade that
# moves the converter registry shows up here, not to a user. It runs only
# where that venv exists; nothing here installs anything.

DROPPER_PY = Path.home() / ".markitdown-dropper-venv" / "bin" / "python"

_REAL_PROBE = r'''
import json, os, socket, sys, wave, zipfile
attempts = []
def _deny(what):
    attempts.append(str(what)[:120])
    raise OSError("network refused by test")
socket.socket.connect = lambda self, addr: _deny(addr)
socket.socket.connect_ex = lambda self, addr: _deny(addr)
socket.create_connection = lambda *a, **k: _deny(a[:1])
socket.getaddrinfo = lambda *a, **k: _deny(a[:1])
d = sys.argv[1]
wav = os.path.join(d, "memo.wav")
w = wave.open(wav, "wb"); w.setnchannels(1); w.setsampwidth(2)
w.setframerate(8000); w.writeframes(b"\x00\x00" * 8000); w.close()
zpath = os.path.join(d, "memo.zip")
with zipfile.ZipFile(zpath, "w") as z:
    z.write(wav, "memo.wav")
def run(mdi):
    res = {}
    for p in (wav, zpath):
        try:
            mdi.convert(p); res[os.path.basename(p)] = "converted"
        except Exception as e:
            res[os.path.basename(p)] = type(e).__name__
    return res
MODE = sys.argv[2]
if MODE == "stock":
    from markitdown import MarkItDown
    results = run(MarkItDown())
    refuse = None
else:
''' + "".join("    " + ln + "\n" for ln in markitdown_bounded._CHILD_SETUP.splitlines()) + r'''
    results = run(md) if md is not None else {}
print(json.dumps({"refuse": refuse, "attempts": attempts, "results": results}))
'''


def _real(tmp_path: Path, mode: str) -> dict:
    script = tmp_path / f"real_{mode}.py"
    script.write_text(_REAL_PROBE)
    work = tmp_path / mode
    work.mkdir()
    p = subprocess.run([str(DROPPER_PY), "-I", str(script), str(work), mode],
                       capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not DROPPER_PY.exists(), reason="no Markitdown Dropper venv here")
def test_the_real_markitdown_sends_nothing_once_the_child_setup_ran(
        tmp_path, allow_subprocess):
    out = _real(tmp_path, "guarded")
    assert out["refuse"] is None, out
    assert out["attempts"] == [], out


@pytest.mark.skipif(not DROPPER_PY.exists(), reason="no Markitdown Dropper venv here")
def test_the_real_probe_does_see_the_upload_without_the_setup(
        tmp_path, allow_subprocess):
    """The control: without the setup, stock MarkItDown tries to reach the
    network for the same files, so an empty list above means something.
    Skipped once the transcription extra is gone from the venv."""
    check = subprocess.run([str(DROPPER_PY), "-I", "-c", "import speech_recognition"],
                           capture_output=True, timeout=60)
    if check.returncode != 0:
        pytest.skip("speech_recognition not installed: nothing to upload with")
    out = _real(tmp_path, "stock")
    assert out["attempts"], out
