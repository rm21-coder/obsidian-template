"""
test_exe_lookup.py -- external programs come from PATH, never from the
working directory or the vault (findings 5278618, 5278616, 5277585).

shutil.which on Windows checks the current directory before PATH and
honours PATHEXT, and every scheduled task started in the vault's
Templates\\Scripts, so a planted claude.cmd or ffprobe.bat there won. These
pin the replacement: only absolute PATH entries, minus the working
directory, minus the vault, in PATH order, with PATHEXT on Windows.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

import exe_lookup


def _tool(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text("#!/bin/sh\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(exe_lookup.sys, "platform", "darwin")


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(exe_lookup.sys, "platform", "win32")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")


def test_finds_a_tool_on_path(posix, tmp_path):
    real = _tool(tmp_path / "bin", "ffprobe")
    assert exe_lookup.find_executable("ffprobe", path=str(tmp_path / "bin")) == str(real.resolve())


def test_returns_none_when_absent(posix, tmp_path):
    (tmp_path / "bin").mkdir()
    assert exe_lookup.find_executable("ffprobe", path=str(tmp_path / "bin")) is None


def test_the_working_directory_is_never_searched(posix, tmp_path, monkeypatch):
    """The plant sits in the cwd and the cwd is not even on PATH: shutil.which
    on Windows would still have found it."""
    planted = tmp_path / "cwd"
    _tool(planted, "claude")
    real = _tool(tmp_path / "bin", "claude")
    monkeypatch.chdir(planted)
    assert exe_lookup.find_executable("claude", path=str(tmp_path / "bin")) == str(real.resolve())
    assert exe_lookup.find_executable("claude", path="") is None


def test_the_working_directory_is_skipped_even_when_on_path(posix, tmp_path, monkeypatch):
    planted = tmp_path / "cwd"
    _tool(planted, "claude")
    real = _tool(tmp_path / "bin", "claude")
    monkeypatch.chdir(planted)
    search = os.pathsep.join([str(planted), str(tmp_path / "bin")])
    assert exe_lookup.find_executable("claude", path=search) == str(real.resolve())


@pytest.mark.parametrize("entry", ["", ".", "bin", "./bin", "..", '"."'])
def test_relative_path_entries_are_ignored(posix, tmp_path, monkeypatch, entry):
    _tool(tmp_path / "bin", "ffmpeg")
    _tool(tmp_path, "ffmpeg")
    monkeypatch.chdir(tmp_path)
    assert exe_lookup.find_executable("ffmpeg", path=entry) is None


def test_the_vault_and_everything_in_it_is_skipped(posix, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    inside = vault / "Templates" / "Scripts"
    _tool(inside, "ffmpeg")
    _tool(vault, "ffmpeg")
    real = _tool(tmp_path / "bin", "ffmpeg")
    monkeypatch.setattr(exe_lookup, "VAULT_ROOT", vault)
    search = os.pathsep.join([str(inside), str(vault), str(tmp_path / "bin")])
    assert exe_lookup.find_executable("ffmpeg", path=search) == str(real.resolve())


def test_the_real_vault_root_is_this_checkout():
    assert (exe_lookup.VAULT_ROOT / "Templates" / "Scripts" / "exe_lookup.py").is_file()


def test_extra_excludes_are_skipped(posix, tmp_path):
    _tool(tmp_path / "sync", "ffmpeg")
    real = _tool(tmp_path / "bin", "ffmpeg")
    search = os.pathsep.join([str(tmp_path / "sync"), str(tmp_path / "bin")])
    assert exe_lookup.find_executable("ffmpeg", path=search,
                                      exclude=[tmp_path / "sync"]) == str(real.resolve())


def test_path_order_is_kept(posix, tmp_path):
    first = _tool(tmp_path / "a", "ffmpeg")
    _tool(tmp_path / "b", "ffmpeg")
    search = os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])
    assert exe_lookup.find_executable("ffmpeg", path=search) == str(first.resolve())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX execute bit")
def test_a_non_executable_file_is_not_a_match(posix, tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "ffmpeg").write_text("x", encoding="utf-8")
    assert exe_lookup.find_executable("ffmpeg", path=str(tmp_path / "bin")) is None


def test_a_directory_is_not_a_match(posix, tmp_path):
    (tmp_path / "bin" / "ffmpeg").mkdir(parents=True)
    assert exe_lookup.find_executable("ffmpeg", path=str(tmp_path / "bin")) is None


@pytest.mark.parametrize("name", ["", "./claude", "bin/claude", "..\\claude", "C:\\x\\claude", ".", ".."])
def test_a_name_with_a_directory_part_is_refused(name):
    with pytest.raises(ValueError, match="wants a bare program name"):
        exe_lookup.find_executable(name, path="")


def test_windows_tries_pathext(windows, tmp_path):
    real = _tool(tmp_path / "npm", "claude.cmd")
    _tool(tmp_path / "npm", "claude")          # npm's sh shim: not runnable there
    assert exe_lookup.find_executable("claude", path=str(tmp_path / "npm")) == str(real.resolve())


def test_windows_prefers_pathext_order(windows, tmp_path):
    exe = _tool(tmp_path / "bin", "ffprobe.exe")
    _tool(tmp_path / "bin", "ffprobe.bat")
    assert exe_lookup.find_executable("ffprobe", path=str(tmp_path / "bin")) == str(exe.resolve())


def test_windows_a_name_with_its_extension_is_taken_as_is(windows, tmp_path):
    real = _tool(tmp_path / "bin", "ffmpeg.exe")
    assert exe_lookup.find_executable("ffmpeg.exe", path=str(tmp_path / "bin")) == str(real.resolve())


def test_windows_a_planted_cmd_in_the_cwd_is_not_found(windows, tmp_path, monkeypatch):
    """The reported case: claude.cmd beside the scripts, the scripts folder
    as the working directory, the real CLI under %APPDATA%\\npm."""
    scripts = tmp_path / "vault" / "Templates" / "Scripts"
    _tool(scripts, "claude.cmd")
    real = _tool(tmp_path / "npm", "claude.cmd")
    monkeypatch.setattr(exe_lookup, "VAULT_ROOT", tmp_path / "vault")
    monkeypatch.chdir(scripts)
    assert exe_lookup.find_executable("claude", path=str(tmp_path / "npm")) == str(real.resolve())
    assert exe_lookup.find_executable("claude", path=os.pathsep.join([".", str(scripts)])) is None


@pytest.mark.parametrize("module,needle", [
    ("podcast_transcribe.py", 'exe_lookup.find_executable("ffprobe")'),
    ("whisper_onnx.py", 'exe_lookup.find_executable("ffmpeg")'),
    ("meeting_pull.py", 'exe_lookup.find_executable("claude")'),
    ("claude_auth_check.py", 'exe_lookup.find_executable("claude")'),
])
def test_every_call_site_uses_the_helper(module, needle):
    src = (Path(exe_lookup.__file__).with_name(module)).read_text(encoding="utf-8")
    assert needle in src
    assert 'shutil.which("claude")' not in src
    assert 'shutil.which("ffmpeg")' not in src
    assert '["ffprobe",' not in src
