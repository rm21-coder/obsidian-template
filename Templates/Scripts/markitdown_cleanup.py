#!/usr/bin/env python3
"""
markitdown_cleanup.py — Post-conversion cleanup for Markitdown output.

Runs inline inside markitdown_dropper.py after markitdown.convert() returns
the raw markdown, before it's written to ~/Obsidian/Creations/.

OPERATIONS (in order)
  1. Split any existing YAML frontmatter from the body.
  2. Extract inline base64 images (`![alt](data:image/...;base64,DATA)`) to
     the attachments directory and replace each with an Obsidian wiki-link
     `![[name.png]]`.
  3. Handle Markitdown stubs (`![alt](data:image/...;base64...)` with no real
     data) and prior cleanup placeholders. If the source file is a .docx,
     .pptx, or .xlsx, extract images from its `*/media/` folder and use them
     to replace stubs in document order. Stubs without a matching source
     image become a placeholder. Extra extracted images get appended in a
     "Images from source" section.
  4. Normalize Outlook/Word bullet markers (•, ○, ▪, ▸, ▹, ‣, ◦, ●, ⁃) to
     standard "-". Tab indentation is converted to two-space indentation
     while preserving nesting depth.
  5. Promote two STRICT heading patterns to `##`:
       a. `N. **Heading text**` on its own line (numbered + entirely bold)
       b. `**Heading text:**` on its own line (bold label ending in colon)
     Anything else is left alone — no aggressive heading inference.
     Promotion is skipped inside fenced code blocks.
  6. Normalize whitespace: strip trailing spaces, collapse blank-line runs
     to a single blank, trim leading and trailing blanks.
  7. If the file had no frontmatter, prepend a minimal block:
       title, created (today), source: markitdown, source_file, tags: []

WHAT IT DOES NOT DO (intentionally)
  - Aggressive heading inference from inline labels surrounded by prose.
  - Smart-quote / em-dash / ellipsis normalization. Those work fine in
    Obsidian and read better in print.
  - Touch fenced code blocks, tables, or links.
  - Backups. Markitdown leaves the source file untouched, and the user's
    convention is that originals live in email.

PUBLIC API

    cleaned_text, summary = clean(content, source_path, attachments_dir)

  `summary` is a dict with:
      images_extracted    list[Path]   inline base64 images decoded
      source_images       list[Path]   images pulled from .docx/.pptx archive
      bullets_normalized  int          bullet markers replaced
      headings_promoted   int          lines turned into ## headings
      stubs_replaced      int          stubs replaced with extracted images
      stubs_placeheld     int          stubs replaced with placeholder text
      frontmatter_added   bool         True if no frontmatter existed

CLI (for spot-checking and retroactive cleanup of existing vault files)

    python3 markitdown_cleanup.py path/to/file.md            # print cleaned
    python3 markitdown_cleanup.py path/to/file.md --in-place # rewrite
    python3 markitdown_cleanup.py path/to/file.md --source path/to/original.docx --in-place
                                                  # also recover images from
                                                  # the original archive

Default attachments dir is ~/Obsidian/Z_attachments — override with
--attachments-dir.
"""

from __future__ import annotations

import base64
import re
import zipfile
from datetime import date
from pathlib import Path


# ─── Frontmatter ─────────────────────────────────────────────────────────────

FRONTMATTER_RE = re.compile(r"\A(---\s*\n.*?\n---\s*\n)", re.DOTALL)


def split_frontmatter(content: str) -> tuple[str, str]:
    """Return (frontmatter_block, body). Empty frontmatter if none present."""
    m = FRONTMATTER_RE.match(content)
    if m:
        return m.group(1), content[m.end():]
    return "", content


def generate_frontmatter(source_path: Path) -> str:
    """Generate minimal frontmatter for a freshly-converted file.

    Sets classification: internal-use-only as the conservative default.
    Markitdown-dropped files come from any source the user chooses (docx,
    pptx, pdf, etc.), so we mirror the Note Template default and let the
    user elevate to `confidential` or downgrade to `public` after import.
    See ~/Obsidian/Knowledge/Data Classification.md for the scheme.
    """
    title = source_path.stem.replace("_", " ").replace("-", " ").strip()
    return (
        "---\n"
        f"title: {title}\n"
        f"created: {date.today().isoformat()}\n"
        "source: markitdown\n"
        f"source_file: {source_path.name}\n"
        "classification: internal-use-only\n"
        "tags: []\n"
        "---\n\n"
    )


