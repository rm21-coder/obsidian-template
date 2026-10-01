#!/usr/bin/env python3
"""lock_requirements.py — (re)generate the hash-pinned requirement locks.

A requirements file says WHAT is needed; its lock says exactly which release
of every package, dependencies of dependencies included, and the SHA256 of
every file PyPI publishes for it. The installers install only from locks,
with pip's --require-hashes, --no-deps and --only-binary, so a compromised or
merely surprising upstream release cannot reach an adopter's venv until a
maintainer reruns this tool and commits the diff -- the same review moment
installers/lib/pin_plugins.py creates for plugins.

Two locks:
  Templates/Scripts/requirements.txt         -> requirements.lock          (the vault venv)
  Templates/Scripts/requirements-dropper.txt -> requirements-dropper.lock  (Markitdown Dropper app)

Each lock is a universal resolve (uv), so one file serves every supported
platform. --only-binary closes the gap --require-hashes leaves open: a source
build fetches its build tools unhashed. So every pin must have a wheel for
every supported target, and this tool refuses to write a lock that does not.

Hashes always come from the index, never from the old lock: versions are
chosen first (keeping existing pins where they still satisfy the
requirements), then resolved again from scratch with those versions as
constraints, and that second resolve's hashes are what get written -- and
what --check compares the committed lock against.

Usage (maintainer, with network; needs uv:  pipx install uv):
    python3 installers/lib/lock_requirements.py              # re-lock; keeps pins that still resolve
    python3 installers/lib/lock_requirements.py --upgrade    # move every pin to its latest release
    python3 installers/lib/lock_requirements.py --check      # verify both locks; write nothing
    python3 installers/lib/lock_requirements.py --audit-sets DIR   # pip-audit inputs (no network)
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "Templates" / "Scripts"
INDEX = "https://pypi.org/simple"
MACOS_FLOOR = "14.0"             # av publishes macOS 14+ wheels only
COMPILE_COMMAND = "python3 installers/lib/lock_requirements.py"

# Every (platform, Python) an install can run on. Windows ARM64 starts at
# 3.12: pyyaml publishes no win_arm64 wheel for 3.11, and the Windows
# installer refuses 3.11 there for that reason. Intel Macs are not a target:
# onnxruntime (pulled in by markitdown's magika) stopped shipping them. The
# installers refuse any Python above the highest version listed here.
TARGETS = [
    ("aarch64-apple-darwin", ("3.10", "3.11", "3.12", "3.13", "3.14")),
    ("x86_64-pc-windows-msvc", ("3.10", "3.11", "3.12", "3.13", "3.14")),
    ("aarch64-pc-windows-msvc", ("3.12", "3.13", "3.14")),
]
PYTHON_FLOOR = "3.10"            # PEP 604 unions in the scripts
PYTHON_CEILING = "3.14"


class LockSet:
    def __init__(self, req: str, lock: str, floor: str, targets: list) -> None:
        self.req = SCRIPTS / req
        self.lock = SCRIPTS / lock
        self.floor = floor
        self.targets = targets


# The Markitdown Dropper is a macOS-only PySide6 app with its own venv, built
# by installers/components/43-markitdown-dropper.sh on Homebrew python3.13.
LOCKSETS = [
    LockSet("requirements.txt", "requirements.lock", PYTHON_FLOOR, TARGETS),
    LockSet("requirements-dropper.txt", "requirements-dropper.lock", "3.13",
            [("aarch64-apple-darwin", ("3.13",))]),
]
MAIN = LOCKSETS[0]
LOCK = MAIN.lock                 # kept for callers and tests

_PIN = re.compile(r"(?m)^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;\\]+)")


def _uv() -> str:
    for cand in (shutil.which("uv"), str(Path.home() / ".local" / "bin" / "uv")):
        if cand and os.access(cand, os.X_OK):
            return cand
    sys.exit("uv not found - install it with:  pipx install uv")


def _env(extra: dict | None = None) -> dict:
    """The caller's environment minus every UV_* setting: an extra index, a
    find-links folder or an exclude-newer date in the maintainer's shell
    would otherwise decide which files the lock trusts."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("UV_")}
    env.update(extra or {})
    return env


def _run(cmd: list[str], extra_env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                          env=_env(extra_env))


