"""Security-suite artifacts and drift-review reports stay out of the repository
when the tools run from a git worktree.

security-checks.sh defaulted its artifacts to a sibling of REPO_ROOT. From a
worktree at <checkout>/.claude/worktrees/<name>, that sibling is
<checkout>/.claude/worktrees/obsidian-security/ -- inside the public checkout,
which CLAUDE.md forbids. The default now resolves from the main checkout.
drift_review.py's inside-the-repo guard had the same blind spot: a report_dir
in the main checkout but outside the worktree passed it.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[3] / "installers" / "lib"
CHECKS = LIB / "security-checks.sh"

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

pytestmark = pytest.mark.skipif(
    platform.system() == "Windows" or not shutil.which("bash") or not shutil.which("git"),
    reason="needs bash and git")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV}).stdout.strip()


@pytest.fixture
def checkout(tmp_path, allow_subprocess):
    """<tmp>/obsidian-template holding the suite, plus a worktree under
    .claude/worktrees/wt, as the desktop app lays them out."""
    main = tmp_path / "obsidian-template"
    (main / "installers" / "lib").mkdir(parents=True)
    shutil.copy2(CHECKS, main / "installers" / "lib" / "security-checks.sh")
    _git(main, "init", "-q")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "suite")
    wt = main / ".claude" / "worktrees" / "wt"
    _git(main, "worktree", "add", "-q", str(wt))
    return main, wt


def _artifact_root(checkout_dir: Path, *extra: str, env_extra: dict | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "SECURITY_ARTIFACTS_DIR"}
    env.update(env_extra or {})
    r = subprocess.run(["bash", str(checkout_dir / "installers" / "lib" / "security-checks.sh"),
                        *extra, "--print-artifact-root"],
                       cwd=checkout_dir, capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_from_a_worktree_the_default_is_the_main_checkouts_sibling(checkout, tmp_path):
    main, wt = checkout
    got = _artifact_root(wt)
    assert got == str(tmp_path / "obsidian-security" / "verification-artifacts")
    # The old default, which put scan output inside the public checkout.
    assert got != str(main / ".claude" / "worktrees" / "obsidian-security" / "verification-artifacts")
    assert not Path(got).resolve().is_relative_to(main.resolve())


def test_from_the_main_checkout_the_default_is_unchanged(checkout, tmp_path):
    main, _ = checkout
    assert _artifact_root(main) == str(tmp_path / "obsidian-security" / "verification-artifacts")


def test_outside_git_the_default_falls_back_to_the_scripts_own_checkout(tmp_path, allow_subprocess):
    plain = tmp_path / "unpacked"
    (plain / "installers" / "lib").mkdir(parents=True)
    shutil.copy2(CHECKS, plain / "installers" / "lib" / "security-checks.sh")
    got = _artifact_root(plain, env_extra={"GIT_CEILING_DIRECTORIES": str(tmp_path)})
    assert got == str(tmp_path / "obsidian-security" / "verification-artifacts")


def test_overrides_still_win_from_a_worktree(checkout, tmp_path):
    _, wt = checkout
    assert _artifact_root(wt, env_extra={"SECURITY_ARTIFACTS_DIR": str(tmp_path / "env")}) \
        == str(tmp_path / "env")
    assert _artifact_root(wt, "--artifacts", str(tmp_path / "flag")) == str(tmp_path / "flag")


def test_drift_reports_in_the_main_checkout_are_refused_from_a_worktree(checkout, monkeypatch):
    main, wt = checkout
    spec = importlib.util.spec_from_file_location("drift_review", LIB / "drift_review.py")
    dr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dr)
    monkeypatch.setattr(dr, "REPO_ROOT", wt)
    cfg = wt.parent / "c.json"
    cfg.write_text('{"report_dir": "%s"}' % (main / ".claude" / "drift-reports"), encoding="utf-8")
    with pytest.raises(dr.ReviewError, match="report_dir .*drift-reports is inside the repository"):
        dr.load_config(cfg)
    cfg.write_text('{"task_note": "%s"}' % (main / "Actions" / "Drift.md"), encoding="utf-8")
    with pytest.raises(dr.ReviewError, match="task_note must be a .md file outside the repository"):
        dr.load_config(cfg)


def test_an_inherited_print_root_does_not_skip_the_run(checkout, tmp_path):
    """Only the flag may turn on --print-artifact-root. PRINT_ROOT was never
    initialised, so PRINT_ROOT=1 in the environment made every run -- the
    hooks' --fast and --full included -- print the path and exit 0 without
    running a pass or writing an artifact: a pass the gates could not see."""
    main, _ = checkout
    art = tmp_path / "art"
    env = {k: v for k, v in os.environ.items() if k != "SECURITY_ARTIFACTS_DIR"}
    env.update(PRINT_ROOT="1", SECURITY_ARTIFACTS_DIR=str(art))
    r = subprocess.run(["bash", str(main / "installers" / "lib" / "security-checks.sh"), "--fast"],
                       cwd=main, capture_output=True, text=True, timeout=120, env=env)
    assert r.stdout.strip() != str(art), "printed the artifact root instead of running"
    # The run started: it made its stamped artifact directory.
    assert list(art.glob("checks-*")), r.stdout + r.stderr


def test_changed_on_a_clean_tree_says_nothing_was_checked(tmp_path, allow_subprocess):
    """N2: with nothing in the working diff, --changed ran no pass and still
    printed RESULT: PASS (checks-20261110-115843). It must say so and must
    not exit 0, or a caller reads it as a passing check. (A plain checkout:
    the shared fixture's worktree shows as untracked, which is a change.)"""
    main = tmp_path / "obsidian-template"
    (main / "installers" / "lib").mkdir(parents=True)
    shutil.copy2(CHECKS, main / "installers" / "lib" / "security-checks.sh")
    _git(main, "init", "-q")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "suite")
    env = {k: v for k, v in os.environ.items() if k != "SECURITY_ARTIFACTS_DIR"}
    env["SECURITY_ARTIFACTS_DIR"] = str(tmp_path / "art")
    r = subprocess.run(["bash", str(main / "installers" / "lib" / "security-checks.sh"), "--changed"],
                       cwd=main, capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "RESULT: NOTHING CHECKED" in r.stdout
    assert "RESULT: PASS" not in r.stdout
    (summary,) = (tmp_path / "art").glob("checks-*/summary.txt")
    assert "NOTHING CHECKED" in summary.read_text()
