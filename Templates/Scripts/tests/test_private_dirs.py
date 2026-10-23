"""N11 (2026-10-23): on Windows, both security controls stopped starting.

`os.mkdir(mode=0o700)` on Windows (Python 3.12.4+) writes a protected ACL that
grants only the folder's OWNER, SYSTEM and Administrators; created from an
elevated prompt, the owner is the Administrators group, so the user's own
scheduled task could not list %LOCALAPPDATA%\\obsidian-security\\bin and the
installed integrity monitor failed at `import security_common` -- exit 1, the
same code as drift, with no alert. These tests hold the three fixes: folders
are made owner-only without that mode on Windows and checked listable; a
control that cannot import its helper says so and exits 3; and the updaters
report an unrunnable control instead of calling it expected drift.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import security_common as sc
from platform_caps import POSIX_MODES

SCRIPTS = Path(__file__).resolve().parent.parent
REPO = SCRIPTS.parent.parent


@pytest.mark.skipif(not POSIX_MODES, reason="POSIX modes only")
def test_make_private_dir_is_0700_on_posix(tmp_path):
    d = tmp_path / "a" / "b"
    sc.make_private_dir(d, parents=True)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_make_private_dir_on_windows_grants_the_user_and_never_passes_0700(tmp_path, monkeypatch):
    seen = {"mkdir_modes": [], "icacls": []}
    real_mkdir = Path.mkdir

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        seen["mkdir_modes"].append(mode)
        return real_mkdir(self, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(sc.sys, "platform", "win32")
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setenv("USERNAME", "tester")
    monkeypatch.setenv("USERDOMAIN", "BOX")
    monkeypatch.setattr(sc.subprocess, "run",
                        lambda argv, **kw: seen["icacls"].append(argv) or subprocess.CompletedProcess(argv, 0))
    sc.make_private_dir(tmp_path / "bin")
    assert seen["mkdir_modes"] == [0o777], "Windows must not get mode=0o700"
    argv = seen["icacls"][0]
    assert argv[0] == sc.ICACLS_EXE and "/inheritance:r" in argv
    assert "BOX\\tester:(OI)(CI)(F)" in argv
    assert f"{sc._SYSTEM_SID}:(OI)(CI)(F)" in argv


def test_an_unlistable_folder_fails_loudly_with_the_remedy(tmp_path, monkeypatch):
    def listdir(path):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(sc.os, "listdir", listdir)
    with pytest.raises(OSError, match="this account cannot list it.*elevated"):
        sc.make_private_dir(tmp_path / "bin")


def _mkdir_0700_calls(path: Path):
    """(enclosing function, line) of every mkdir(mode=0o700) call in code --
    comments and strings do not count."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "mkdir"):
                for kw in child.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and kw.value.value == 0o700:
                        found.append((func, child.lineno))
            walk(child, name)
    walk(tree, None)
    return found


def test_install_controls_builds_its_folders_with_make_private_dir():
    src = (SCRIPTS / "security_common.py").read_text(encoding="utf-8")
    assert "make_private_dir(dest)" in src[src.index("def install_controls("):]
    assert [f for f, _ in _mkdir_0700_calls(SCRIPTS / "security_common.py")] == ["make_private_dir"]


def test_no_shipped_script_passes_mode_0o700_to_mkdir_outside_make_private_dir():
    offenders = []
    for path in sorted(SCRIPTS.glob("*.py")) + sorted((SCRIPTS / "windows").glob("*.py")):
        for func, line in _mkdir_0700_calls(path):
            if not (path.name == "security_common.py" and func == "make_private_dir"):
                offenders.append(f"{path.name}:{line}")
    assert not offenders, "use make_private_dir or _PRIVATE_DIR_MODE: " + ", ".join(offenders)


@pytest.mark.parametrize("control", ["integrity_monitor.py", "plugin_integrity_check.py"])
def test_a_control_that_cannot_import_its_helper_exits_3(control, tmp_path, allow_subprocess):
    shutil.copy2(SCRIPTS / control, tmp_path / control)       # security_common left behind
    r = subprocess.run([sys.executable, "-I", str(tmp_path / control), "--json"],
                       capture_output=True, text=True, cwd=str(tmp_path))
    assert r.returncode == 3
    assert "CONTROL_ERROR: cannot import security_common from" in r.stderr
    assert "The control did not run." in r.stderr


def test_both_updaters_report_an_unrunnable_control_as_an_error():
    sh = (REPO / "update.sh").read_text(encoding="utf-8")
    assert "unreadable)" in sh and "the control could not run, so nothing is being checked." in sh
    ps = (SCRIPTS / "windows" / "update.ps1").read_text(encoding="ascii")
    assert "} elseif ($status -eq 'unreadable') {" in ps
    assert "The integrity monitor could not run, so nothing is being checked." in ps
