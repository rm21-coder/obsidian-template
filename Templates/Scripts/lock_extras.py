#!/usr/bin/env python3
"""lock_extras.py -- packages in this venv that its lock does not name.

The installers install a venv only from a hash-pinned lock
(requirements.lock), reinstalling every locked package on each run so its
files are re-checked. They do not remove what the lock no longer names: a
dependency an older install pulled in, or a tool installed by hand (pytest,
for one). Those were never hash-checked and are not audited. This lists
them, so the installer can say so. Only rebuilding the venv clears them --
and clears files no package owns (a stray .pth, say), which a reinstall
leaves in place and this cannot see.

Run with the venv's own interpreter:
    <venv python> lock_extras.py <lock file>
Prints one name per line; exit 0 always (it informs, it does not gate).
"""
from __future__ import annotations

import re
import sys
from importlib import metadata

# Installed by venv itself, not by the lock.
VENV_OWN = {"pip"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_here(lock_text: str) -> set[str]:
    """Names the lock installs on THIS interpreter: an entry counts only if
    its marker holds here (a win32-only tzdata is not 'locked' on a Mac).
    Without `packaging` (it is in the lock, so normally present) every entry
    counts -- under-reporting, never a false alarm."""
    try:
        from packaging.markers import Marker
    except ImportError:
        Marker = None
    names = set()
    for m in re.finditer(r"(?m)^([A-Za-z0-9][A-Za-z0-9_.-]*)==[^\s;\\]+\s*(?:;\s*([^\\\n]*?))?\s*\\?$",
                         lock_text):
        marker = (m.group(2) or "").strip()
        if marker and Marker is not None:
            try:
                if not Marker(marker).evaluate():
                    continue
            except Exception:
                pass
        names.add(_norm(m.group(1)))
    return names


def extras(lock_text: str) -> list[str]:
    installed = {_norm(d.metadata["Name"]) for d in metadata.distributions()
                 if d.metadata["Name"]}
    return sorted(installed - _locked_here(lock_text) - VENV_OWN)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        return 0
    try:
        text = open(sys.argv[1], encoding="utf-8").read()
    except OSError as exc:
        print(f"lock_extras: cannot read {sys.argv[1]}: {exc}", file=sys.stderr)
        return 0
    for name in extras(text):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
