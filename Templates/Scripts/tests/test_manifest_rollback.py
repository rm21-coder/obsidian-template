"""
test_manifest_rollback.py -- a rollback manifest must not choose where it writes.

merge_tags, tag_clippings_rag and vault_lint restore notes from a JSON
manifest. Before 2026-09-24 each wrote every entry's "original" to its "path"
unchecked (M-DASH, CWE-73). The case that matters most is not the one M-DASH
named: a target INSIDE the vault that is not a note -- the automation's own
scripts live there, so "under the vault root" alone would have let a planted
manifest overwrite a scheduled script.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import manifest_rollback as mr


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    (v / "Notes").mkdir(parents=True)
    (v / "Templates" / "Scripts").mkdir(parents=True)
    (v / "Notes" / "a.md").write_text("changed\n")
    (v / "Templates" / "Scripts" / "job.py").write_text("print('real')\n")
    return v


def _manifest(tmp_path: Path, changes) -> Path:
    m = tmp_path / "manifest.json"
    m.write_text(json.dumps({"changes": changes}))
    return m


def test_restores_a_note(vault: Path, tmp_path: Path) -> None:
    note = vault / "Notes" / "a.md"
    m = _manifest(tmp_path, [{"path": str(note), "original": "before\n"}])
    assert mr.apply_rollback(m, vault) == 1
    assert note.read_text() == "before\n"


@pytest.mark.parametrize("target", [
    "../outside.md",                     # escapes the vault
    "/etc/evil.md",                      # absolute, elsewhere
    "Templates/Scripts/job.py",          # INSIDE the vault, but a script
    "Notes/a.md.py",                     # disguised suffix
])
def test_unsafe_target_is_refused(vault: Path, tmp_path: Path, target: str) -> None:
    path = str(vault / target) if not target.startswith("/") else target
    m = _manifest(tmp_path, [{"path": path, "original": "pwned\n"}])
    with pytest.raises(mr.UnsafeManifest):
        mr.apply_rollback(m, vault)
    assert (vault / "Templates" / "Scripts" / "job.py").read_text() == "print('real')\n"


def test_one_bad_entry_refuses_the_whole_manifest(vault: Path, tmp_path: Path) -> None:
    """All-or-nothing: a good entry listed first is NOT written."""
    note = vault / "Notes" / "a.md"
    m = _manifest(tmp_path, [
        {"path": str(note), "original": "before\n"},
        {"path": str(vault / "Templates/Scripts/job.py"), "original": "pwned\n"},
    ])
    with pytest.raises(mr.UnsafeManifest):
        mr.apply_rollback(m, vault)
    assert note.read_text() == "changed\n", "a partial rollback was applied"


@pytest.mark.parametrize("bad", [
    {"changes": [{"path": 5, "original": "x"}]},
    {"changes": [{"path": "Notes/a.md"}]},
    {"changes": "not a list"},
    {},
    [],
])
def test_malformed_manifest_is_refused(vault: Path, tmp_path: Path, bad) -> None:
    m = tmp_path / "m.json"
    m.write_text(json.dumps(bad))
    with pytest.raises(mr.UnsafeManifest):
        mr.apply_rollback(m, vault)


# ---------------------------------------------------------------------------
# Adversarial review of the M-DASH fix, 2026-09-25.

@pytest.mark.parametrize("rel", [
    "Templates/Meeting Template.md",     # Templater: runs JS when applied
    "CLAUDE.md",                          # instructs agent sessions with a shell
    "Notes/AGENTS.md",
    ".obsidian/snippets/x.md",            # configuration, not a note
    "Notes/.hidden/x.md",
])
def test_markdown_that_is_not_a_note_is_refused(vault: Path, tmp_path: Path, rel: str) -> None:
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("real\n")
    m = _manifest(tmp_path, [{"path": str(target),
                              "original": "<%* require('child_process').exec('x') %>"}])
    with pytest.raises(mr.UnsafeManifest):
        mr.apply_rollback(m, vault)
    assert target.read_text() == "real\n"


def test_a_rollback_never_creates_a_file(vault: Path, tmp_path: Path) -> None:
    new = vault / "Notes" / "new.md"
    m = _manifest(tmp_path, [{"path": str(new), "original": "x"}])
    with pytest.raises(mr.UnsafeManifest, match="does not exist"):
        mr.apply_rollback(m, vault)
    assert not new.exists()


def test_hard_link_to_a_file_outside_the_vault_is_refused(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    link = vault / "Notes" / "linked.md"
    os.link(outside, link)
    m = _manifest(tmp_path, [{"path": str(link), "original": "pwned"}])
    with pytest.raises(mr.UnsafeManifest, match="hard links"):
        mr.apply_rollback(m, vault)
    assert outside.read_text() == "keep\n"


def test_directory_named_like_a_note_refuses_everything(vault: Path, tmp_path: Path) -> None:
    """Used to write the first entry and then crash with IsADirectoryError."""
    note = vault / "Notes" / "a.md"
    (vault / "Notes" / "dir.md").mkdir()
    m = _manifest(tmp_path, [{"path": str(note), "original": "before\n"},
                             {"path": str(vault / "Notes" / "dir.md"), "original": "x"}])
    with pytest.raises(mr.UnsafeManifest):
        mr.apply_rollback(m, vault)
    assert note.read_text() == "changed\n"


def test_io_error_while_staging_writes_nothing(vault: Path, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    a = vault / "Notes" / "a.md"
    b = vault / "Notes" / "b.md"; b.write_text("changed-b\n")
    real = mr.tempfile.mkstemp
    calls = {"n": 0}
    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real(**kw)
    monkeypatch.setattr(mr.tempfile, "mkstemp", flaky)
    m = _manifest(tmp_path, [{"path": str(a), "original": "A"}, {"path": str(b), "original": "B"}])
    with pytest.raises(OSError):
        mr.apply_rollback(m, vault)
    assert a.read_text() == "changed\n" and b.read_text() == "changed-b\n"
    assert not list((vault / "Notes").glob(".rollback-*")), "temp files left behind"


def test_relative_path_resolves_against_the_vault_not_the_cwd(
        vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Notes").mkdir()
    decoy = tmp_path / "Notes" / "a.md"; decoy.write_text("decoy\n")
    m = _manifest(tmp_path, [{"path": "Notes/a.md", "original": "before\n"}])
    assert mr.apply_rollback(m, vault) == 1
    assert (vault / "Notes" / "a.md").read_text() == "before\n"
    assert decoy.read_text() == "decoy\n"
