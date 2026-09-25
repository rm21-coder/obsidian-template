"""pinned_model.py -- fetch a Whisper model only at its reviewed commit, and check it.

Every transcription backend used to download its model from Hugging Face by
repo name alone, so whatever the repo's main branch held at download time is
what ran: a supply-chain fetch with nothing pinned, in a project whose headline
control is pinning plugins by tag and SHA-256. bandit flagged the ONNX path
(B615, open since CISO packet v2.3); the macOS path through mlx-whisper and the
x86 path through faster-whisper had the same shape and were not flagged, because
the download happens inside the library. Closed 2026-09-25.

Now each backend asks this module for a local directory:

  * a model named in whisper_model_pins.PINS is downloaded at its pinned commit
    only, and every file the backend will load is checked against the pinned
    size and hash (SHA-256 for LFS objects, git blob SHA-1 for small files)
    before the path is returned -- a revision pin stops the repo changing under
    you, the hash check stops a tampered cache or CDN;
  * a file in the snapshot that matches what the backend loads but is not in
    the pins is refused;
  * a model that is not pinned is refused, unless the operator passes
    --allow-unpinned-model, which is logged;
  * a local directory the operator names explicitly is theirs and is used as-is.
"""
from __future__ import annotations

import fnmatch
import hashlib
import sys
from pathlib import Path

from whisper_model_pins import PINS


class UnpinnedModel(RuntimeError):
    """The model is not in whisper_model_pins.PINS."""


class ModelIntegrityError(RuntimeError):
    """A downloaded file does not match its pin."""


def _matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel, p) for p in patterns)


def _file_digest(path: Path, algo: str, size: int) -> str:
    h = hashlib.new(algo)
    if algo == "sha1":
        h.update(b"blob %d\x00" % size)      # git's blob object header
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(repo: str, local_dir: Path, patterns: list[str]) -> list[str]:
    """Check every pinned file of `repo` matching `patterns` in `local_dir`.

    Returns the verified relative paths. Raises ModelIntegrityError on a
    missing, resized, altered or unpinned file.
    """
    files = PINS[repo]["files"]
    wanted = sorted(p for p in files if _matches(p, patterns))
    if not wanted:
        raise ModelIntegrityError(f"{repo}: no pinned file matches {patterns}")
    for rel in wanted:
        f = local_dir / rel
        meta = files[rel]
        if not f.is_file():
            raise ModelIntegrityError(f"{repo}: {rel} is missing from {local_dir}")
        size = f.stat().st_size
        if size != meta["size"]:
            raise ModelIntegrityError(
                f"{repo}: {rel} is {size} bytes, pinned {meta['size']}")
        if "sha256" in meta:
            got, want = _file_digest(f, "sha256", size), meta["sha256"]
        else:
            got, want = _file_digest(f, "sha1", size), meta["git_sha1"]
        if got != want:
            raise ModelIntegrityError(
                f"{repo}: {rel} does not match its pin (got {got[:12]}..., "
                f"pinned {want[:12]}...)")
    for f in local_dir.rglob("*"):
        if f.is_file():
            rel = f.relative_to(local_dir).as_posix()
            if _matches(rel, patterns) and rel not in files:
                raise ModelIntegrityError(f"{repo}: {rel} is not in the pins")
    return wanted


def fetch(model: str, *, allow_patterns: list[str], cache_dir: str | None = None,
          allow_unpinned: bool = False, log=None) -> Path:
    """Local directory holding `model`, downloaded at its pin and verified."""
    log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
    local = Path(model).expanduser()
    if local.is_dir():
        log(f"using the local model directory the operator named: {local}")
        return local
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError("huggingface-hub is required to fetch the model") from exc
    pin = PINS.get(model)
    if pin is None:
        if not allow_unpinned:
            raise UnpinnedModel(
                f"'{model}' is not a pinned model (pinned: {', '.join(sorted(PINS))}). "
                "Pin it in whisper_model_pins.py after review, or pass "
                "--allow-unpinned-model to run it unverified.")
        log(f"WARNING: '{model}' is NOT pinned; running whatever its main branch "
            "holds today, unverified (--allow-unpinned-model)")
        return Path(snapshot_download(model, revision="main",
                                      allow_patterns=allow_patterns,
                                      cache_dir=cache_dir))
    path = Path(snapshot_download(model, revision=pin["revision"],
                                  allow_patterns=allow_patterns,
                                  cache_dir=cache_dir))
    verified = verify(model, path, allow_patterns)
    log(f"model {model}@{pin['revision'][:10]} verified ({len(verified)} files)")
    return path
