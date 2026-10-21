"""
test_handoff_source.py -- an empty HMAC key must not verify anything.

An empty or whitespace-only HANDOFF_HMAC_KEY_FILE loaded as b"" rather than
None, so verify_signature went ahead under a key anyone can compute and every
forged handoff verified. Microsoft M-DASH 2026-09-23 (CWE-347).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import stat
import time
from pathlib import Path

import pytest

import handoff_source as hs
from platform_caps import CAN_SYMLINK, requires_posix_modes, requires_symlinks

DATA = b'{"meetings": []}'


REAL_KEY = b"0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("key", [
    b"", b"   ", b"\n\t",
    # HMAC zero-pads short keys, so every one of these computes the MAC of the
    # empty key. A NUL-only key file survived the first version of the check.
    b"\x00", b"\x00" * 32, b"\x00" * 64, b"\n\x00\x00\n",
    b"short",                      # guessable, whatever it is
])
@pytest.mark.parametrize("required", [True, False])
def test_forgery_under_an_empty_key_is_refused(key: bytes, required: bool) -> None:
    forged = hmac.new(b"", DATA, hashlib.sha256).hexdigest() if not key.strip(b" \n\t\x00") \
        else hmac.new(key, DATA, hashlib.sha256).hexdigest()
    with pytest.raises(hs.HandoffError, match="shorter than 16 significant bytes"):
        hs.verify_signature(DATA, forged, key, required=required)


def test_empty_key_file_is_refused_end_to_end(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    kf = tmp_path / "key"
    kf.write_bytes(b"  \n")
    monkeypatch.setenv("HANDOFF_HMAC_KEY_FILE", str(kf))
    monkeypatch.delenv("HANDOFF_HMAC_KEY", raising=False)
    key = hs.load_hmac_key()
    forged = hmac.new(key, DATA, hashlib.sha256).hexdigest()
    with pytest.raises(hs.HandoffError):
        hs.verify_signature(DATA, forged, key, required=False)


def test_a_real_key_still_verifies() -> None:
    sig = hmac.new(REAL_KEY, DATA, hashlib.sha256).hexdigest()
    hs.verify_signature(DATA, sig, REAL_KEY, required=True)


@pytest.mark.parametrize("make", ["missing", "directory", "dangling"])
def test_configured_key_file_that_is_not_a_file_fails_closed(
        tmp_path, monkeypatch: pytest.MonkeyPatch, make: str) -> None:
    """A moved or deleted key file used to warn and fall back to "no key",
    which with the default HANDOFF_REQUIRE_SIGNATURE off accepted unsigned
    handoffs: authenticity switched off silently."""
    kf = tmp_path / "key"
    if make == "directory":
        kf.mkdir()
    elif make == "dangling":
        if not CAN_SYMLINK:
            pytest.skip("cannot create symlinks here (Windows: Developer Mode or "
                        "elevation) -- the dangling-key-file case is NOT verified")
        kf.symlink_to(tmp_path / "nowhere")
    monkeypatch.setenv("HANDOFF_HMAC_KEY_FILE", str(kf))
    monkeypatch.delenv("HANDOFF_HMAC_KEY", raising=False)
    with pytest.raises(hs.HandoffError, match="not a readable file"):
        hs.load_hmac_key()


# ---------------------------------------------------------------------------
# 5278617: the archive held meeting bodies and join URLs at 0644, forever.
# ---------------------------------------------------------------------------

def _drop_set(folder, name: str = "week") -> "Path":
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text('{"meetings": []}\n', encoding="utf-8")
    (folder / f"{name}.json.sha256").write_text("x  week.json\n", encoding="utf-8")
    ready = folder / f"{name}.ready"
    ready.write_text("", encoding="utf-8")
    for f in folder.iterdir():
        os.chmod(f, 0o644)
    return ready


@pytest.fixture
def loose_umask():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@requires_posix_modes
def test_ack_archives_owner_only(tmp_path, loose_umask) -> None:
    ingest = tmp_path / "MeetingIngest"
    ready = _drop_set(ingest)
    os.chmod(ingest, 0o755)
    hs.DropFolderSource(ingest).ack(ready)
    processed = ingest / hs.DropFolderSource.PROCESSED_SUBDIR
    archived = sorted(processed.iterdir())
    assert len(archived) == 3
    for f in archived:
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f.name
    assert stat.S_IMODE(processed.stat().st_mode) == 0o700
    assert stat.S_IMODE(ingest.stat().st_mode) == 0o700
    assert not list(ingest.glob("week.*"))


def test_ack_prunes_archived_sets_past_retention(tmp_path) -> None:
    assert hs.PROCESSED_RETENTION_DAYS == 14
    ingest = tmp_path / "MeetingIngest"
    processed = ingest / hs.DropFolderSource.PROCESSED_SUBDIR
    processed.mkdir(parents=True)
    old = processed / "week.20260801T210000.json"
    old.write_text("{}", encoding="utf-8")
    recent = processed / "week.20261015T210000.json"
    recent.write_text("{}", encoding="utf-8")
    now = time.time()
    os.utime(old, (now - 15 * 86400, now - 15 * 86400))
    os.utime(recent, (now - 13 * 86400, now - 13 * 86400))
    keep_dir = processed / "a-subfolder"
    keep_dir.mkdir()
    os.utime(keep_dir, (now - 99 * 86400, now - 99 * 86400))

    ready = _drop_set(ingest, "next")
    # The producer's file is old; archiving must not make it prunable at once.
    os.utime(ingest / "next.json", (now - 30 * 86400, now - 30 * 86400))
    hs.DropFolderSource(ingest).ack(ready)

    names = {p.name for p in processed.iterdir()}
    assert old.name not in names
    assert recent.name in names
    assert "a-subfolder" in names
    assert any(n.startswith("next.") and n.endswith(".json") for n in names), names


def test_prune_reports_what_it_removed(tmp_path) -> None:
    processed = tmp_path / "_processed"
    processed.mkdir()
    (processed / "a.json").write_text("{}", encoding="utf-8")
    (processed / "b.json").write_text("{}", encoding="utf-8")
    assert hs.prune_processed(processed, days=1, now=time.time() + 2 * 86400) \
        == ["a.json", "b.json"]
    assert hs.prune_processed(tmp_path / "missing") == []


@requires_symlinks
def test_prune_does_not_follow_a_symlinked_directory(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.md"
    precious.write_text("keep", encoding="utf-8")
    processed = tmp_path / "_processed"
    processed.mkdir()
    (processed / "link").symlink_to(outside, target_is_directory=True)
    hs.prune_processed(processed, days=0, now=time.time() + 86400)
    assert precious.exists()
