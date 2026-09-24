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
