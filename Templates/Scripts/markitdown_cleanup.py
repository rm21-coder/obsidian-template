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
  7. Prepend the pipeline's own frontmatter block:
       title, created (today), source: markitdown, source_file,
       classification: internal-use-only, tags: []
     A converted document is outsider text, so its OWN leading frontmatter
     never becomes the note's: it could set `classification: public` or any
     other property. That block is kept visibly, as a fenced code block at the
     top of the body, so nothing in the document silently disappears. (The CLI
     keeps a file's existing frontmatter, because it re-cleans notes that are
     already in the vault; see `keep_frontmatter`.)

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
      frontmatter_added   bool         True if the pipeline wrote the block
      source_frontmatter  bool         True if the document's own block was
                                       fenced into the body
      images_skipped      int          images not written: over a size or
                                       count limit (see Limits below)

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
import json
import re
import zipfile
import zlib
from datetime import date
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import templater_guard  # noqa: E402  -- outside text must not run as Templater code


# ─── Frontmatter ─────────────────────────────────────────────────────────────

FRONTMATTER_RE = re.compile(r"\A(---\s*\n.*?\n---\s*\n)", re.DOTALL)


def split_frontmatter(content: str) -> tuple[str, str]:
    """Return (frontmatter_block, body). Empty frontmatter if none present.

    Leading byte-order marks are ignored, as Obsidian ignores them.
    """
    stripped = content.lstrip("\ufeff")
    m = FRONTMATTER_RE.match(stripped)
    if m:
        return m.group(1), stripped[m.end():]
    return "", content


def fence_source_frontmatter(block: str) -> str:
    """The document's own frontmatter as inert text at the top of the body.

    The fence is longer than any backtick run inside the block, so the block
    cannot close it early and put its lines back into the note as markdown.
    """
    inner = block.rstrip("\n")
    longest = max((len(r) for r in re.findall(r"`+", inner)), default=0)
    fence = "`" * max(3, longest + 1)
    return (
        "Frontmatter from the source document (kept as text, not applied):\n\n"
        f"{fence}yaml\n{inner}\n{fence}\n\n"
    )


# Control characters, every line break included, and the three characters
# that are line breaks to YAML 1.1 but not to Obsidian's YAML 1.2. Same rule
# as meeting_prepopulate's _YAML_CTRL_RE, plus the rest of C1, and the
# characters YAML does not allow in a document at all: Unicode noncharacters
# (U+FDD0-U+FDEF and U+xFFFE/U+xFFFF in every plane) and lone surrogates (a
# filename that is not valid UTF-8 arrives as them). Obsidian's js-yaml and
# PyYAML both reject the whole block on U+FFFE/U+FFFF, so the note would
# show no properties.
_YAML_CTRL_RE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u2028\u2029\ufdd0-\ufdef\ud800-\udfff"
    + "".join(chr(p | 0xFFFE) + chr(p | 0xFFFF) for p in range(0, 0x110000, 0x10000))
    + "]+"
)


def yaml_quoted(value: str) -> str:
    """Outside text as one double-quoted YAML scalar.

    The source filename belongs to whoever sent the file. Unquoted, names
    such as `[DRAFT] Budget`, `Q3: plan` or `Report #3` are invalid YAML or
    lose text, and Obsidian then shows no properties for the note. A JSON
    string is a valid YAML double-quoted scalar with every escape correct.
    """
    return json.dumps(_YAML_CTRL_RE.sub(" ", value).strip(), ensure_ascii=False)


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
        f"title: {yaml_quoted(title)}\n"
        f"created: {date.today().isoformat()}\n"
        "source: markitdown\n"
        f"source_file: {yaml_quoted(source_path.name)}\n"
        "classification: internal-use-only\n"
        "tags: []\n"
        "---\n\n"
    )


# ─── Limits ──────────────────────────────────────────────────────────────────
#
# A converted document comes from outside (an emailed attachment dropped on the
# dropper). Without limits, ~30 bytes of repeated `![](data:image/png;base64,
# AAAA)` text made one file each in Z_attachments, and an Office archive whose
# media member inflates to gigabytes was read whole into memory and written
# into the vault. Past a limit an image is not written: an inline one becomes
# OMITTED_TEXT, an archive one is skipped (its stub then gets the placeholder).
#
# These limits cover only THIS pass. MarkItDown's own conversion runs first
# and reads archive members whole; the converters guard that separately with
# archive_limits.refusal() before calling md.convert().

