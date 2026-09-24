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
"""
from __future__ import annotations

import json
from pathlib import Path


class UnsafeManifest(ValueError):
    """The manifest names a target outside what a rollback may touch."""


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
        target = Path(path).resolve()
        if not target.is_relative_to(root):
            raise UnsafeManifest(f"entry {i}: {path!r} is outside the vault")
        if target.suffix.lower() != ".md":
            raise UnsafeManifest(f"entry {i}: {path!r} is not a Markdown note")
        out.append((target, original))
    return out


def apply_rollback(manifest_path: str | Path, vault_root: Path) -> int:
    """Restore every note in the manifest. Returns the number restored.

    Raises UnsafeManifest, writing nothing, if any entry is unsafe.
    """
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    entries = _validated(manifest, vault_root)
    for target, original in entries:
        target.write_text(original, encoding="utf-8")
    return len(entries)
