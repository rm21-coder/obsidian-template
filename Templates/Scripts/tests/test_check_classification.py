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


@pytest.mark.parametrize("block, expected", [
    ("classification: public\nclassification: confidential", "(frontmatter the gate cannot read reliably)"),  # duplicate key: Obsidian rejects it (round 4)
    ("classification: confidential\nclassification: public", "(frontmatter the gate cannot read reliably)"),  # duplicate key: Obsidian rejects it (round 4)
    ("classification: public   # reviewed", "public"),
    ("classification: 'public' # reviewed", "public"),
    # Another spelling of the key is unreadable (round 3): still never public.
    ("Classification: restricted\nclassification: public",
     "(frontmatter the gate cannot read reliably)"),
])
def test_every_declared_value_counts_not_the_first(audit, block: str, expected: str) -> None:
    """A `public` line above a real tier used to pass this audit, and the note
    went into a public commit. Adversarial review, 2026-09-25."""
    assert audit.parse_classification(f"---\n{block}\n---\nbody\n") == expected


@pytest.mark.parametrize("fm", ['{"classification": "public"}',
                                '"classification": public',
                                '{"classification": "restricted", "x": {\nclassification: public\n}}'])
def test_frontmatter_the_reader_cannot_read_never_passes_as_public(audit, fm: str) -> None:
    assert audit.parse_classification(f"---\n{fm}\n---\nbody\n") != "public"


# ---------------------------------------------------------------------------
# Paths the gate used to drop (M-DASH re-verification, findings 62/63/65).
#
# Each of these let a confidential note through while the audit printed a
# clean summary. They run the real script against a real throwaway checkout,
# because the failures lived in how git output was parsed and how paths were
# pruned -- nothing a mock of git would reproduce.
# ---------------------------------------------------------------------------

CONFIDENTIAL = "---\ntitle: real person\nclassification: confidential\n---\nbody\n"
PUBLIC = "---\ntitle: demo\nclassification: public\n---\nbody\n"
ACCENTED = Path("People") / "José García.md"


@pytest.fixture
def content_repo(tmp_path: Path, allow_subprocess: None) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # Pin git's default so a global core.quotePath=false on the test machine
    # cannot hide the quoting this guards against.
    subprocess.run(["git", "config", "core.quotePath", "true"],
                   cwd=tmp_path, check=True)
    for folder in ("People", "Knowledge"):
        (tmp_path / folder).mkdir()
    return tmp_path


def _write(root: Path, rel: Path | str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return Path(rel)


def _run_audit(root: Path, *flags: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(AUDIT_PY), "--repo-root", str(root), *flags],
        capture_output=True, text=True, encoding="utf-8")