MAX_INLINE_IMAGES = 200                   # inline data: images per document
MAX_INLINE_BYTES = 100 * 1024 * 1024      # decoded bytes, all inline images
MAX_ARCHIVE_MEMBERS = 500                 # media members considered per archive
MAX_ARCHIVE_MEMBER_BYTES = 25 * 1024 * 1024    # one decompressed member
MAX_ARCHIVE_TOTAL_BYTES = 200 * 1024 * 1024    # all members, decompressed
_COPY_CHUNK = 1024 * 1024

OMITTED_TEXT = "*[Embedded image omitted — over the per-document image limit]*"


# ─── Inline base64 image extraction ──────────────────────────────────────────

# Matches ![any alt text](data:image/<ext>;base64,<data>)
B64_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]"
    r"\(data:image/(?P<ext>png|jpe?g|gif|webp);base64,(?P<data>[^)]+)\)"
)


def extract_base64_images(
    body: str, source_stem: str, attachments_dir: Path,
    skipped: list[int] | None = None,
) -> tuple[str, list[Path]]:
    """Decode each inline base64 image, save to attachments_dir, replace the
    inline blob with an Obsidian wiki-link.

    Output filenames: `<source_stem>-img-<N>.<ext>` (with `-2`, `-3`... suffix
    on collision so prior runs aren't clobbered).

    At most MAX_INLINE_IMAGES files and MAX_INLINE_BYTES decoded bytes are
    written; any image past either limit is replaced with OMITTED_TEXT and
    counted in `skipped[0]` when a counter is passed.
    """
    attachments_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    counter = [0]
    total = [0]

    def replace(m: re.Match) -> str:
        counter[0] += 1
        n = counter[0]
        ext = m.group("ext").lower().replace("jpeg", "jpg")
        b64 = m.group("data")
        # Size from the encoded length first, so an oversized blob is never
        # decoded at all.
        if (len(extracted) >= MAX_INLINE_IMAGES
                or total[0] + len(b64) * 3 // 4 > MAX_INLINE_BYTES):
            if skipped is not None:
                skipped[0] += 1
            return OMITTED_TEXT
        try:
            img_bytes = base64.b64decode(b64, validate=False)
        except Exception:
            return m.group(0)  # leave intact on decode failure
        total[0] += len(img_bytes)

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


def _copy_bounded(src_f, out_path: Path, limit: int) -> int | None:
    """Stream src_f into out_path. Returns bytes written, or None (and no
    file left behind) when more than `limit` bytes arrive. The declared
    file_size is checked before this, but it is the archive's own claim."""
    written = 0
    try:
        with out_path.open("xb") as out_f:
            while True:
                chunk = src_f.read(min(_COPY_CHUNK, limit - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    break
                out_f.write(chunk)
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    if written > limit:
        out_path.unlink(missing_ok=True)
        return None
    return written


def extract_archive_images(
    source_path: Path, source_stem: str, attachments_dir: Path,
    skipped: list[int] | None = None,
) -> list[Path]:
    """Pull all images from a .docx / .pptx / .xlsx archive into
    `attachments_dir`. Returns extracted Path objects in archive order
    (which is generally document order). Non-renderable formats (WMF, EMF)
    are skipped — they'd just produce broken icons in Obsidian.

    Returns [] if the source isn't an Office archive, doesn't exist, or is
    unreadable as a ZIP.

    Bounded against a zip bomb: only the first MAX_ARCHIVE_MEMBERS media
    members are considered, a member over MAX_ARCHIVE_MEMBER_BYTES is
    skipped, and nothing more is written once MAX_ARCHIVE_TOTAL_BYTES have
    been. Sizes are checked against the declared file_size and again while
    streaming, since the declared size is the archive's own claim. Skipped
    members are counted in `skipped[0]` when a counter is passed.
    """
    if not source_path.exists():
        return []
    prefix = ARCHIVE_MEDIA_PREFIXES.get(source_path.suffix.lower())
    if prefix is None:
        return []

    attachments_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total = 0

    def skip(n: int = 1) -> None:
        if skipped is not None:
            skipped[0] += n

    try:
        with zipfile.ZipFile(source_path) as zf:
            infos = sorted(
                (m for m in zf.infolist()
                 if m.filename.startswith(prefix) and not m.filename.endswith("/")),
                key=lambda m: m.filename,
            )
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                skip(len(infos) - MAX_ARCHIVE_MEMBERS)
                infos = infos[:MAX_ARCHIVE_MEMBERS]
            for i, info in enumerate(infos, 1):
                member = info.filename
                ext = Path(member).suffix.lower().lstrip(".")
                if ext == "jpeg":
                    ext = "jpg"
                if ext not in RENDERABLE_EXTS:
                    continue
                limit = min(MAX_ARCHIVE_MEMBER_BYTES,
                            MAX_ARCHIVE_TOTAL_BYTES - total)
                if info.file_size > limit:
                    skip()
                    continue
                out_path = attachments_dir / f"{source_stem}-source-img-{i}.{ext}"
                attempt = 1
                while out_path.exists():
                    attempt += 1
                    out_path = (
                        attachments_dir
                        / f"{source_stem}-source-img-{i}-{attempt}.{ext}"
                    )
                with zf.open(info) as src_f:
                    written = _copy_bounded(src_f, out_path, limit)
                if written is None:
                    skip()
                    continue
                total += written
                extracted.append(out_path)
    except (zipfile.BadZipFile, OSError, EOFError, zlib.error,
            RuntimeError, NotImplementedError):
        # A damaged member (CRC error, truncated deflate stream, encryption)
        # aborts the whole recovery. Remove what this run already wrote:
        # the caller gets [] and so would never reference those files, and
        # every re-run would leave another orphaned copy in Z_attachments.
        for p in extracted:
            p.unlink(missing_ok=True)
        skip(len(extracted) + 1)
        return []
    return extracted


def handle_stub_images(
    body: str, source_path: Path, attachments_dir: Path,
    skipped: list[int] | None = None,
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
        source_path, source_path.stem, attachments_dir, skipped
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
    content: str, source_path: Path, attachments_dir: Path,
    keep_frontmatter: bool = False,
) -> tuple[str, dict]:
    """Run the full cleanup pipeline on Markitdown output.

    Args:
        content: raw markdown text from markitdown.convert()
        source_path: the original input file. Used for naming images, the
            generated frontmatter, AND (when it's a .docx/.pptx/.xlsx) for
            source-archive image recovery when Markitdown emitted stubs.
        attachments_dir: where to drop extracted images, typically
            ~/Obsidian/Z_attachments
        keep_frontmatter: keep `content`'s own leading frontmatter as the
            note's. Only for re-cleaning a note already in the vault (the
            CLI). The converters leave it False: a converted document is
            outsider text, and its own block could declare
            `classification: public`.

    Returns:
        (cleaned_markdown, summary_dict)
    """
    fm, body = split_frontmatter(content)

    skipped = [0]
    body, inline_images = extract_base64_images(
        body, source_path.stem, attachments_dir, skipped
    )
    body, stubs_replaced, stubs_placeheld, source_images = handle_stub_images(
        body, source_path, attachments_dir, skipped
    )
    body, bullets = normalize_bullets(body)
    body, headings = promote_headings(body)
    body = normalize_whitespace(body)

    fm_added = False
    source_fm = False
    if fm and not keep_frontmatter:
        # The document's block stays readable, but only as body text.
        body = fence_source_frontmatter(fm) + body
        fm = ""
        source_fm = True
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
        "source_frontmatter": source_fm,
        "images_skipped": skipped[0],
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
    # The CLI re-cleans a file the user already has; its frontmatter is kept.
    cleaned, summary = clean(
        raw, src_for_cleanup, args.attachments_dir, keep_frontmatter=True
    )
    # A converted document is text from outside the vault.
    cleaned = templater_guard.neutralize(cleaned)

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
    print(f"  images skipped:        {summary['images_skipped']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
