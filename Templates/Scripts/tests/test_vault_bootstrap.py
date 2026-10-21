"""
test_vault_bootstrap.py -- installers/components/10-vault-bootstrap.sh, run
against a scratch HOME and a scratch repository.

5278631: the vault root ends up 0700, and a real ~/Obsidian that is not this
repo is refused rather than having a link to the repo put inside it (what
`ln -sfn` does when its target is an existing directory).

The venv already exists in the scratch repo and install_requirements is
stubbed, so nothing is created or downloaded.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
COMPONENT = REPO / "installers" / "components" / "10-vault-bootstrap.sh"
FOLDERS = ("Actions Categories Clippings Creations Daily Groups Knowledge "
           "Meetings Notes People Templates Topics Z_archive Z_attachments").split()

pytestmark = pytest.mark.skipif(os.name != "posix", reason="bash component")


def _repo(path: Path) -> Path:
    for d in FOLDERS:
        (path / d).mkdir(parents=True, exist_ok=True)
    py = path / "Templates" / "Scripts" / ".venv" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    py.chmod(0o755)
    os.chmod(path, 0o755)
    return path


def _run(home: Path, repo: Path, *, interactive: int = 0, stdin: str = "") -> subprocess.CompletedProcess:
    script = (f'source "{REPO}/installers/lib/common.sh"\n'
              'install_requirements() { :; }\n'
              f'source "{COMPONENT}"\n')
    env = {**os.environ, "HOME": str(home), "REPO_ROOT": str(repo),
           "INTERACTIVE": str(interactive)}
    return subprocess.run(["/bin/bash", "-c", script], env=env, input=stdin,
                          capture_output=True, text=True, timeout=60)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_the_cloned_vault_is_made_owner_only(tmp_path, allow_subprocess) -> None:
    home = tmp_path / "home"
    vault = _repo(home / "Obsidian")
    r = _run(home, vault)
    assert r.returncode == 0, r.stderr
    assert _mode(vault) == 0o700
    assert "vault restricted to its owner (0700)" in r.stderr


def test_a_symlinked_vault_is_accepted_and_its_target_restricted(
        tmp_path, allow_subprocess) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _repo(tmp_path / "clone")
    (home / "Obsidian").symlink_to(repo, target_is_directory=True)
    r = _run(home, repo)
    assert r.returncode == 0, r.stderr
    assert "vault at" in r.stderr
    assert _mode(repo) == 0o700


def test_a_new_symlink_is_offered_and_made(tmp_path, allow_subprocess) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _repo(tmp_path / "clone")
    r = _run(home, repo, interactive=1, stdin="y\n")
    assert r.returncode == 0, r.stderr
    link = home / "Obsidian"
    assert link.is_symlink() and link.resolve() == repo.resolve()
    assert _mode(repo) == 0o700


def test_a_real_folder_that_is_not_the_repo_is_refused(
        tmp_path, allow_subprocess) -> None:
    home = tmp_path / "home"
    other = home / "Obsidian"
    other.mkdir(parents=True)
    (other / "my-note.md").write_text("mine", encoding="utf-8")
    os.chmod(other, 0o755)
    repo = _repo(tmp_path / "clone")
    r = _run(home, repo, interactive=1, stdin="y\n")
    assert r.returncode == 1
    assert "already exists and is not this repo" in r.stderr
    # Nothing was linked inside it, and it was not re-moded.
    assert sorted(p.name for p in other.iterdir()) == ["my-note.md"]
    assert _mode(other) == 0o755