def _compile(ls: LockSet, out: Path, *, seed: Path | None = None,
             constraints: Path | None = None, upgrade: bool = False) -> None:
    if seed is not None and seed.exists():
        shutil.copyfile(seed, out)
    cmd = [_uv(), "pip", "compile", str(ls.req.relative_to(REPO_ROOT)),
           "--universal", "--python-version", ls.floor,
           "--generate-hashes", "--no-config", "--default-index", INDEX,
           "--custom-compile-command", COMPILE_COMMAND, "-q", "-o", str(out)]
    if constraints is not None:
        cmd += ["-c", str(constraints)]
    if upgrade:
        cmd.append("--upgrade")
    p = _run(cmd)
    if p.returncode != 0:
        sys.exit(f"uv pip compile failed for {ls.req.name}:\n{p.stderr}")


# ---- parsing ---------------------------------------------------------------

def pins(text: str) -> list[tuple[str, str]]:
    return [(m.group(1).lower(), m.group(2)) for m in _PIN.finditer(text)]


def entries(text: str) -> dict[tuple[str, str, str], frozenset[str]]:
    """{(name, version, marker): hashes} -- the part of a lock that decides
    what installs; comments and formatting are ignored."""
    out: dict = {}
    key, hashes = None, set()
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;\\]+)\s*(?:;\s*([^\\]*?))?\s*\\?$", line)
        if m:
            if key:
                out[key] = frozenset(hashes)
            key, hashes = (m.group(1).lower(), m.group(2), (m.group(3) or "").strip()), set()
        hashes.update(re.findall(r"--hash=sha256:([0-9a-f]{64})", line))
    if key:
        out[key] = frozenset(hashes)
    return out


def unhashed(text: str) -> list[str]:
    """Pinned entries with no --hash line under them."""
    return [name for (name, _, _), h in entries(text).items() if not h]


def _constraints_text(text: str) -> str:
    return "".join(f"{n}=={v}" + (f" ; {mk}" if mk else "") + "\n"
                   for n, v, mk in entries(text))


def _clean(text: str) -> str:
    """Drop the temporary constraints file from uv's 'via' annotations."""
    return "".join(ln for ln in text.splitlines(keepends=True)
                   if not re.match(r"^\s+#\s+-c\s", ln))


def fresh_resolve(ls: LockSet, pinned_text: str, tmp: Path) -> str:
    """Resolve requirements from scratch with `pinned_text`'s versions as
    constraints: same versions, hashes straight from the index."""
    cons = tmp / f"{ls.lock.stem}.constraints.txt"
    cons.write_text(_constraints_text(pinned_text), encoding="utf-8")
    out = tmp / f"{ls.lock.stem}.fresh.txt"
    _compile(ls, out, constraints=cons)
    return _clean(out.read_text(encoding="utf-8"))


