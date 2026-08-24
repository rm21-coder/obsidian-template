#!/usr/bin/env python3
"""
tag_clippings.py — Semantic auto-tagger for Obsidian notes.

Scans the Clippings/, Creations/, and Meetings/ folders for markdown
files, uses the Claude API to semantically match content against your
existing tag taxonomy, and auto-applies tags to YAML frontmatter.

Usage:
    python3 tag_clippings.py                  # Process all untagged files
    python3 tag_clippings.py --force           # Re-tag all files (ignore tracking)
    python3 tag_clippings.py --dry-run         # Preview without applying changes
    python3 tag_clippings.py --file "path"     # Tag a specific file

Setup:
    1. pip install anthropic pyyaml python-dotenv
    2. Set ANTHROPIC_API_KEY in your environment, or create a .env file.
       To route through an institutional AI gateway instead of a personal
       Anthropic key, set LLM_BASE_URL and LLM_API_KEY_NAME — see
       Templates/Scripts/llm_endpoint.py.
       The script checks for ~/dev/secrets/.env by default — edit the
       load_dotenv() path below if your secrets live elsewhere.
    3. Place this script in Templates/Scripts/ (or the vault root).
    4. Run from the vault root: python3 Templates/Scripts/tag_clippings.py
"""

import os
import sys
import json
import re
import argparse
import hashlib
import time
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
import yaml
import anthropic

# Cross-platform: force UTF-8 on stdout/stderr so printing a note title that
# contains non-ASCII characters can't crash the run on Windows, whose console
# defaults to a legacy code page (cp1252). Harmless no-op on macOS/Linux.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Load API keys — resolves to ~/dev/secrets/.env on macOS and
# %USERPROFILE%\dev\secrets\.env on Windows (Path.home() is cross-platform).
load_dotenv(Path.home() / "dev" / "secrets" / ".env")

# ─── Configuration ───────────────────────────────────────────────────────────

# Vault root is two levels up from Templates/Scripts/
VAULT_ROOT = Path(__file__).parent.parent.parent.resolve()

# Folders to scan for untagged/under-tagged notes
WATCH_FOLDERS = [
    VAULT_ROOT / "Clippings",
    VAULT_ROOT / "Creations",
    VAULT_ROOT / "Meetings",
]

# Per-folder configuration: base_tag is always preserved (None = no base tag)
FOLDER_CONFIG = {
    # 'clippings' base tag retired 2026-05-27 — it restated the folder/category
    # and added no retrieval value. None means no base tag is prepended, and
    # because 'clippings' is in IGNORE_TAGS the tagger also strips any lingering
    # instance from notes it reprocesses.
    "Clippings": {"base_tag": None},
    "Creations": {"base_tag": None},
    "Meetings":  {"base_tag": None},
}

# Tracking file — stores which files have been processed and their content hash
TRACKING_FILE = VAULT_ROOT / ".tag_tracking.json"

# Edit-safety guard: skip notes modified within this many seconds. A very
# recent edit usually means the note is open and being typed in, and the
# tagger rewrites the whole file — which drops unsaved keystrokes in the
# editor. Recently-touched notes are simply deferred to a later run.
# Override with TAG_RECENT_EDIT_GUARD_SECONDS; set 0 to disable.
RECENT_EDIT_GUARD_SECONDS = int(os.environ.get("TAG_RECENT_EDIT_GUARD_SECONDS", "120"))

# Canonical taxonomy file — the hard allowlist (optional). When present, the
# tagger uses it as the source of truth and refuses to apply tags not listed.
# When absent, the tagger falls back to scanning the vault for existing tags.
# See docs/Semantic Auto-Tagger Setup.md for the file format.
TAXONOMY_FILE = VAULT_ROOT / "Knowledge" / "Tag Taxonomy.md"

# Promotion candidates — tags Claude proposes that aren't in the taxonomy yet.
# Reviewed weekly; promoted to TAXONOMY_FILE if they show real signal.
PROMOTION_CANDIDATES_FILE = VAULT_ROOT / "Templates" / "Scripts" / "tag-promotion-candidates.md"

# Per-run tag diff — what got added/removed this run, for sniff testing.
LAST_DIFF_FILE = VAULT_ROOT / "Templates" / "Scripts" / "last-tag-diff.md"

