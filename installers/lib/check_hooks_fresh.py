#!/usr/bin/env python3
"""check_hooks_fresh.py -- are the installed git hooks the repository's own?

install-git-hooks.sh installs the hooks as copies, and a copy does not follow
the repository. The maintainer's pre-push hook was a 2026-09-25 copy until
2026-10-04, so the per-commit push gates added in between had never run on a
push, and nothing said so. This compares every hook in installers/lib/hooks/
with the one git will actually run, byte for byte, and checks that git can
execute it.

Exit 0: every hook is installed and current. Exit 1: one or more is missing,
stale or not executable (each named on stdout). Exit 2: not a git checkout.

Usage:
    installers/lib/check_hooks_fresh.py [--repo-root PATH]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

FIX = "run ./installers/install-git-hooks.sh"


def installed_hooks_dir(root: Path) -> Path | None:
    """The directory git runs hooks from. `--git-path hooks` honours
    core.hooksPath and resolves a linked worktree to the shared hooks."""
    p = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
                       capture_output=True, text=True, check=False)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    hooks = Path(p.stdout.strip())
    return hooks if hooks.is_absolute() else root / hooks


def check(root: Path) -> tuple[int, list[str]]:
    hooks = installed_hooks_dir(root)
    if hooks is None:
        return 2, ["not a git checkout"]
    sources = sorted(p for p in (root / "installers" / "lib" / "hooks").iterdir()
                     if p.is_file())
    if not sources:
        return 1, ["no hook sources found in installers/lib/hooks"]
    problems = []
    for src in sources:
        dst = hooks / src.name
        if not dst.is_file():
            problems.append(f"{src.name}: not installed -- {FIX}")
        elif dst.read_bytes() != src.read_bytes():
            problems.append(f"{src.name}: installed copy differs from the repository's -- {FIX}")
        elif os.name != "nt" and not os.access(dst, os.X_OK):
            problems.append(f"{src.name}: installed but not executable, so git skips it -- {FIX}")
    if problems:
        return 1, problems
    return 0, [f"{len(sources)} hook(s) installed and current: "
               + ", ".join(s.name for s in sources)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--repo-root", default=None)
    args = ap.parse_args()
    if args.repo_root:
        root = Path(args.repo_root).resolve()
    else:
        p = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, check=False)
        if p.returncode != 0:
            print("not a git checkout")
            return 2
        root = Path(p.stdout.strip())
    code, lines = check(root)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