def _significant(text: str) -> list[str]:
    """Every line pip acts on: all but blank lines and whole-line comments."""
    return [ln.rstrip("\n") for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def compare(committed: str, fresh: str) -> list[str]:
    """What differs between a committed lock and a fresh resolve.

    Every line pip reads must be exactly what uv writes. A parser that
    understands only part of pip's requirements syntax can be walked
    around: a same-line `--hash sha256:...`, an upper-case hash, a
    duplicate entry, an `-r https://...` include are all honoured by pip.
    So the parsed comparison below only explains a difference; the line
    comparison is what decides."""
    a, b = entries(committed), entries(fresh)
    problems = []
    for k in sorted(a.keys() - b.keys()):
        problems.append(f"locked but no longer required (or marker changed): {k[0]}=={k[1]} {k[2]}")
    for k in sorted(b.keys() - a.keys()):
        problems.append(f"required but not locked: {k[0]}=={k[1]} {k[2]}")
    for k in sorted(a.keys() & b.keys()):
        if a[k] != b[k]:
            extra, missing = len(a[k] - b[k]), len(b[k] - a[k])
            problems.append(f"hashes differ from the index for {k[0]}=={k[1]}: "
                            f"{extra} not published, {missing} published but not locked")
    if _significant(committed) != _significant(fresh):
        extra = sorted(set(_significant(committed)) - set(_significant(fresh)))
        dup = len(_significant(committed)) - len(set(_significant(committed)))
        detail = "; ".join(repr(ln.strip())[:120] for ln in extra[:5])
        problems.append("lines pip would act on differ from a fresh resolve"
                        + (f": {detail}" if detail else "")
                        + (f" ({dup} duplicated line(s))" if dup else ""))
    return problems


def missing_wheels(ls: LockSet, lock: Path) -> list[str]:
    """Each target whose exact pins cannot all install from wheels."""
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        for platform, versions in ls.targets:
            for py in versions:
                p = _run([_uv(), "pip", "compile", str(lock),
                          "--python-platform", platform, "--python-version", py,
                          "--only-binary", ":all:", "--no-deps", "--no-config",
                          "--default-index", INDEX, "-q",
                          "-o", str(Path(tmp) / "out.txt")],
                         {"MACOSX_DEPLOYMENT_TARGET": MACOS_FLOOR})
                if p.returncode != 0:
                    cause = next((ln.strip() for ln in p.stderr.splitlines()
                                  if "cause:" in ln or "hint:" in ln),
                                 p.stderr.strip()[:200])
                    failures.append(f"{platform} / Python {py}: {cause}")
    return failures


def audit_sets(out_dir: Path) -> list[Path]:
    """pip-audit takes one version per package per file, and evaluates
    markers against the machine it runs on. Strip the markers and split the
    pins of every lock into as many files as the most-versioned package
    needs, so every pinned release on every platform is audited."""
    by_name: dict[str, list[str]] = defaultdict(list)
    for ls in LOCKSETS:
        for name, ver in pins(ls.lock.read_text(encoding="utf-8")):
            if ver not in by_name[name]:
                by_name[name].append(ver)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for k in range(max(len(v) for v in by_name.values())):
        f = out_dir / f"pins-{k + 1}.txt"
        f.write_text("".join(f"{n}=={v[k]}\n" for n, v in sorted(by_name.items())
                             if k < len(v)), encoding="utf-8")
        files.append(f)
    return files


# ---- commands --------------------------------------------------------------

def check() -> int:
    failed = False
    for ls in LOCKSETS:
        name = ls.lock.relative_to(REPO_ROOT)
        if not ls.lock.exists():
            print(f"FAIL: {name} is missing")
            failed = True
            continue
        committed = ls.lock.read_text(encoding="utf-8")
        problems = [f"no hash for {n}" for n in unhashed(committed)]
        with tempfile.TemporaryDirectory() as tmp:
            problems += compare(committed, fresh_resolve(ls, committed, Path(tmp)))
        # Only a lock identical to a fresh resolve goes to uv again: a planted
        # `-r https://...` must not be fetched from the maintainer's machine.
        if not problems:
            problems += [f"no wheel: {f}" for f in missing_wheels(ls, ls.lock)]
        for p in problems:
            print(f"FAIL: {name}: {p}")
        if problems:
            print(f"      rerun {COMPILE_COMMAND}, review the diff, commit")
            failed = True
        else:
            n = len(entries(committed))
            t = sum(len(v) for _, v in ls.targets)
            print(f"OK: {name}: {n} pins, hashes match the index, in step with "
                  f"{ls.req.name}, wheels for {t} platform/Python target(s)")
    return 1 if failed else 0


def relock(ls: LockSet, upgrade: bool) -> bool:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        chosen = tmp / "chosen.txt"
        _compile(ls, chosen, seed=None if upgrade else ls.lock, upgrade=upgrade)
        chosen_text = chosen.read_text(encoding="utf-8")
        fresh = fresh_resolve(ls, chosen_text, tmp)
        drift = [p for p in compare(chosen_text, fresh)
                 if not p.startswith(("hashes differ", "lines pip would act on"))]
        new = tmp / "new.lock"
        new.write_text(fresh, encoding="utf-8")
        refusals = [f"versions moved between resolves: {p}" for p in drift]
        refusals += [f"no hash for {n}" for n in unhashed(fresh)]
        refusals += [f"no wheel: {f}" for f in missing_wheels(ls, new)]
        if refusals:
            for r in refusals:
                print(f"REFUSED: {ls.lock.name}: {r}")
            print(f"{ls.lock.name} NOT written.")
            return False
        shutil.copyfile(new, ls.lock)
    print(f"wrote {ls.lock.relative_to(REPO_ROOT)}: {len(entries(fresh))} pins")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--upgrade", action="store_true", help="move every pin to its latest release")
    g.add_argument("--check", action="store_true", help="verify the committed locks; write nothing")
    g.add_argument("--audit-sets", metavar="DIR", type=Path,
                   help="write marker-free pin files for pip-audit into DIR")
    a = ap.parse_args()

    if a.check:
        return check()
    if a.audit_sets:
        for f in audit_sets(a.audit_sets):
            print(f)
        return 0
    ok = all([relock(ls, a.upgrade) for ls in LOCKSETS])
    if ok:
        print("review the diff, then commit")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
