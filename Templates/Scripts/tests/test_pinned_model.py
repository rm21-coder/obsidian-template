"""test_pinned_model.py -- a Whisper model runs only at its reviewed commit.

Every backend used to download its model by repo name, so the model that ran
was whatever the repo's main branch held that day (bandit B615 on the ONNX
path; the MLX and faster-whisper paths had the same shape inside their
libraries). Closed 2026-09-25 by pinned_model.py + whisper_model_pins.py.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

import pinned_model as pm
import podcast_transcribe as pt
import whisper_model_pins
import whisper_onnx

REPO = "example-org/whisper-test"


def _pin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "snap"
    (d / "onnx").mkdir(parents=True)
    (d / "config.json").write_text('{"a": 1}')
    (d / "onnx" / "encoder_model.onnx").write_bytes(b"\x00weights" * 100)
    enc = (d / "onnx" / "encoder_model.onnx").read_bytes()
    cfg = (d / "config.json").read_bytes()
    monkeypatch.setitem(pm.PINS, REPO, {"revision": "a" * 40, "files": {
        "config.json": {"size": len(cfg),
                        "git_sha1": hashlib.sha1(b"blob %d\x00" % len(cfg) + cfg).hexdigest()},
        "onnx/encoder_model.onnx": {"size": len(enc), "sha256": hashlib.sha256(enc).hexdigest()},
    }})
    return d


PATTERNS = ["config.json", "onnx/encoder_model.onnx"]


def test_matching_files_verify(tmp_path, monkeypatch) -> None:
    d = _pin_dir(tmp_path, monkeypatch)
    assert pm.verify(REPO, d, PATTERNS) == ["config.json", "onnx/encoder_model.onnx"]


def test_an_altered_byte_is_refused(tmp_path, monkeypatch) -> None:
    d = _pin_dir(tmp_path, monkeypatch)
    p = d / "onnx" / "encoder_model.onnx"
    b = bytearray(p.read_bytes()); b[5] ^= 1; p.write_bytes(bytes(b))
    with pytest.raises(pm.ModelIntegrityError, match="does not match its pin"):
        pm.verify(REPO, d, PATTERNS)


def test_a_resized_file_is_refused(tmp_path, monkeypatch) -> None:
    d = _pin_dir(tmp_path, monkeypatch)
    (d / "config.json").write_text('{"a": 12}')
    with pytest.raises(pm.ModelIntegrityError, match="bytes, pinned"):
        pm.verify(REPO, d, PATTERNS)


def test_a_missing_file_is_refused(tmp_path, monkeypatch) -> None:
    d = _pin_dir(tmp_path, monkeypatch)
    (d / "config.json").unlink()
    with pytest.raises(pm.ModelIntegrityError, match="is missing"):
        pm.verify(REPO, d, PATTERNS)


def test_an_unpinned_file_the_backend_would_load_is_refused(tmp_path, monkeypatch) -> None:
    d = _pin_dir(tmp_path, monkeypatch)
    (d / "extra.json").write_text("{}")
    with pytest.raises(pm.ModelIntegrityError, match="is not in the pins"):
        pm.verify(REPO, d, ["*.json", "onnx/encoder_model.onnx"])


def test_git_blob_hash_matches_git(tmp_path, allow_subprocess) -> None:
    f = tmp_path / "x.txt"; f.write_bytes(b"hello pinned world\n")
    want = subprocess.run(["git", "hash-object", str(f)], capture_output=True,
                          text=True, check=True).stdout.strip()
    assert pm._file_digest(f, "sha1", f.stat().st_size) == want


@pytest.fixture
def fake_hub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    calls: list[dict] = []
    def snapshot_download(repo, **kw):
        calls.append({"repo": repo, **kw})
        return str(kw.pop("_dir", tmp_path / "snap"))
    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        types.SimpleNamespace(snapshot_download=snapshot_download))
    return calls


def test_a_pinned_model_is_fetched_at_its_revision_and_verified(tmp_path, monkeypatch, fake_hub) -> None:
    _pin_dir(tmp_path, monkeypatch)
    out = pm.fetch(REPO, allow_patterns=PATTERNS, log=lambda m: None)
    assert out == tmp_path / "snap"
    assert fake_hub == [{"repo": REPO, "revision": "a" * 40,
                         "allow_patterns": PATTERNS, "cache_dir": None}]


def test_an_unpinned_model_is_refused_without_a_download(fake_hub) -> None:
    with pytest.raises(pm.UnpinnedModel, match="is not a pinned model"):
        pm.fetch("someone/whisper-evil", allow_patterns=["*"], log=lambda m: None)
    assert fake_hub == []


def test_the_override_is_explicit_and_logged(fake_hub) -> None:
    logged: list[str] = []
    pm.fetch("someone/whisper-x", allow_patterns=["*"], allow_unpinned=True,
             log=logged.append)
    assert fake_hub[0]["revision"] == "main"
    assert any("NOT pinned" in m for m in logged)


def test_an_operator_named_directory_is_used_as_is(tmp_path, fake_hub) -> None:
    d = tmp_path / "my-model"; d.mkdir()
    assert pm.fetch(str(d), allow_patterns=["*"], log=lambda m: None) == d
    assert fake_hub == []


# --- the real pin table ------------------------------------------------------

def test_every_default_model_is_pinned() -> None:
    pins = whisper_model_pins.PINS
    assert pt.DEFAULT_MODEL in pins                                   # macOS / MLX
    assert whisper_onnx.resolve_repo(pt.DEFAULT_ONNX_MODEL) in pins   # Windows ARM64
    assert "Systran/faster-whisper-base" in pins                      # x86 fallback


def test_pins_are_well_formed() -> None:
    for repo, pin in whisper_model_pins.PINS.items():
        assert re.fullmatch(r"[0-9a-f]{40}", pin["revision"]), repo
        assert pin["files"], repo
        for rel, meta in pin["files"].items():
            assert isinstance(meta["size"], int) and meta["size"] >= 0, (repo, rel)
            hashes = {k: v for k, v in meta.items() if k in ("sha256", "git_sha1")}
            assert len(hashes) == 1, (repo, rel)
            (k, v), = hashes.items()
            assert re.fullmatch(r"[0-9a-f]{%d}" % (64 if k == "sha256" else 40), v), (repo, rel)


def test_the_files_each_backend_loads_are_all_pinned() -> None:
    pins = whisper_model_pins.PINS
    assert {"config.json", "weights.safetensors"} <= set(pins[pt.DEFAULT_MODEL]["files"])
    onnx = pins[whisper_onnx.resolve_repo(pt.DEFAULT_ONNX_MODEL)]["files"]
    assert {"onnx/encoder_model.onnx", "onnx/decoder_model_merged.onnx"} <= set(onnx)
    assert {"model.bin", "config.json", "tokenizer.json"} <= set(pins["Systran/faster-whisper-base"]["files"])


def test_no_backend_downloads_by_name_any_more() -> None:
    """Every snapshot_download call lives in pinned_model.py and names a revision."""
    here = Path(pt.__file__).parent
    calls = 0
    for py in here.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for m in re.finditer(r"snapshot_download\(", text):
            depth, i = 1, m.end()
            while depth and i < len(text):           # the call's full argument list
                depth += (text[i] == "(") - (text[i] == ")")
                i += 1
            args = text[m.end():i - 1]
            calls += 1
            assert py.name == "pinned_model.py", f"{py.name} downloads a model directly"
            assert "revision=" in args, (py.name, args[:80])
    assert calls, "found no snapshot_download calls at all -- the check is not looking"


def test_mlx_backend_is_given_the_verified_local_directory(tmp_path, monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(pt, "_mlx_available", lambda: True)
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(
        transcribe=lambda audio, path_or_hf_repo, verbose: seen.setdefault(
            "path", path_or_hf_repo) and {"text": "", "segments": []}))
    monkeypatch.setattr(pm, "fetch", lambda model, **kw: tmp_path / "verified")
    pt.transcribe(tmp_path / "a.mp3", model=None, verbose=False)
    assert seen["path"] == str(tmp_path / "verified")


def test_an_unpinned_mlx_model_stops_the_run(monkeypatch, fake_hub) -> None:
    monkeypatch.setattr(pt, "_mlx_available", lambda: True)
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=None))
    with pytest.raises(SystemExit):
        pt.transcribe(Path("a.mp3"), model="someone/whisper-evil", verbose=False)
    assert fake_hub == []