# ─── Inline base64 image extraction ──────────────────────────────────────────

# Matches ![any alt text](data:image/<ext>;base64,<data>)
B64_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]"
    r"\(data:image/(?P<ext>png|jpe?g|gif|webp);base64,(?P<data>[^)]+)\)"
)


def extract_base64_images(
    body: str, source_stem: str, attachments_dir: Path
) -> tuple[str, list[Path]]:
    """Decode each inline base64 image, save to attachments_dir, replace the
    inline blob with an Obsidian wiki-link.

    Output filenames: `<source_stem>-img-<N>.<ext>` (with `-2`, `-3`... suffix
    on collision so prior runs aren't clobbered).
    """
    attachments_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    counter = [0]

    def replace(m: re.Match) -> str:
        counter[0] += 1
        n = counter[0]
        ext = m.group("ext").lower().replace("jpeg", "jpg")
        b64 = m.group("data")
        try:
            img_bytes = base64.b64decode(b64, validate=False)
        except Exception:
            return m.group(0)  # leave intact on decode failure

        out_path = attachments_dir / f"{source_stem}-img-{n}.{ext}"
        attempt = 1
        while out_path.exists():
            attempt += 1
            out_path = attachments_dir / f"{source_stem}-img-{n}-{attempt}.{ext}"
        out_path.write_bytes(img_bytes)
        extracted.append(out_path)
        return f"![[{out_path.name}]]"

    return B64_IMAGE_RE.sub(replace, body), extracted


# ─── Stub image handling + source-archive recovery ──────────────────────────

# Matches Markitdown's truncated/stub form (`;base64...)` with no real data,
# or any other malformed data URL the prior decode pass failed on).
STUB_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]"
    r"\(data:image/(?P<ext>png|jpe?g|gif|webp|bmp|svg)"
    r"(?:;[a-z0-9-]+)*;base64[^,)][^)]*\)",
    re.IGNORECASE,
)

# Distinctive placeholder so a later --source re-run can locate prior stubs.
PLACEHOLDER_TEXT = (
    "*[Embedded image — Markitdown could not extract; see source document]*"
)
PLACEHOLDER_RE = re.compile(re.escape(PLACEHOLDER_TEXT))

# Combined matcher: either a fresh stub or a prior placeholder.
STUB_OR_PLACEHOLDER_RE = re.compile(
    rf"(?:{STUB_IMAGE_RE.pattern})|(?:{PLACEHOLDER_RE.pattern})",
    re.IGNORECASE,
)

# File extensions Obsidian renders natively (anything else gets wiki-linked
# but won't preview — see extract_archive_images).
RENDERABLE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}

# Office-archive media paths.
ARCHIVE_MEDIA_PREFIXES = {
    ".docx": "word/media/",
    ".pptx": "ppt/media/",
    ".xlsx": "xl/media/",
}


def extract_archive_images(
    source_path: Path, source_stem: str, attachments_dir: Path
) -> list[Path]:
    """Pull all images from a .docx / .pptx / .xlsx archive into
    `attachments_dir`. Returns extracted Path objects in archive order
    (which is generally document order). Non-renderable formats (WMF, EMF)
    are skipped — they'd just produce broken icons in Obsidian.

    Returns [] if the source isn't an Office archive, doesn't exist, or is
    unreadable as a ZIP.
    """
    if not source_path.exists():
        return []
    prefix = ARCHIVE_MEDIA_PREFIXES.get(source_path.suffix.lower())
    if prefix is None:
        return []

    attachments_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    try:
        with zipfile.ZipFile(source_path) as zf:
            members = sorted(
                m for m in zf.namelist()
                if m.startswith(prefix) and not m.endswith("/")
            )
            for i, member in enumerate(members, 1):
                ext = Path(member).suffix.lower().lstrip(".")
                if ext == "jpeg":
                    ext = "jpg"
                if ext not in RENDERABLE_EXTS:
                    continue
                out_path = attachments_dir / f"{source_stem}-source-img-{i}.{ext}"
                attempt = 1
                while out_path.exists():
                    attempt += 1
                    out_path = (
                        attachments_dir
                        / f"{source_stem}-source-img-{i}-{attempt}.{ext}"
                    )
                with zf.open(member) as src_f:
                    out_path.write_bytes(src_f.read())
                extracted.append(out_path)
    except (zipfile.BadZipFile, OSError):
        return []
    return extracted


