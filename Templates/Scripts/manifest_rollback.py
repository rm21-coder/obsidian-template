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
"""
from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from pathlib import Path

# Never a rollback target, anywhere in the vault.
_PROTECTED_NAMES = frozenset({"claude.md", "agents.md"})
_PROTECTED_TOP = frozenset({"templates"})


class UnsafeManifest(ValueError):
    """The manifest names a target outside what a rollback may touch."""


def _check_target(path: str, root: Path, i: int) -> Path:
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
    if rel and rel[0].lower() in _PROTECTED_TOP:
        raise UnsafeManifest(f"entry {i}: {path!r} is under {rel[0]}/, "
                             "which holds templates and automation, not notes")
    if target.name.lower() in _PROTECTED_NAMES:
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
    out = []
    for i, rec in enumerate(changes):
        path = rec.get("path") if isinstance(rec, dict) else None
        original = rec.get("original") if isinstance(rec, dict) else None
        if not isinstance(path, str) or not isinstance(original, str):
            raise UnsafeManifest(f"entry {i}: path and original must be strings")
        out.append((_check_target(path, root, i), original))
    return out


def apply_rollback(manifest_path: str | Path, vault_root: Path) -> int:
    """Restore every note in the manifest. Returns the number restored.

    Raises UnsafeManifest, writing nothing, if any entry is unsafe. An I/O
    error while preparing the new versions also writes nothing (it propagates
    after the temp files are removed).
    """
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    entries = _validated(manifest, vault_root)
    root = vault_root.resolve()

    staged: list[tuple[str, Path]] = []
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
            # Re-check immediately before the swap: the parent must still be
            # the same directory inside the vault.
            if target.parent.resolve() != target.parent or \
                    not target.parent.is_relative_to(root):
                raise UnsafeManifest(f"entry {i}: {target.parent} changed "
                                     "during the rollback")
            os.replace(tmp, target)
    finally:
        for tmp, _target in staged:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
    return len(entries)