# Acronyms that stay all-caps (or have a specific canonical form) when
# normalizing incoming tag candidates. Used so that variants like `ai`/`AI`/`Ai`
# all collapse to one canonical entry. Extend this with org-specific or
# domain-specific acronyms as your taxonomy grows.
# Map: UPPERCASE → canonical form to emit. e.g. {"GPUS": "GPUs"} keeps the s.
ACRONYM_MAP = {a.upper(): a for a in (
    "AI", "GPU", "AWS", "GCP", "IT", "HR", "DEI", "HIPAA", "HPC",
    "RAG", "API", "SLA", "NDA",
)}
ACRONYM_MAP["GPUS"] = "GPUs"

# Tags to ignore when collecting the taxonomy (structural, not topical)
IGNORE_TAGS = {
    "clippings", "note", "journal", "categories",
    "meetings/type", "type: table", "two-column-grid",
    "Draft", "Final",
}

# The base tag that all clippings already have (won't be added/removed)
BASE_TAG = "clippings"

# Claude model to use. Tagging against a fixed allowlist is a Haiku-class
# task — the taxonomy IS the guardrail, so the cheaper model loses little
# and costs ~1/3 as much per token. Override per-install with TAGGER_MODEL
# (e.g. a gateway alias, or a bigger model if your taxonomy is subtle).
CLAUDE_MODEL = os.environ.get("TAGGER_MODEL", "claude-haiku-4-5-20251001")

# Prompt caching: the system block (rules + taxonomy) is identical for every
# note in a run, so it is sent with a cache_control breakpoint — calls after
# the first read it at ~10% of the input price. Set TAGGER_PROMPT_CACHE=0 if
# your gateway rejects the cache_control field.
PROMPT_CACHE = os.environ.get("TAGGER_PROMPT_CACHE", "1") != "0"

# Max content characters to send to Claude (to manage token usage)
MAX_CONTENT_CHARS = 4000


# ─── Frontmatter Parsing ────────────────────────────────────────────────────

def parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """Parse YAML frontmatter from markdown text.
    Returns (frontmatter_dict, body_text). Returns (None, text) if no frontmatter."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if not match:
        return None, text
    try:
        fm = yaml.safe_load(match.group(1))
        if not isinstance(fm, dict):
            return None, text
        return fm, match.group(2)
    except yaml.YAMLError:
        return None, text


def rebuild_file(frontmatter: dict, body: str) -> str:
    """Rebuild a markdown file with updated frontmatter."""
    fm_str = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{fm_str}---\n{body}"


# ─── Tag Taxonomy Collection ────────────────────────────────────────────────

def load_canonical_taxonomy(taxonomy_file: Path) -> list[str] | None:
    """Load tags from the canonical taxonomy file. This is the HARD ALLOWLIST.

    Looks for `- tag` bullet lines under any section heading. Returns None if
    the file doesn't exist (caller falls back to vault scan)."""
    if not taxonomy_file.exists():
        return None
    tags: set[str] = set()
    in_filtered_section = False
    for line in taxonomy_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        # Section headers
        if stripped.startswith("##"):
            # The "Filtered out" section is reference-only — never promote those.
            in_filtered_section = "filtered out" in stripped.lower()
            continue
        if in_filtered_section:
            continue
        # Bullet list items: "- tag"
        m = re.match(r"^-\s+([A-Za-z][A-Za-z0-9_/\-]*)\s*$", stripped)
        if m:
            tags.add(m.group(1))
    return sorted(tags)


def collect_all_tags(vault_root: Path) -> list[str]:
    """Fallback: walk the vault and collect tags. Used only if taxonomy file is missing."""
    tags = set()
    for md_file in vault_root.rglob("*.md"):
        parts = md_file.relative_to(vault_root).parts
        if any(p.startswith(".") for p in parts):
            continue
        try:
            text = md_file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        fm, _ = parse_frontmatter(text)
        if fm and "tags" in fm and isinstance(fm["tags"], list):
            for tag in fm["tags"]:
                tag_str = str(tag).strip().strip('"').strip("'").lstrip("#")
                if tag_str.startswith("[[") or tag_str.startswith("Categories/"):
                    continue
                if tag_str and tag_str not in IGNORE_TAGS:
                    tags.add(tag_str)
    tags.discard(BASE_TAG)
    return sorted(tags)