def handle_stub_images(
    body: str, source_path: Path, attachments_dir: Path
) -> tuple[str, int, int, list[Path]]:
    """Find Markitdown image stubs (and any prior placeholders), replace each
    with either a wiki-link to a recovered source-archive image or a clean
    placeholder. Any extracted source images that didn't match a stub are
    appended in a "Images from source" section at the bottom.

    Returns (new_body, stubs_replaced, stubs_placeheld, source_images).
    """
    stub_count = sum(1 for _ in STUB_OR_PLACEHOLDER_RE.finditer(body))
    if stub_count == 0:
        return body, 0, 0, []

    # Try to recover images from the source archive (.docx/.pptx/.xlsx).
    source_images = extract_archive_images(
        source_path, source_path.stem, attachments_dir
    )

    iter_images = iter(source_images)
    replaced = 0
    placeheld = 0

    def replace(_m: re.Match) -> str:
        nonlocal replaced, placeheld
        try:
            img = next(iter_images)
        except StopIteration:
            placeheld += 1
            return PLACEHOLDER_TEXT
        replaced += 1
        return f"![[{img.name}]]"

    new_body = STUB_OR_PLACEHOLDER_RE.sub(replace, body)

    # Any extracted images we didn't consume → list at the bottom so nothing
    # silently disappears.
    leftover = list(iter_images)
    if leftover:
        new_body = new_body.rstrip() + "\n\n## Images from source\n\n"
        for img in leftover:
            new_body += f"![[{img.name}]]\n\n"

    return new_body, replaced, placeheld, source_images


# ─── Bullet normalization ────────────────────────────────────────────────────

BULLET_CHARS = "•○▪▸▶▹‣◦●⁃◾◼"
BULLET_LINE_RE = re.compile(rf"^([\t ]*)([{re.escape(BULLET_CHARS)}])(\s+)")