class TestNonAsciiPathsAreAudited:
    """#65: without -z, git C-quoted a non-ASCII staged path, the quoted
    string named no file, and the note was dropped -- '0 file(s) audited'."""

    def test_staged_accented_confidential_note_is_refused(
            self, content_repo: Path, allow_subprocess: None) -> None:
        _write(content_repo, ACCENTED, CONFIDENTIAL)
        subprocess.run(["git", "add", "--", ACCENTED.as_posix()],
                       cwd=content_repo, check=True)
        proc = _run_audit(content_repo, "--staged")
        assert proc.returncode == 1, proc.stderr
        assert f"VIOLATION  {ACCENTED}" in proc.stderr
        assert "1 file(s) audited, 1 violation(s)" in proc.stderr

    def test_staged_list_names_the_real_file(
            self, audit, content_repo: Path, allow_subprocess: None) -> None:
        _write(content_repo, ACCENTED, CONFIDENTIAL)
        subprocess.run(["git", "add", "--", ACCENTED.as_posix()],
                       cwd=content_repo, check=True)
        assert audit.git_staged_files(content_repo) == [ACCENTED]

    def test_full_mode_flags_accented_confidential_note(
            self, content_repo: Path, allow_subprocess: None) -> None:
        _write(content_repo, ACCENTED, CONFIDENTIAL)
        subprocess.run(["git", "add", "--", ACCENTED.as_posix()],
                       cwd=content_repo, check=True)
        proc = _run_audit(content_repo)
        assert proc.returncode == 1, proc.stderr
        assert f"VIOLATION  {ACCENTED}" in proc.stderr

    def test_staged_public_note_still_passes(
            self, content_repo: Path, allow_subprocess: None) -> None:
        _write(content_repo, ACCENTED, PUBLIC)
        _write(content_repo, "Knowledge/plain.md", PUBLIC)
        subprocess.run(["git", "add", "-A"], cwd=content_repo, check=True)
        proc = _run_audit(content_repo, "--staged")
        assert proc.returncode == 0, proc.stderr
        assert "2 file(s) audited, 0 violation(s)" in proc.stderr

    def test_staged_file_missing_from_disk_is_a_violation(
            self, content_repo: Path, allow_subprocess: None) -> None:
        """Staged, then deleted from the working tree: it is still being
        committed, so it must not quietly fall out of the audit."""
        rel = _write(content_repo, ACCENTED, CONFIDENTIAL)
        subprocess.run(["git", "add", "--", rel.as_posix()],
                       cwd=content_repo, check=True)
        (content_repo / rel).unlink()
        proc = _run_audit(content_repo, "--staged")
        assert proc.returncode == 1, proc.stderr
        assert f"VIOLATION  {rel}  could not read file" in proc.stderr


class TestOnlyTheRootReadmeIsExempt:
    """#63: README.md was exempt at any depth, not just at the root."""

    def test_nested_readme_is_audited(self, audit) -> None:
        assert audit.should_audit(Path("Knowledge/README.md")) is True
        assert audit.should_audit(Path("People/README.md")) is True

    def test_root_readme_is_still_skipped(self, audit) -> None:
        assert audit.should_audit(Path("README.md")) is False

    def test_staged_nested_readme_confidential_is_refused(
            self, content_repo: Path, allow_subprocess: None) -> None:
        rel = _write(content_repo, "Knowledge/README.md", CONFIDENTIAL)
        _write(content_repo, "README.md", "# template\n")
        subprocess.run(["git", "add", "-A"], cwd=content_repo, check=True)
        proc = _run_audit(content_repo, "--staged")
        assert proc.returncode == 1, proc.stderr
        assert f"VIOLATION  {rel}" in proc.stderr
        assert "1 file(s) audited, 1 violation(s)" in proc.stderr


class TestSkippedFolderNamesOnlyAtTopLevel:
    """#62: a folder named docs/Templates/installers deeper in the tree was
    pruned from full mode, so Knowledge/docs/x.md was never audited."""

    @pytest.mark.parametrize("rel", ["Knowledge/docs/x.md",
                                     "People/Templates/x.md",
                                     "Notes/installers/x.md"])
    def test_nested_namesake_folder_is_audited_in_full_mode(
            self, content_repo: Path, rel: str,
            allow_subprocess: None) -> None:
        _write(content_repo, rel, CONFIDENTIAL)
        proc = _run_audit(content_repo)
        assert proc.returncode == 1, proc.stderr
        assert f"VIOLATION  {Path(rel)}" in proc.stderr

    def test_top_level_skipped_folders_are_still_skipped(
            self, audit, content_repo: Path, allow_subprocess: None) -> None:
        for rel in ("docs/guide.md", "Templates/t.md", "installers/i.md"):
            _write(content_repo, rel, CONFIDENTIAL)
        kept = _write(content_repo, "Knowledge/docs/x.md", PUBLIC)
        found = audit.all_md_files(content_repo)
        assert found == [kept]
        proc = _run_audit(content_repo)
        assert proc.returncode == 0, proc.stderr
        assert "1 file(s) audited, 0 violation(s)" in proc.stderr
