#!/usr/bin/env python3
"""exe_lookup.py -- find an external program by name on PATH, and only there.

    from exe_lookup import find_executable
    ffmpeg = find_executable("ffmpeg")      # absolute path, or None

Why not shutil.which: on Windows it looks in the current directory before
PATH (3.10 and 3.11 always; 3.12+ unless NoDefaultCurrentDirectoryInExePath
is set), and it honours PATHEXT, so a `claude.cmd` or `ffprobe.bat` beside
the scripts wins over the real program. A bare name handed to subprocess is
worse: CreateProcess searches the interpreter's folder and the current
directory first. Every scheduled job used to start with the vault's
Templates/Scripts as its working directory, and the vault is a synced,
shared folder, so a planted file there ran inside the job (findings 5278618,
5278616, 5277585).

This searches the PATH entries alone, in order, and skips:
  - any entry that is not an absolute path ("", ".", "bin", ...), which a
    process would otherwise resolve against wherever it happens to stand;
  - the current working directory, however PATH spells it;
  - the vault and anything inside it (this file's Templates/Scripts/..);
  - anything the caller adds through `exclude`.
On Windows a name without an extension is tried with each PATHEXT extension
(.exe, .cmd, ...), as the shell does; elsewhere the file must be executable.

Stdlib only and Python 3.9-compatible: Apple's /usr/bin/python3 jobs import
it as well as the venv's.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# <vault>/Templates/Scripts/exe_lookup.py -> <vault>
VAULT_ROOT = Path(__file__).resolve().parent.parent.parent

_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"


def _resolved(p) -> Path | None:
    try:
        return Path(p).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _candidates(name: str) -> list:
    if sys.platform != "win32":
        return [name]
    exts = [e for e in (os.environ.get("PATHEXT") or _DEFAULT_PATHEXT).split(";") if e]
    if os.path.splitext(name)[1].lower() in (e.lower() for e in exts):
        return [name]
    return [name + e.lower() for e in exts]


def _usable(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    return sys.platform == "win32" or os.access(str(path), os.X_OK)


def find_executable(name: str, *, exclude=(), path: str | None = None) -> str | None:
    """Absolute path of `name` from the PATH entries alone, or None.

    `name` must be a bare program name: a value with a directory part is a
    caller's mistake (it would bypass the search), so it raises ValueError.
    `path` stands in for $PATH (tests); `exclude` adds folders to skip, with
    everything beneath them.
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError("find_executable wants a bare program name, not %r" % (name,))
    search = os.environ.get("PATH", "") if path is None else path
    cwd = _current_dir()
    skip_trees = [r for r in (_resolved(VAULT_ROOT),) if r is not None]
    skip_trees += [r for r in (_resolved(e) for e in exclude) if r is not None]
    names = _candidates(name)
    for raw in search.split(os.pathsep):
        entry = raw.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        folder = _resolved(entry)
        if folder is None or folder == cwd:
            continue
        if any(_within(folder, root) for root in skip_trees):
            continue
        for candidate in names:
            full = folder / candidate
            if _usable(full):
                return str(full)
    return None


def _current_dir() -> Path | None:
    try:
        return _resolved(os.getcwd())
    except OSError:          # the working directory was deleted under us
        return None
