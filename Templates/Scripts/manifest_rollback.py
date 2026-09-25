"""manifest_rollback.py -- restore notes from an --apply manifest, safely.

merge_tags, tag_clippings_rag and vault_lint each write a JSON manifest of the
notes they change, and each can undo itself with --rollback MANIFEST. All three
used to write every manifest entry's "original" text to its "path" with no
check at all, so a manifest that had been planted or altered -- it is a plain
file, and inside a synced vault it can arrive from another device -- could
write arbitrary content to any path the user can write. Found by Microsoft
M-DASH 2026-09-23 (CWE-73, three findings).

The fix M-DASH proposed, "keep the target under the vault root", is not enough
on its own: the vault root contains Templates/Scripts/, the automation itself,
so a manifest aimed at a scheduled script inside the vault passes that check
and turns into code execution at the next run. These tools only ever modify
Markdown notes, so a restore target must be BOTH inside the vault AND a .md
file.

Validation is all-or-nothing. A manifest with one unsafe entry is refused
before anything is written: a restore that half-applies leaves the vault in a
state neither the manifest nor the user intended.

Tightened after the adversarial review of the M-DASH fixes (2026-09-25):

  * ".md inside the vault" was still too wide. Templater templates are .md
    files that run JavaScript when applied, CLAUDE.md instructs agent sessions
    that hold a shell, and .obsidian/ is configuration. None of those is a
    note these tools modify, so Templates/, dot-folders and agent-instruction
    files are refused -- and a rewrite there would not have been noticed,
    because the integrity monitor hashes code, not Markdown.
  * A target must already exist as a regular, singly-linked file. A rollback
    restores notes the tool changed; it never creates one, and a hard link is
    a way to write through to a file outside the vault.
  * "Writes nothing" held only for validation. An I/O error mid-restore left
    it half-applied. Every new version is now written to a temp file first and
    only then swapped in with os.replace, which also replaces a symlink planted
    after validation instead of following it.

Round 2 of that review broke the denylist twice -- "Templateſ/" (long s) is
"Templates/" to APFS but not to str.lower(), and CLAUDE.local.md, GEMINI.md
and docs/ were simply not on it -- which is what denylists do. The real flaw
was trusting the manifest's location: manifests were written INTO the vault,
where a synced device can plant or edit one. They are now written outside it
(manifest_dir()), and a manifest found inside the vault is refused outright.
The location checks stay as defence in depth, compared by filesystem
identity and by NFKC case-folded name rather than by spelling.
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import unicodedata
from pathlib import Path

# Never a rollback target (compared NFKC-casefolded, and top-level folders
# also by inode). Instruction files for coding agents are matched by prefix
# so their variants (CLAUDE.local.md, AGENTS.override.md, ...) are included.
_PROTECTED_NAME_PREFIXES = ("claude", "agents", "gemini", "copilot-instructions")
_PROTECTED_TOP = frozenset({"templates", "docs"})
MAX_MANIFEST_BYTES = 256 * 1024 * 1024


class UnsafeManifest(ValueError):
    """The manifest names a target outside what a rollback may touch, or is
    itself untrustworthy. Raised before anything is written."""


class PartialRollback(RuntimeError):
    """Some notes were restored and then a swap failed. `restored` lists them."""

    def __init__(self, msg: str, restored: list[Path]):
        super().__init__(msg)
        self.restored = restored


def manifest_dir() -> Path:
    """Where the tools write rollback manifests: outside the vault, private.

    OBSIDIAN_ROLLBACK_DIR overrides it (tests, or a machine with a different
    layout)."""
    d = Path(os.environ.get("OBSIDIAN_ROLLBACK_DIR")
             or Path.home() / ".local" / "state" / "obsidian-template" / "rollback")
    d.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(d, 0o700)
    return d


def _fold(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold()


def _protected_identities(root: Path) -> set[tuple[int, int]]:
    ids = set()
    with contextlib.suppress(OSError):
        for child in root.iterdir():
            if _fold(child.name) in _PROTECTED_TOP:
                st = os.stat(child)
                ids.add((st.st_dev, st.st_ino))
    return ids


def _check_target(path: str, root: Path, i: int,
                  protected: set[tuple[int, int]] | None = None) -> Path:
    given = Path(path)
    if not given.is_absolute():
        given = root / given          # relative to the vault, never to the cwd
    target = given.resolve()
    if not target.is_relative_to(root):
        raise UnsafeManifest(f"entry {i}: {path!r} is outside the vault")
    if target.suffix.lower() != ".md":
        raise UnsafeManifest(f"entry {i}: {path!r} is not a Markdown note")
    rel = target.relative_to(root).parts
    if any(part.startswith(".") for part in rel):
        raise UnsafeManifest(f"entry {i}: {path!r} is inside a dot-folder")
    if rel and _fold(rel[0]) in _PROTECTED_TOP:
        raise UnsafeManifest(f"entry {i}: {path!r} is under {rel[0]}/, "
                             "which holds templates, automation or docs, not notes")
    if protected is None:
        protected = _protected_identities(root)
    anc = target.parent
    while anc != root and anc.is_relative_to(root):
        with contextlib.suppress(OSError):
            st = os.stat(anc)
            if (st.st_dev, st.st_ino) in protected:
                raise UnsafeManifest(f"entry {i}: {path!r} is inside a protected "
                                     "folder (matched by identity, not spelling)")
        anc = anc.parent
    if _fold(target.name).startswith(_PROTECTED_NAME_PREFIXES):
        raise UnsafeManifest(f"entry {i}: {path!r} is an agent-instruction file")
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        raise UnsafeManifest(f"entry {i}: {path!r} does not exist; a rollback "
                             "restores notes, it never creates them") from None
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeManifest(f"entry {i}: {path!r} is not a regular file")
    if st.st_nlink != 1:
        raise UnsafeManifest(f"entry {i}: {path!r} has {st.st_nlink} hard links")
    return target


def _validated(manifest: dict, vault_root: Path) -> list[tuple[Path, str]]:
    changes = manifest.get("changes") if isinstance(manifest, dict) else None
    if not isinstance(changes, list):
        raise UnsafeManifest("manifest has no 'changes' list")
    root = vault_root.resolve()
    protected = _protected_identities(root)
    out = []
    for i, rec in enumerate(changes):
        path = rec.get("path") if isinstance(rec, dict) else None
        original = rec.get("original") if isinstance(rec, dict) else None
        if not isinstance(path, str) or not isinstance(original, str) or "\x00" in path:
            raise UnsafeManifest(f"entry {i}: path and original must be strings")
        out.append((_check_target(path, root, i, protected), original))
    return out


def _load_manifest(manifest_path: str | Path, root: Path) -> dict:
    mp = Path(manifest_path).expanduser()
    real = mp.resolve()
    if real == root or real.is_relative_to(root):
        raise UnsafeManifest(
            f"{mp} is inside the vault. Manifests are written to {manifest_dir()} "
            "because anything inside a synced vault can be planted or edited from "
            "another device. If you are certain this one is yours, read its "
            "entries, then move it out of the vault and run again.")
    try:
        st = os.stat(real)
    except OSError as e:
        raise UnsafeManifest(f"{mp}: {e.strerror}") from None
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeManifest(f"{mp} is not a regular file")
    if st.st_size > MAX_MANIFEST_BYTES:
        raise UnsafeManifest(f"{mp} is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        return json.loads(real.read_text(encoding="utf-8"))
    except (ValueError, RecursionError) as e:
        raise UnsafeManifest(f"{mp} is not a valid manifest ({type(e).__name__})") from None


def apply_rollback(manifest_path: str | Path, vault_root: Path) -> int:
    """Restore every note in the manifest. Returns the number restored.

    Raises UnsafeManifest, writing nothing, if any entry is unsafe. An I/O
    error while preparing the new versions also writes nothing (it propagates
    after the temp files are removed).
    """
    root = vault_root.resolve()
    manifest = _load_manifest(manifest_path, root)
    entries = _validated(manifest, vault_root)

    staged: list[tuple[str, Path]] = []
    restored: list[Path] = []
    try:
        for target, original in entries:
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".rollback-",
                                       suffix=".tmp")
            staged.append((tmp, target))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(original)
            with contextlib.suppress(OSError):
                os.chmod(tmp, stat.S_IMODE(os.lstat(target).st_mode))
        for i, (tmp, target) in enumerate(staged):
            # Re-check immediately before each swap. Everything validated can
            # have changed since: the parent swapped for a symlink, the target
            # deleted (os.replace would then CREATE it) or replaced.
            try:
                _check_target(str(target), root, i)
                if target.parent.resolve() != target.parent:
                    raise UnsafeManifest(f"entry {i}: {target.parent} changed "
                                         "during the rollback")
                os.replace(tmp, target)
            except (UnsafeManifest, OSError) as e:
                if not restored:
                    raise
                raise PartialRollback(
                    f"restored {len(restored)} of {len(staged)} notes, then entry "
                    f"{i} failed: {e}", restored) from e
            restored.append(target)
    finally:
        for tmp, _target in staged:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
    return len(entries)


def run_rollback_cli(manifest_path: str | Path, vault_root: Path) -> int:
    """The --rollback handler all three tools share. Returns an exit code.

    Says "nothing written" only when that is true: a refusal before any swap,
    or an I/O error while staging. A partial restore is reported as partial,
    naming what was restored.
    """
    import sys
    try:
        n = apply_rollback(manifest_path, vault_root)
    except UnsafeManifest as exc:
        print(f"Refusing rollback, nothing written: {exc}", file=sys.stderr)
        return 1
    except PartialRollback as exc:
        print(f"ROLLBACK INCOMPLETE: {exc}", file=sys.stderr)
        for p in exc.restored:
            print(f"  restored: {p}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Rollback failed before any note was changed, nothing written: "
              f"{exc}", file=sys.stderr)
        return 1
    print(f"Rolled back {n} files from {manifest_path}")
    return 0


def new_manifest_path(tool: str, stamp: str) -> Path:
    """A fresh manifest path for `tool`, outside the vault."""
    return manifest_dir() / f"{tool}_manifest_{stamp}.json"