def normalize_bullets(body: str) -> tuple[str, int]:
    """Convert non-standard bullet markers to '- '. Tab indents become two-
    space indents while preserving the nesting depth.

    Returns (new_body, count_replaced).
    """
    out_lines: list[str] = []
    count = 0
    for line in body.splitlines():
        m = BULLET_LINE_RE.match(line)
        if m:
            count += 1
            indent = m.group(1)
            tabs = indent.count("\t")
            spaces_only = len(indent) - tabs
            depth = tabs + spaces_only // 2
            new_indent = "  " * depth
            rest = line[m.end():]
            out_lines.append(f"{new_indent}- {rest}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines), count


# ─── Heading promotion (strict patterns only) ────────────────────────────────

# Pattern A: "1. **Heading text**" on its own line
NUMBERED_BOLD_HEADING_RE = re.compile(r"^\s*\d+\.\s+\*\*([^*]+?)\*\*\s*$")

# Pattern B: "**Heading text:**" on its own line (bold label ending in colon)
BOLD_LABEL_HEADING_RE = re.compile(r"^\s*\*\*([^*]+?):\s*\*\*\s*$")


def promote_headings(body: str) -> tuple[str, int]:
    """Promote two strict patterns to ## headings. Skips fenced code blocks.
    Ensures a blank line precedes and follows each promoted heading so that
    CommonMark parsers don't fold the heading into an adjacent paragraph or
    list. Excess blanks are collapsed by normalize_whitespace afterward.

    Returns (new_body, count_promoted).
    """
    out_lines: list[str] = []
    count = 0
    in_code = False

    def append_heading(text: str) -> None:
        if out_lines and out_lines[-1] != "":
            out_lines.append("")
        out_lines.append(f"## {text.strip()}")
        out_lines.append("")  # may be collapsed later if excess

    for line in body.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            out_lines.append(line)
            continue
        if in_code:
            out_lines.append(line)
            continue

        m = NUMBERED_BOLD_HEADING_RE.match(line)
        if m:
            count += 1
            append_heading(m.group(1))
            continue

        m = BOLD_LABEL_HEADING_RE.match(line)
        if m:
            count += 1
            append_heading(m.group(1))
            continue

        out_lines.append(line)
    return "\n".join(out_lines), count


# ─── Whitespace normalization ────────────────────────────────────────────────

def normalize_whitespace(body: str) -> str:
    """Strip trailing spaces, collapse any run of blank lines to a single
    blank line, trim leading and trailing blank lines.
    """
    lines = [ln.rstrip() for ln in body.splitlines()]
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if ln == "":
            blanks += 1
            if blanks == 1:
                out.append(ln)
        else:
            blanks = 0
            out.append(ln)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


# ─── Public entrypoint ───────────────────────────────────────────────────────

def clean(
    content: str, source_path: Path, attachments_dir: Path
) -> tuple[str, dict]:
    """Run the full cleanup pipeline on Markitdown output.

    Args:
        content: raw markdown text from markitdown.convert()
        source_path: the original input file. Used for naming images, the
            generated frontmatter, AND (when it's a .docx/.pptx/.xlsx) for
            source-archive image recovery when Markitdown emitted stubs.
        attachments_dir: where to drop extracted images, typically
            ~/Obsidian/Z_attachments

    Returns:
        (cleaned_markdown, summary_dict)
    """
    fm, body = split_frontmatter(content)

    body, inline_images = extract_base64_images(
        body, source_path.stem, attachments_dir
    )
    body, stubs_replaced, stubs_placeheld, source_images = handle_stub_images(
        body, source_path, attachments_dir
    )
    body, bullets = normalize_bullets(body)
    body, headings = promote_headings(body)
    body = normalize_whitespace(body)

    fm_added = False
    if not fm:
        fm = generate_frontmatter(source_path)
        fm_added = True

    cleaned = fm + body
    summary = {
        "images_extracted": inline_images,
        "source_images": source_images,
        "bullets_normalized": bullets,
        "headings_promoted": headings,
        "stubs_replaced": stubs_replaced,
        "stubs_placeheld": stubs_placeheld,
        "frontmatter_added": fm_added,
    }
    return cleaned, summary


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _main() -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(
        description="Cleanup pass for Markitdown-converted markdown files.",
    )
    p.add_argument("file", type=Path, help="markdown file to clean")
    p.add_argument(
        "--source",
        type=Path,
        default=None,
        help=(
            "path to the original source file (.docx/.pptx/.xlsx) for stub "
            "image recovery; if omitted, the markdown file itself is used as "
            "the source reference (no archive recovery)"
        ),
    )
    p.add_argument(
        "--attachments-dir",
        type=Path,
        default=Path.home() / "Obsidian" / "Z_attachments",
        help="where to drop extracted images (default: ~/Obsidian/Z_attachments)",
    )
    p.add_argument(
        "--in-place",
        action="store_true",
        help="overwrite the file in place (otherwise writes cleaned text to stdout)",
    )
    args = p.parse_args()

    src_for_cleanup = args.source if args.source else args.file

    raw = args.file.read_text(encoding="utf-8")
    cleaned, summary = clean(raw, src_for_cleanup, args.attachments_dir)

    if args.in_place:
        args.file.write_text(cleaned, encoding="utf-8")
        print(f"cleaned: {args.file}", file=sys.stderr)
    else:
        sys.stdout.write(cleaned)

    print(
        f"  inline base64 images:  {len(summary['images_extracted'])}",
        file=sys.stderr,
    )
    for img in summary["images_extracted"]:
        print(f"      -> {img.name}", file=sys.stderr)
    print(
        f"  source-archive images: {len(summary['source_images'])}",
        file=sys.stderr,
    )
    for img in summary["source_images"]:
        print(f"      -> {img.name}", file=sys.stderr)
    print(f"  stubs replaced:        {summary['stubs_replaced']}", file=sys.stderr)
    print(f"  stubs placeheld:       {summary['stubs_placeheld']}", file=sys.stderr)
    print(f"  bullets normalized:    {summary['bullets_normalized']}", file=sys.stderr)
    print(f"  headings promoted:     {summary['headings_promoted']}", file=sys.stderr)
    print(f"  frontmatter added:     {summary['frontmatter_added']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
