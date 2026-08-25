"""
test_check_classification.py — the public-repo classification audit.

Focused on the gitignore filter, which is the part that has failed silently
before. The audit walks the filesystem, so without the filter it flags local
runtime output (Creations/RAG-Sync-*.md) that can never reach the repo, and
install.sh / install.ps1 then hard-fail at component 02 until the audit is
skipped.

The filter's first implementation worked on macOS and silently matched
nothing on Windows -- str(Path) gave git a backslash path, which git quoted
and escaped on the way back out, and text=True turned the newline delimiter
into CRLF so the CR arrived as part of the filename. Both are invisible from
a POSIX machine, which is why this exercises git for real rather than
mocking it.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AUDIT_PY = REPO_ROOT / "installers" / "lib" / "check_classification.py"


@pytest.fixture(scope="module")
def audit():
    """Import check_classification.py by path -- installers/lib is not a
    package and is not on sys.path."""
    if not AUDIT_PY.is_file():
        pytest.skip(f"audit script not found at {AUDIT_PY}")
    spec = importlib.util.spec_from_file_location("check_classification",
                                                  AUDIT_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_classification"] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop("check_classification", None)


@pytest.fixture
def git_repo(tmp_path: Path, allow_subprocess: None) -> Path:
    """A throwaway checkout that ignores Creations/ the way the real one does."""
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(
        "Creations/*\n!Creations/.gitkeep\n", encoding="utf-8")
    for folder in ("Creations", "Knowledge"):
        (tmp_path / folder).mkdir()
    (tmp_path / "Creations" / "RAG-Sync-2026-08-25_000000.md").write_text(
        "---\ntitle: local run report\n---\nbody\n", encoding="utf-8")
    (tmp_path / "Knowledge" / "real.md").write_text(
        "---\ntitle: tracked note\n---\nbody\n", encoding="utf-8")
    return tmp_path


IGNORED = Path("Creations/RAG-Sync-2026-08-25_000000.md")
TRACKED = Path("Knowledge/real.md")


class TestGitIgnoreFilter:

    def test_ignored_file_is_detected(self, audit, git_repo: Path,
                                      allow_subprocess: None) -> None:
        ignored = audit.git_ignored(git_repo, [IGNORED, TRACKED])
        assert IGNORED in ignored, (
            "gitignored runtime output was not detected as ignored -- the "
            "filter is a no-op, and the audit will flag files that can never "
            "reach the repo")

    def test_tracked_file_is_not_swept_up(self, audit, git_repo: Path,
                                          allow_subprocess: None) -> None:
        ignored = audit.git_ignored(git_repo, [IGNORED, TRACKED])
        assert TRACKED not in ignored, (
            "a committable file was treated as ignored -- the audit would "
            "stop inspecting real content")

    def test_fails_open_outside_a_checkout(self, audit, tmp_path: Path,
                                           allow_subprocess: None) -> None:
        """No git, no exclusions. The audit is a safety net for a PUBLIC repo;
        a missing checkout must not quietly shrink what it looks at."""
        assert audit.git_ignored(tmp_path, [IGNORED]) == set()

    def test_empty_input_short_circuits(self, audit, git_repo: Path) -> None:
        assert audit.git_ignored(git_repo, []) == set()


class TestAuditRespectsTheFilter:

    def test_all_md_files_excludes_ignored(self, audit, git_repo: Path,
                                           allow_subprocess: None) -> None:
        found = audit.all_md_files(git_repo)
        assert TRACKED in found
        assert IGNORED not in found, (
            "the audit is walking gitignored output; component 02 will "
            "hard-fail on any machine that has run the RAG sync")
