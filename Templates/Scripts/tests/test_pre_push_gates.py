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
# A real-looking domain, built at run time so this file does not trip the
# identity gate it is testing.
REAL = "realcorp" + ".com"
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


def test_a_name_in_a_commit_message_is_refused(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "Knowledge" / "x.md").write_text(PUBLIC, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"ping carol@{REAL} about it", "--no-verify")
    proc = _push(repo, _git(repo, "rev-parse", "HEAD"), base)
    assert proc.returncode == 1
    assert f"message:1: real-looking address: carol@{REAL}" in proc.stderr


def test_an_annotated_tag_message_is_scanned(repo: Path) -> None:
    _git(repo, "tag", "-a", "v1", "-m", f"release for dave@{REAL}")
    tag = _git(repo, "rev-parse", "v1")
    proc = _push(repo, tag, lref="refs/tags/v1")
    assert proc.returncode == 1
    assert f"real-looking address: dave@{REAL}" in proc.stderr


def test_every_message_in_a_tag_on_a_tag_is_scanned(repo: Path) -> None:
    # The remote receives the whole chain, so the inner message is published too.
    _git(repo, "tag", "-a", "inner", "-m", f"cut by erin@{REAL}")
    _git(repo, "tag", "-a", "outer", "-m", "plain release", "inner")
    proc = _push(repo, _git(repo, "rev-parse", "outer"), lref="refs/tags/outer")
    assert proc.returncode == 1
    assert f"real-looking address: erin@{REAL}" in proc.stderr


def test_an_updated_tag_message_is_scanned(repo: Path) -> None:
    # Same commit, new tag object: the range lists no commits at all.
    _git(repo, "tag", "-a", "v2", "-m", "plain release")
    old = _git(repo, "rev-parse", "v2")
    _git(repo, "tag", "-f", "-a", "v2", "-m", f"re-cut by frank@{REAL}")
    proc = _push(repo, _git(repo, "rev-parse", "v2"), old, lref="refs/tags/v2")
    assert proc.returncode == 1
    assert f"real-looking address: frank@{REAL}" in proc.stderr


def _merge_commit_with_mergetag(repo: Path, tag_message: str) -> str:
    """A merge whose `mergetag` header embeds a tag object, as merging a signed
    tag writes. Built by hand so the test needs no GPG key."""
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "Knowledge" / "side.md").write_text(PUBLIC, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side", "--no-verify")
    side = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    who = "T <t@example.com> 1700000000 +0000"
    tag = (f"object {side}\ntype commit\ntag v9\ntagger {who}\n\n{tag_message}\n")
    embedded = "\n ".join(tag.rstrip("\n").split("\n"))
    body = (f"tree {tree}\nparent {base}\nparent {side}\nauthor {who}\n"
            f"committer {who}\nmergetag {embedded}\n\nclean merge\n")
    return subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                          cwd=repo, input=body, capture_output=True, text=True,
                          check=True).stdout.strip(), base


def test_a_tag_message_embedded_in_a_merge_is_scanned(repo: Path) -> None:
    merge, base = _merge_commit_with_mergetag(repo, f"signed off by gina@{REAL}")
    proc = _push(repo, merge, base)
    assert proc.returncode == 1
    assert f"mergetag 1 message:1: real-looking address: gina@{REAL}" in proc.stderr


def test_a_latin1_tag_message_is_scanned(repo: Path) -> None:
    (repo / "installers" / "lib" / "identity-denylist.local").write_text(
        "Renée Fictional\n", encoding="utf-8")
    msg = repo.parent / "msg"
    msg.write_bytes("cut for Renée Fictional\n".encode("latin-1"))
    _git(repo, "-c", "i18n.commitEncoding=latin-1", "tag", "-a", "v3", "-F", str(msg))
    proc = _push(repo, _git(repo, "rev-parse", "v3"), lref="refs/tags/v3")
    assert proc.returncode == 1
    assert "Renée Fictional" in proc.stderr


def test_a_tag_name_is_scanned(repo: Path) -> None:
    (repo / "installers" / "lib" / "identity-denylist.local").write_text(
        "Fictionalname\n", encoding="utf-8")
    _git(repo, "tag", "-a", "Fictionalname-v1", "-m", "plain release")
    proc = _push(repo, _git(repo, "rev-parse", "Fictionalname-v1"),
                 lref="refs/tags/Fictionalname-v1")
    assert proc.returncode == 1
    assert "name:1: deny-list: Fictionalname" in proc.stderr
