"""
test_source_media.py -- the drop folders are this account's alone (5278632).

Drops are voice transcripts and podcast audio. A bare mkdir under the default
umask left them 0755, readable by every other local account, and the
retired done/ and failed/ folders (with each failure's .error.log) the same.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import source_media
from platform_caps import requires_posix_modes, requires_symlinks


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "SourceMedia"
    monkeypatch.setattr(source_media, "drop_root", lambda: r)
    old = os.umask(0o022)
    yield r
    os.umask(old)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@requires_posix_modes
def test_new_drop_folders_are_owner_only(root: Path) -> None:
    made = source_media.ensure_dirs()
    assert [p.name for p in made] == sorted(source_media.DIR_NAMES.values())
    assert _mode(root) == 0o700
    for p in made:
        assert _mode(p) == 0o700, p


@requires_posix_modes
def test_an_existing_install_is_tightened(root: Path) -> None:
    pod = root / "PodcastInput"
    for sub in ("done", "failed"):
        (pod / sub).mkdir(parents=True)
    retired = pod / "done" / "episode.m4a"
    retired.write_bytes(b"audio")
    err = pod / "failed" / "bad.m4a.error.log"
    err.write_text("Traceback\n", encoding="utf-8")
    for p in (root, pod, pod / "done", pod / "failed"):
        os.chmod(p, 0o755)
    for p in (retired, err):
        os.chmod(p, 0o644)

    source_media.ensure_dirs()

    for p in (root, pod, pod / "done", pod / "failed"):
        assert _mode(p) == 0o700, p
    assert _mode(retired) == 0o600
    assert _mode(err) == 0o600


def test_retire_folders_are_not_created_where_unused(root: Path) -> None:
    source_media.ensure_dirs()
    assert not (root / "VoiceInput" / "done").exists()
    assert not (root / "VoiceInput" / "failed").exists()


@requires_posix_modes
@requires_symlinks
def test_a_symlinked_retire_folder_is_not_followed(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    keep = outside / "note.md"
    keep.write_text("x", encoding="utf-8")
    os.chmod(outside, 0o755)
    os.chmod(keep, 0o644)
    pod = root / "PodcastInput"
    pod.mkdir(parents=True)
    (pod / "done").symlink_to(outside, target_is_directory=True)

    source_media.ensure_dirs()

    assert _mode(outside) == 0o755
    assert _mode(keep) == 0o644