def normalize_tag(tag: str) -> str:
    """Normalize a candidate tag to PascalCase, respecting acronyms and hierarchy.

    Applied to incoming tag suggestions before allowlist lookup so that the
    tagger doesn't produce case duplicates like `ai` vs `AI`.
    """
    if not tag:
        return tag
    # Hierarchical: normalize each segment separately
    if "/" in tag:
        return "/".join(normalize_tag(seg) for seg in tag.split("/"))
    # Acronym match (case-insensitive lookup, canonical-form output)
    upper = tag.upper()
    if upper in ACRONYM_MAP:
        return ACRONYM_MAP[upper]
    # snake_case or kebab-case → PascalCase
    if "_" in tag or "-" in tag:
        parts = re.split(r"[_\-]+", tag)
        return "".join(p[:1].upper() + p[1:].lower() if p else "" for p in parts)
    # Already mixed case starting upper — leave alone
    if tag[:1].isupper():
        return tag
    # All-lowercase single word → capitalize
    if tag.isalpha():
        return tag[:1].upper() + tag[1:].lower()
    return tag


def log_promotion_candidate(tag: str, source_file: Path):
    """Append an unknown-but-proposed tag to the promotion candidates file."""
    PROMOTION_CANDIDATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not PROMOTION_CANDIDATES_FILE.exists():
        PROMOTION_CANDIDATES_FILE.write_text(
            "---\ntags:\n- Documentation\n---\n\n"
            "# Tag Promotion Candidates\n\n"
            "Tags Claude proposed that are not in the canonical taxonomy.\n"
            "Review weekly. Add to `Knowledge/Tag Taxonomy.md` if signal is real.\n\n"
            "| Date | Source file | Proposed tag |\n|---|---|---|\n",
            encoding="utf-8",
        )
    line = f"| {datetime.now().strftime('%Y-%m-%d %H:%M')} | {source_file.name} | `{tag}` |\n"
    with PROMOTION_CANDIDATES_FILE.open("a", encoding="utf-8") as f:
        f.write(line)


def format_tag_taxonomy(tags: list[str]) -> str:
    """Format tags into a readable grouped structure for the Claude prompt."""
    groups: dict[str, list[str]] = {}
    standalone = []

    for tag in tags:
        if "/" in tag:
            prefix = tag.split("/")[0]
            groups.setdefault(prefix, []).append(tag)
        else:
            standalone.append(tag)

    lines = ["Standalone tags:"]
    lines.extend(f"  - {t}" for t in standalone)
    lines.append("")
    for prefix in sorted(groups.keys()):
        lines.append(f"{prefix}/ hierarchy:")
        lines.extend(f"  - {t}" for t in sorted(groups[prefix]))
        lines.append("")

    return "\n".join(lines)


# ─── Tracking (avoid re-processing) ─────────────────────────────────────────

def load_tracking() -> dict:
    """Load the tracking file."""
    if TRACKING_FILE.exists():
        try:
            return json.loads(TRACKING_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, PermissionError):
            return {}
    return {}


def save_tracking(data: dict):
    """Save the tracking file."""
    TRACKING_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def content_hash(text: str) -> str:
    """Hash file content to detect changes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ─── Claude API Tagging ─────────────────────────────────────────────────────

def get_tag_suggestions(
    client: anthropic.Anthropic,
    title: str,
    description: str,
    body: str,
    taxonomy: str,
    existing_tags: list[str],
    folder_name: str = "Clippings",
    extra_context: str = "",
) -> list[str]:
    """Ask Claude to suggest tags for a piece of content."""

    body_excerpt = body[:MAX_CONTENT_CHARS]
    if len(body) > MAX_CONTENT_CHARS:
        body_excerpt += "\n[... content truncated ...]"

    # Folder-specific prompt guidance
    base_tag = FOLDER_CONFIG.get(folder_name, {}).get("base_tag")
    if folder_name == "Meetings":
        folder_guidance = """This is a MEETING NOTE. Meeting notes are often informal with abbreviations,
shorthand, and sentence fragments. Focus on the substantive topics discussed in
the body — projects, organizational units, vendors, strategic themes, technology
topics — not on the fact that it is a meeting. Tag ONLY based on what is written
in the body. Do not infer tags from frontmatter, participants, or filename."""
        base_note = ""
    elif folder_name == "Creations":
        folder_guidance = """This is an ORIGINAL CREATION — a polished voice note, hand-written piece,
