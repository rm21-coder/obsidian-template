"""The pre-push hook runs both public-content gates over every pushed commit.

Exercised end to end: a temporary repo carrying the real hook and gates, with
the ref lines git would send on stdin (review rounds 1-2, 2026-10-04).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LIB = REPO / "installers" / "lib"
ZERO = "0" * 40
PUBLIC = "---\nclassification: public\n---\nok\n"
SECRET = "---\nclassification: restricted\n---\nsecret\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=T",
                           *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, allow_subprocess: None) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    root = tmp_path / "r"
    (root / "installers" / "lib" / "hooks").mkdir(parents=True)
    for name in ("check_classification.py", "check_identity_leak.py",
                 "identity-allowed-domains.txt"):
        shutil.copy2(LIB / name, root / "installers" / "lib" / name)
    shutil.copy2(LIB / "hooks" / "pre-push", root / "installers" / "lib" / "hooks" / "pre-push")
    _git(root, "init", "-q")
    (root / "Knowledge").mkdir()
    (root / "Knowledge" / "ok.md").write_text(PUBLIC, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base", "--no-verify")
    return root


def _push(root: Path, lsha: str, rsha: str = ZERO, lref: str = "refs/heads/main"):
    return subprocess.run(["bash", str(root / "installers/lib/hooks/pre-push"), "origin", "x"],
                          cwd=root, input=f"{lref} {lsha} {lref} {rsha}\n",
                          capture_output=True, text=True)


def test_a_clean_push_passes(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    proc = _push(repo, head)
    assert proc.returncode == 0, proc.stderr


def test_a_note_added_in_a_merge_commit_is_refused(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "side")
    (repo / "Knowledge" / "side.md").write_text(PUBLIC, encoding="utf-8")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "side", "--no-verify")
    _git(repo, "checkout", "-q", "-")
    (repo / "Knowledge" / "main.md").write_text(PUBLIC, encoding="utf-8")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "main", "--no-verify")
    _git(repo, "merge", "--no-commit", "--no-ff", "side")
    (repo / "Knowledge" / "evil.md").write_text(SECRET, encoding="utf-8")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "merge", "--no-verify")
    proc = _push(repo, _git(repo, "rev-parse", "HEAD"), base)
    assert proc.returncode == 1, proc.stderr
    assert "VIOLATION  Knowledge/evil.md @ " in proc.stderr


def test_a_tag_on_a_blob_is_refused(repo: Path) -> None:
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input="secret\n",
                          capture_output=True, text=True, check=True).stdout.strip()
    proc = _push(repo, blob, lref="refs/tags/blobtag")
    assert proc.returncode == 1
    assert "is not a commit or a tag on one" in proc.stderr


def test_an_unlistable_range_fails_closed(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    proc = _push(repo, head, rsha="1" * 40)          # remote sha we do not have
    assert proc.returncode == 1
    assert "cannot list commits in" in proc.stderr
