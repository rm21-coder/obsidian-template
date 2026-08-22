#!/usr/bin/env python3
"""
merge_tags.py — consolidate/rename tags across the vault, surgically and reversibly.

Use it to fold a flat tag into a hierarchy (e.g. Cybersecurity -> Security/Cyber),
collapse duplicates, or rename a tag everywhere. It edits ONLY the frontmatter
tags: block (block-list and inline forms); bodies, other frontmatter, indentation,
and quoting are left byte-for-byte unchanged. It is idempotent, dry-run by default,
and writes a rollback manifest on --apply.

EDIT the MERGES map below for your own taxonomy, then:
    python3 Templates/Scripts/merge_tags.py             # dry-run over the whole vault
    python3 Templates/Scripts/merge_tags.py --apply     # perform the edits
    python3 Templates/Scripts/merge_tags.py --rollback merge_tags_manifest_*.json
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

VAULT_ROOT = Path(__file__).parent.parent.parent.resolve()   # two levels up from Templates/Scripts/

# EXAMPLE — replace with your own consolidations (flat tag -> target tag).
# A note carrying both the source and the target ends up with just the target.
MERGES = {
    # "Cybersecurity": "Security/Cybersecurity",
    # "Infrastructure": "IT/Infrastructure",
    # "ProjectManagement": "PMO/ProjectManagement",
}

SKIP_FILES = {"Tag Taxonomy.md"}     # the allowlist doc lists tags as prose, not frontmatter

_FM_SPLIT = re.compile(r"^(---\s*\n)(.*?)(\n---\s*\n?)(.*)$", re.DOTALL)


def _merge_list(before):
    after, seen = [], set()
    for tag in before:
        new = MERGES.get(tag, tag)
        if new not in seen:
            after.append(new)
            seen.add(new)
    return after


def rewrite(text):
    """Return (new_text, before, after); (None, ...) if nothing changes. Touches
    only the tags: block; handles block-list and inline forms."""
    m = _FM_SPLIT.match(text)
    if not m:
        return None, None, None
    open_, fm, close, body = m.groups()
    lines = fm.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^tags:\s*$", line):                     # block-list
            j = i + 1
            while j < len(lines) and re.match(r"^\s*-\s+.+", lines[j]):
                j += 1
            indent_m = re.match(r"^(\s*)-", lines[i + 1]) if j > i + 1 else None
            indent = indent_m.group(1) if indent_m else "  "
            before = [re.match(r"^\s*-\s+(.+?)\s*$", l).group(1) for l in lines[i + 1:j]]
            after = _merge_list(before)
            if after == before:
                return None, before, after
            block = [lines[i]] + [f"{indent}- {t}" for t in after]
            return open_ + "\n".join(lines[:i] + block + lines[j:]) + close + body, before, after
        m2 = re.match(r"^tags:\s*\[(.*)\]\s*$", line)         # inline
        if m2:
            inner = m2.group(1).strip()
            if not inner:
                return None, [], []
            quoted = '"' in inner or "'" in inner
            before = [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
            after = _merge_list(before)
            if after == before:
                return None, before, after
            items = [f'"{t}"' for t in after] if quoted else after
            lines[i] = f"tags: [{', '.join(items)}]"
            return open_ + "\n".join(lines) + close + body, before, after
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--rollback", metavar="MANIFEST", help="undo a prior apply from its manifest")
    args = ap.parse_args()

    if args.rollback:
        manifest = json.loads(Path(args.rollback).read_text())
        for rec in manifest["changes"]:
            Path(rec["path"]).write_text(rec["original"], encoding="utf-8")
        print(f"Rolled back {len(manifest['changes'])} files from {args.rollback}")
        return

    if not MERGES:
        sys.exit("MERGES is empty — edit the MERGES map at the top of this script first.")

    changes = []
    for p in sorted(VAULT_ROOT.rglob("*.md")):
        if any(part.startswith(".") for part in p.relative_to(VAULT_ROOT).parts):
            continue
        if p.name in SKIP_FILES:
            continue
        original = p.read_text(encoding="utf-8")
        new_text, before, after = rewrite(original)
        if new_text is None:
            continue
        changes.append({"path": str(p), "rel": str(p.relative_to(VAULT_ROOT)),
                        "before": before, "after": after, "original": original})

    print(f"{'APPLY' if args.apply else 'DRY-RUN'} | {len(changes)} notes to change\n")
    for c in changes:
        removed = [t for t in c["before"] if t not in c["after"]]
        added = [t for t in c["after"] if t not in c["before"]]
        print(f"  {c['rel']}\n      - {removed}   + {added}")

    if args.apply:
        for c in changes:
            new_text, _, _ = rewrite(c["original"])
            Path(c["path"]).write_text(new_text, encoding="utf-8")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        manifest = Path(f"merge_tags_manifest_{stamp}.json")
        manifest.write_text(json.dumps({"changes": changes}, ensure_ascii=False, indent=0))
        print(f"\nApplied. Rollback manifest: {manifest}")
        print(f"Undo with:  python3 merge_tags.py --rollback {manifest}")
    else:
        print("\nDry-run only. Re-run with --apply to write. No files changed.")


if __name__ == "__main__":
    main()