or a Markitdown-converted document (Word/Excel/PowerPoint/PDF/HTML/audio/image
flattened to markdown). Tone may range from informal dictation to formal prose.
Tag based on subject matter, not on the fact that it originated as a voice note
or a converted document."""
        base_note = ""
    else:
        folder_guidance = "This is a WEB CLIPPING (a saved article)."
        base_note = ""

    # Split for prompt caching: everything identical across a run (role,
    # rules, taxonomy) goes in the system block, which carries the cache
    # breakpoint; everything per-note goes in the user message. Do not move
    # per-note content into system — it would defeat the cache.
    system_text = f"""You are a semantic tagger for an Obsidian knowledge vault. Your job is to assign
the most relevant existing tags to a piece of content.

RULES:
1. ONLY use tags from the provided taxonomy — never invent new tags.
2. Assign between 1 and 6 tags (be selective, not exhaustive).
3. Use the most specific tag available. For example, if content is about autonomous
   coding agents, use "AI/Agents" rather than just "AI".
4. Consider the full content meaning, not just keyword matches.
5. If the content already has correct tags, return those same tags.

EXISTING TAG TAXONOMY:
{taxonomy}

Respond with ONLY a JSON array of tag strings. Example: ["AI", "Programming", "Vendors/AWS"]"""

    prompt = f"""{folder_guidance}
{base_note}
CONTENT TO TAG:
Title: {title}
Description: {description}
{extra_context}Current tags: {', '.join(existing_tags) if existing_tags else 'none'}

