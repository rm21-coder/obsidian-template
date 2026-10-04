"""check_hooks_fresh.py: the installed git hooks must be the repository's own.

The hooks are installed as copies, and the maintainer's pre-push hook sat as a
stale copy for nine days, so the push gates added meanwhile never ran
(2026-10-04). Each failure path asserts its own message.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CHECK = REPO / "installers" / "lib" / "check_hooks_fresh.py"


@pytest.fixture
def repo(tmp_path: Path, allow_subprocess: None) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    root = tmp_path / "r"
    (root / "installers" / "lib" / "hooks").mkdir(parents=True)
    for name in ("pre-commit", "pre-push"):
        (root / "installers" / "lib" / "hooks" / name).write_text(
            f"#!/bin/sh\necho {name}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def _install(root: Path) -> Path:
    hooks = root / ".git" / "hooks"
    for src in (root / "installers" / "lib" / "hooks").iterdir():
        shutil.copy2(src, hooks / src.name)
        (hooks / src.name).chmod(0o755)
    return hooks


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CHECK), "--repo-root", str(root)],
                          capture_output=True, text=True)


def test_current_hooks_pass(repo: Path) -> None:
    _install(repo)
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout
    assert "2 hook(s) installed and current: pre-commit, pre-push" in proc.stdout


def test_a_missing_hook_fails(repo: Path) -> None:
    hooks = _install(repo)
    (hooks / "pre-push").unlink()
    proc = _run(repo)
    assert proc.returncode == 1
    assert "pre-push: not installed" in proc.stdout


def test_a_stale_copy_fails(repo: Path) -> None:
    _install(repo)
    (repo / "installers" / "lib" / "hooks" / "pre-push").write_text(
        "#!/bin/sh\necho newer gates\n", encoding="utf-8")
    proc = _run(repo)
    assert proc.returncode == 1
    assert "pre-push: installed copy differs from the repository's" in proc.stdout


@pytest.mark.skipif(os.name == "nt", reason="no POSIX exec bit on Windows")
def test_a_non_executable_hook_fails(repo: Path) -> None:
    hooks = _install(repo)
    (hooks / "pre-commit").chmod(0o644)
    proc = _run(repo)
    assert proc.returncode == 1
    assert "pre-commit: installed but not executable" in proc.stdout


def test_core_hookspath_is_where_it_looks(repo: Path) -> None:
    # Hooks installed in .git/hooks do not count when git runs them elsewhere.
    _install(repo)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", "elsewhere"],
                   check=True)
    proc = _run(repo)
    assert proc.returncode == 1
    assert "pre-commit: not installed" in proc.stdout


def test_outside_a_checkout_is_named(tmp_path: Path, allow_subprocess: None) -> None:
    (tmp_path / "installers" / "lib" / "hooks").mkdir(parents=True)
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(tmp_path.parent))
    proc = subprocess.run([sys.executable, str(CHECK), "--repo-root", str(tmp_path)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 2
    assert "not a git checkout" in proc.stdout
