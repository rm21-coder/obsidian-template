"""
test_group_photos.py -- the nightly group-photos pair, from its new home.

The two scripts moved from Z_attachments/ to Templates/Scripts/ on 2026-10-20
(finding 5278622) and now find the vault two levels above themselves. These
tests lay out a throwaway vault, copy the scripts and the Windows runner into
its Templates/Scripts, and run the runner the way the scheduled task does:
no OBSIDIAN_VAULT, every step under -I.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
FILES = ("run_group_photos.py", "insert_group_placeholders.py", "refresh_groups.py")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    dest = v / "Templates" / "Scripts"
    dest.mkdir(parents=True)
    for name in FILES:
        shutil.copy2(SCRIPTS / name, dest / name)
    (v / "Groups").mkdir()
    (v / "Z_attachments").mkdir()
    (v / "Z_attachments" / "Doe-Jane.png").write_bytes(b"\x89PNG")
    (v / "Groups" / "Team.md").write_text(
        "# Team\n[[Doe, Jane]]\n[[Roe, Rick]]\n![[Meetings.base#Group]]\n")
    return v


def _run(vault: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "OBSIDIAN_VAULT"}
    return subprocess.run(
        [sys.executable, "-I", str(vault / "Templates" / "Scripts" / "run_group_photos.py"), *args],
        cwd=str(vault.parent), env=env, capture_output=True, text=True, timeout=60)


def test_the_moved_scripts_find_the_vault_from_templates_scripts(
        vault: Path, allow_subprocess) -> None:
    r = _run(vault)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (vault / "Groups" / "Team.md").read_text() == (
        "# Team\n"
        "![[Doe-Jane.png|40]] [[Doe, Jane]]\n"
        "![[placeholder-person.png|40]] [[Roe, Rick]]\n"
        "![[Meetings.base#Group]]\n")
    assert f"Indexed 1 photo(s) in {vault / 'Z_attachments'}" in r.stdout


def test_a_module_planted_beside_the_scripts_is_not_imported(
        vault: Path, allow_subprocess) -> None:
    """-I keeps the script's own folder off sys.path: a planted re.py there
    would otherwise shadow the standard library in both steps."""
    marker = vault / "PWNED"
    (vault / "Templates" / "Scripts" / "re.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\nraise SystemExit(9)\n")
    r = _run(vault)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not marker.exists()


def test_a_missing_vault_folder_stops_the_pipeline(
        vault: Path, allow_subprocess) -> None:
    shutil.rmtree(vault / "Groups")
    r = _run(vault)
    assert r.returncode == 1
    assert "ERROR: expected vault dirs missing under" in r.stderr
    assert "ERROR: insert_group_placeholders.py exited 1" in r.stderr