Body:
{body_excerpt}"""

    system_block: list | str
    if PROMPT_CACHE:
        system_block = [{"type": "text", "text": system_text,
                         "cache_control": {"type": "ephemeral"}}]
    else:
        system_block = system_text

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        system=system_block,
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        import usage_log
        usage_log.record("tag_clippings", CLAUDE_MODEL, response.usage)
    except Exception:
        pass

    text = response.content[0].text.strip()
    # Handle markdown code blocks if Claude wraps the response
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # First try: parse the whole response as a JSON array
    try:
        tags = json.loads(text)
        if isinstance(tags, list):
            return [str(t) for t in tags if str(t)]
    except json.JSONDecodeError:
        pass

    # Fallback: extract the first JSON array embedded in the response. Claude
    # occasionally appends reasoning ("Wait, "Tools" isn't in the taxonomy...")
    # after a perfectly valid array. Find the first `[...]` and try that.
    array_match = re.search(r"\[[^\[\]]*\]", text, re.DOTALL)
    if array_match:
        try:
            tags = json.loads(array_match.group(0))
            if isinstance(tags, list):
                return [str(t) for t in tags if str(t)]
        except json.JSONDecodeError:
            pass

    print(f"  Warning: Could not parse Claude response: {text[:200]}")
    return []


# ─── Main Processing Logic ──────────────────────────────────────────────────

def _detect_folder(filepath: Path) -> str:
    """Detect which watch folder a file belongs to."""
    try:
        rel = filepath.relative_to(VAULT_ROOT)
        return rel.parts[0] if rel.parts else "Clippings"
    except ValueError:
        return "Clippings"


def _extract_meeting_context(fm: dict) -> str:
    """Extract meeting-specific metadata for the Claude prompt."""
    lines = []
    meeting_type = fm.get("type", "")
    if meeting_type:
        lines.append(f"Meeting type: {meeting_type}")
    group = fm.get("group", [])
    if group and isinstance(group, list):
        names = [g.replace("[[", "").replace("]]", "") for g in group if isinstance(g, str) and g.strip()]
        if names:
            lines.append(f"Group: {', '.join(names)}")
    people = fm.get("people", [])
    if people and isinstance(people, list):
        names = [p.replace("[[", "").replace("]]", "") for p in people if isinstance(p, str)]
        if names:
            lines.append(f"People: {', '.join(names)}")
    if lines:
        return "\n".join(lines) + "\n"
    return ""


def process_file(
    filepath: Path,
    client: anthropic.Anthropic,
    taxonomy_text: str,
    valid_tags: dict[str, str],
    dry_run: bool = False,
) -> bool:
    """Process a single markdown file. Returns True if tags were updated."""
    # Read preserving the file's original line endings so we can write them
    # back unchanged. Without this, Path.write_text on Windows would rewrite
    # every note with CRLF endings and produce a spurious full-file diff on
    # each run. Content is normalized to \n for parsing, restored on write.
    with filepath.open("r", encoding="utf-8", newline="") as fh:
        original = fh.read()
    file_newline = "\r\n" if "\r\n" in original else "\n"
    text = original.replace("\r\n", "\n").replace("\r", "\n")
    fm, body = parse_frontmatter(text)

    if fm is None:
        print(f"  Warning: No frontmatter found, skipping: {filepath.name}")
        return False

    folder_name = _detect_folder(filepath)
    config = FOLDER_CONFIG.get(folder_name, {"base_tag": BASE_TAG})
    base_tag = config.get("base_tag")

    title = fm.get("title", filepath.stem) or filepath.stem
    description = fm.get("description", "") or ""
    current_tags = fm.get("tags", []) or []

    # Normalize current tags
    current_clean = []
    for t in current_tags:
        t_str = str(t).strip().strip('"').strip("'")
        current_clean.append(t_str)

    # Filter to just topical tags (exclude base tag, author links, etc.)
    existing_topical = [
        t for t in current_clean
        if t != base_tag and not t.startswith("[[") and t not in IGNORE_TAGS
    ]

    print(f"  File: {filepath.name}  [{folder_name}]")
    print(f"     Current topical tags: {existing_topical or '(none)'}")

    # Empty-body guard: do not derive tags from frontmatter alone.
    # If the body has no meaningful content yet (e.g. a freshly-templated
    # meeting note), skip the file entirely. The tracking hash still gets
    # updated below, so once real content arrives the file picks up.
    if len(body.strip()) < 50:
        print(f"     Empty/minimal body — skipping (tags should reflect body content, not frontmatter)")
        return False

    # extra_context is intentionally empty: tags are derived from body only,
    # never from frontmatter (people, group, type). Prevents empty meetings
    # from getting topical tags inferred from the participant list.
    extra_context = ""

    # Get suggestions from Claude
    suggestions = get_tag_suggestions(
        client, title, description, body, taxonomy_text, existing_topical,
        folder_name=folder_name, extra_context=extra_context,
    )

    # Validate: only keep tags that exist in our taxonomy. Apply the casing
    # normalizer first so that variants like `ai`, `AI`, `Ai` all resolve to
    # the canonical entry, and snake_case → PascalCase. Anything not on the
    # allowlist is logged to tag-promotion-candidates.md for weekly review
    # rather than written to the file (no-singletons rule).
    validated: list[str] = []
    dropped: set[str] = set()
    for t in suggestions:
        # Try the suggestion as-is (case-insensitive) first — preserves
        # established hyphenated tags like `Self-improvement`, `IT-Governance`.
        canon = valid_tags.get(t.lower())
        if canon is None:
            # Then try the normalized form (PascalCase, acronym-aware).
            normalized = normalize_tag(t)
            canon = valid_tags.get(normalized.lower())
        if canon is not None:
            validated.append(canon)
        else:
            dropped.add(t)
            log_promotion_candidate(normalize_tag(t), filepath)
    if dropped:
        print(f"     Logged promotion candidates (not written): {dropped}")

    print(f"     Suggested tags: {validated}")

    # Build the new tag list: base tag (if any) + topical tags from Claude.
    # Deduplicate, preserving order — the model can return the same tag twice,
    # and two suggestions ('ai' and 'AI') can normalize onto one canonical entry,
    # so validated is not guaranteed distinct and may already contain base_tag.
    seen: set[str] = set()
    new_tags = []
    for t in ([base_tag] + validated if base_tag else validated):
        if t not in seen:
            seen.add(t)
            new_tags.append(t)

    # Check if anything actually changed
    if set(new_tags) == set(current_clean):
        print(f"     Tags already correct, no changes needed")
        return False

    if dry_run:
        print(f"     DRY RUN — would set tags to: {new_tags}")
        return False

    # Update frontmatter and write back
    fm["tags"] = new_tags
    new_text = rebuild_file(fm, body)
    filepath.write_text(new_text, encoding="utf-8", newline=file_newline)
    print(f"     Updated tags: {new_tags}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Semantic auto-tagger for Obsidian notes")
    parser.add_argument("--vault", type=str, help="Override vault root path (default: auto-detect from script location)")
    parser.add_argument("--force", action="store_true", help="Re-tag all files, ignoring tracking")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying")
    parser.add_argument("--file", type=str, help="Tag a specific file path")
    args = parser.parse_args()

    # Allow vault root override (needed for scheduled tasks running from different paths)
    global VAULT_ROOT, WATCH_FOLDERS, TRACKING_FILE
    if args.vault:
        VAULT_ROOT = Path(args.vault).resolve()
        WATCH_FOLDERS = [
            VAULT_ROOT / "Clippings",
            VAULT_ROOT / "Creations",
            VAULT_ROOT / "Meetings",
        ]
        TRACKING_FILE = VAULT_ROOT / ".tag_tracking.json"

    # Resolve the endpoint and its credential (env/.env first, then the
    # platform keystore). Stock Anthropic unless LLM_BASE_URL redirects it.
    import llm_endpoint
    try:
        client = llm_endpoint.client()
    except llm_endpoint.GatewayUnreachable as exc:
        # Skipped, not failed — see the note in classify_notes.py. The tagger
        # runs every 30 minutes, so off-VPN stretches would otherwise fill the
        # log with failures for a job that had nothing wrong with it.
        print(f"Skipped: {exc}")
        sys.exit(0)
    except llm_endpoint.EndpointError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    print("Obsidian Semantic Tagger")
    print(f"   Vault: {VAULT_ROOT}")
    print(f"   Endpoint: {llm_endpoint.describe()}")
    print(f"   Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print()

    # Step 1: Load tag taxonomy. Prefer the canonical allowlist file (when
    # present); fall back to a vault scan if it's missing.
    canonical = load_canonical_taxonomy(TAXONOMY_FILE)
    if canonical is not None:
        print(f"Loaded canonical taxonomy from {TAXONOMY_FILE.name}")
        all_tags = canonical
    else:
        print("Canonical taxonomy file not found — falling back to vault scan.")
        print(f"  (To enable allowlist mode, create: {TAXONOMY_FILE})")
        all_tags = collect_all_tags(VAULT_ROOT)
    valid_tags_map = {t.lower(): t for t in all_tags}
    taxonomy_text = format_tag_taxonomy(all_tags)
    print(f"   Allowlist size: {len(all_tags)} tags\n")

    # Step 2: Find files to process
    tracking = load_tracking() if not args.force else {}

    if args.file:
        files_to_process = [Path(args.file).resolve()]
    else:
        # rglob (recursive) — covers archived material under subfolders like
        # Meetings/History/. Doc-aligned: the setup guide explicitly tells
        # users they can move older notes into a History/ subfolder and have
        # the tagger keep seeing them.
        files_to_process = []
        for folder in WATCH_FOLDERS:
            if folder.exists():
                files_to_process.extend(sorted(folder.rglob("*.md")))
            else:
                print(f"   Folder not found (will be watched when created): {folder.name}")

    if not files_to_process:
        print("   No files found to process.")
        return

    print(f"Found {len(files_to_process)} file(s) to check\n")

    # Step 3: Process each file
    updated_count = 0
    skipped_count = 0
    deferred_count = 0

    for filepath in files_to_process:
        # Edit-safety: defer notes touched very recently — you're probably
        # still typing, and rewriting the file would clobber unsaved edits.
        # Bypassed by --force / --file (explicit, deliberate runs).
        if RECENT_EDIT_GUARD_SECONDS > 0 and not args.force and not args.file:
            try:
                if time.time() - filepath.stat().st_mtime < RECENT_EDIT_GUARD_SECONDS:
                    deferred_count += 1
                    continue
            except OSError:
                pass
        file_key = str(filepath.relative_to(VAULT_ROOT))
        text = filepath.read_text(encoding="utf-8")
        file_hash = content_hash(text)

        # Skip if already processed and content hasn't changed
        if file_key in tracking and tracking[file_key] == file_hash and not args.force:
            skipped_count += 1
            continue

        try:
            changed = process_file(filepath, client, taxonomy_text, valid_tags_map, args.dry_run)
            if changed:
                updated_count += 1
                # Re-read and hash the updated content
                new_text = filepath.read_text(encoding="utf-8")
                tracking[file_key] = content_hash(new_text)
            else:
                tracking[file_key] = file_hash
        except Exception as e:
            print(f"  Error processing {filepath.name}: {e}")

    # Step 4: Save tracking
    if not args.dry_run:
        save_tracking(tracking)

    print(f"\nDone! Updated: {updated_count} | Skipped (unchanged): {skipped_count} | Deferred (recent edit): {deferred_count} | Total: {len(files_to_process)}")


if __name__ == "__main__":
    main()
