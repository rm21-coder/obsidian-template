#!/usr/bin/env python3
"""
youtube_summarize.py — One-shot YouTube → Obsidian summarizer.

Pulls a video's auto-generated (or manual) captions via yt-dlp, sends the
transcript to a model for summarization, and writes a markdown note into
~/Obsidian/Clippings/YouTube/ using the same frontmatter convention as
notes produced by Obsidian Web Clipper.

Summarization goes through llm_endpoint.py, the same endpoint and credential
every other model call in the vault uses. This script therefore needs NO key of
its own, is metered in usage_log alongside the tagger and classifier, and is
governed by an institutional gateway when one is configured.

It used to call Gemini, which is worth recording because the reasoning is not
obvious: yt-dlp fetches YouTube's own caption track (manual when present,
Google's ASR otherwise) and this script parses it to plain text locally. The
model never receives the URL, so Google had no privileged access to the
transcript and no advantage from having produced it. On a blind three-arm
comparison over real ASR text, Claude with this exact prompt was preferred over
gemini-2.5-flash, so the second API key bought nothing.

Single video:
    youtube_summarize.py "https://www.youtube.com/watch?v=XXXX"

Playlist:
    youtube_summarize.py --playlist "https://www.youtube.com/playlist?list=YYYY"

Only YouTube URLs are accepted (youtube.com, www./m./music.youtube.com,
youtu.be), and yt-dlp runs with its YouTube extractors alone. yt-dlp's generic
extractor would otherwise fetch whatever a pasted page or feed names --
including loopback and LAN addresses -- with no SSRF guard of ours in the way.

Flags:
    --model NAME      model id (default: claude-sonnet-5; env: YOUTUBE_MODEL)
    --out DIR         Output directory (default: ~/Obsidian/Clippings/YouTube)
    --max N           Cap playlist runs at N videos (default: no cap)
    --dry-run         Fetch transcript but skip the API call and write
    --verbose         Log progress to stderr

Credentials come from llm_endpoint.py: ANTHROPIC_API_KEY by default, or
LLM_BASE_URL + LLM_API_KEY_NAME on an institutional gateway. That is the same
credential the tagger already uses, so there is nothing extra to set up.

Exit codes:
    0  success (all videos summarized or already existed)
    1  fatal error (no key, no transcript, API failure on a single-video run)
    2  partial failure (one or more videos in a playlist failed; rest succeeded)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import templater_guard  # noqa: E402  -- outside text must not run as Templater code

import textwrap
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# ---------- venv bootstrap --------------------------------------------------
#
# The shebang is `/usr/bin/env python3`, so the interpreter is whatever the
# caller's PATH hands us. Anything launching this with the stock macOS PATH --
# an Obsidian plugin, a launchd job, a .app wrapper -- lands on
# /usr/bin/python3 (system Python 3.9, no third-party packages) and dies on the
# import below. The launchd plists in this directory dodge that by naming
# .venv/bin/python3 outright; this covers every other caller. Re-exec into the
# sibling venv unless we're already inside one. Path is derived from __file__,
# so it survives the genericized repo copy. The sentinel keeps a broken venv
# from looping forever.
_HERE = Path(__file__).resolve()
_VENV_PY = _HERE.parent / ".venv" / (
    "Scripts/python.exe" if os.name == "nt" else "bin/python3"
)
# __name__ guard first, and it is load-bearing. os.execv REPLACES the running
# process, so at module scope this fires on `import` -- and under pytest the
# process it replaces is pytest. Importing this module while collecting its
# test file therefore killed the runner before it could report: the documented
# entry point returned exit 2 with zero test output, on any machine where the
# vault venv existed. All 599 tests were unreachable and the failure read like
# a pytest config problem. Found on a Mac Studio full-cycle run 2026-08-25 and
# reproduced on the primary machine by creating the venv.
#
# Re-execing is still right when this file is run as a command; it is never
# right on import. Pinned by
# test_static.py::TestNoModuleReExecsOnImport.
if (
    __name__ == "__main__"
    and sys.prefix == sys.base_prefix
    and _VENV_PY.exists()
    and not os.environ.get("_VENV_BOOTSTRAPPED")
):
    os.environ["_VENV_BOOTSTRAPPED"] = "1"
    os.execv(str(_VENV_PY), [str(_VENV_PY), str(_HERE), *sys.argv[1:]])

from dotenv import load_dotenv

# The endpoint credential comes from the shared secrets file / keystore.
load_dotenv(Path.home() / "dev" / "secrets" / ".env")

# Summarizing a long transcript against a strict output spec is not a
# Haiku-class task the way tagging is, so this is a Sonnet default rather than
# the tagger's model. YOUTUBE_MODEL overrides it (and reaches the scheduled
# path, because 20-secrets persists it to .env); --model overrides that.
DEFAULT_MODEL = "claude-sonnet-5"


def resolve_model(cli_model: str | None = None) -> str:
    return (cli_model or (os.environ.get("YOUTUBE_MODEL") or "").strip()
            or DEFAULT_MODEL)
DEFAULT_OUT = Path.home() / "Obsidian" / "Clippings" / "YouTube"

# Filename hygiene for the note written into DEFAULT_OUT.
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9 _\-().,&!]")
COLLAPSE_DASH = re.compile(r"\s*-\s*")

# ---------- SSRF guard ------------------------------------------------------
#
# Every URL from a remote response that this script fetches itself, or hands
# to yt-dlp, goes through url_safety: caption-track URLs out of yt-dlp's info
# dict (attacker-chosen for any non-YouTube page yt-dlp's generic extractor
# reads) and playlist entries (attacker-chosen for an RSS feed). This module
# used to carry its own copy of is_safe_url and then urlopen the caption URL,
# which followed redirects without re-checking them and read the body without
# a cap (M-DASH 224/358). The names below are re-exported so the predicate has
# exactly one implementation.

import url_safety  # noqa: E402

DISALLOWED_TLDS = url_safety.DISALLOWED_TLDS
LOOPBACK_NAMES = url_safety.LOOPBACK_NAMES
is_safe_url = url_safety.is_safe_url

# A caption track is text; an hour of json3 is well under 2 MB. The cap keeps a
# hostile track served as an endless stream from growing the process.
CAPTION_MAX_BYTES = 5 * 1024 * 1024

# yt-dlp makes its own requests, outside url_safety, so it is held to YouTube:
# the operator's URL and every playlist entry must name one of these hosts,
# and yt-dlp loads only its YouTube extractors (--use-extractors matches each
# name exactly). With the default set, the generic extractor follows a page's
# <video><source> or a 302 anywhere, 127.0.0.1 included.
YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com",
                           "music.youtube.com", "youtu.be"})
VIDEO_EXTRACTORS = "youtube"
PLAYLIST_EXTRACTORS = "youtube,youtube:tab,youtube:playlist"
YTDLP_TIMEOUT_SECONDS = 600


def youtube_url(url: object) -> tuple[str | None, str]:
    """(url fit to hand to yt-dlp, "") or (None, reason).

    The fragment is dropped: yt-dlp reads "#__youtubedl_smuggle=..." from a
    URL's fragment as extractor options, and a feed entry could carry one.
    Userinfo, an explicit port, backslashes, whitespace and control
    characters are refused outright -- each is a way for two URL parsers to
    disagree about which host a string names.
    """
    if not isinstance(url, str):
        return None, f"not a string: {type(url).__name__}"
    if "\\" in url or any(ord(c) <= 0x20 or 0x7f <= ord(c) <= 0x9f for c in url):
        return None, "backslash, whitespace or control character in URL"
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None, "unparseable URL"
    if parts.scheme not in ("http", "https"):
        return None, f"disallowed scheme: {parts.scheme!r}"
    if "@" in parts.netloc:
        return None, "userinfo in URL"
    if port is not None:
        return None, "explicit port in URL"
    if host not in YOUTUBE_HOSTS:
        return None, f"not a YouTube host: {host!r}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       parts.query, "")), ""


# ---------- Logging ----------------------------------------------------------

def log(msg: str, *, verbose: bool) -> None:
    if verbose:
        print(f"[yt-sum] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"[yt-sum] error: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# ---------- Keychain ---------------------------------------------------------

# ---------- yt-dlp -----------------------------------------------------------

def run_ytdlp(args: list[str]) -> dict | list[dict]:
    """Run yt-dlp -J and return parsed JSON. Raises on failure."""
    # Invoke via the interpreter's module so it works when yt-dlp is a pip dep
    # in the venv but its console script isn't on PATH (common on Windows).
    cmd = [sys.executable, "-m", "yt_dlp", "-J", "--no-warnings", *args]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True,
                              timeout=YTDLP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"yt-dlp timed out after {YTDLP_TIMEOUT_SECONDS}s") from None
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def enumerate_playlist(url: str) -> list[str]:
    """Return ordered video URLs for a playlist, unsafe entries dropped.

    Entry URLs come from remote playlist metadata, and each one is handed
    back to yt-dlp, which fetches it. So each must be a YouTube URL (see
    youtube_url): otherwise an entry could point yt-dlp at the LAN, name a
    yt-dlp option ("--cookies-from-browser=...") in place of a URL, or
    smuggle extractor options in its fragment.
    """
    data = run_ytdlp(["--flat-playlist", "--use-extractors",
                      PLAYLIST_EXTRACTORS, "--", url])
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        urls = []
        for e in data["entries"]:
            if not isinstance(e, dict):
                continue
            raw = e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}"
            u, reason = youtube_url(raw)
            if u is None:
                print(f"[yt-sum] skipping playlist entry "
                      f"{url_safety.redact_url(raw)}: {reason}",
                      file=sys.stderr, flush=True)
                continue
            urls.append(u)
        return urls
    raise RuntimeError("not a playlist or no entries returned")


def fetch_video(url: str) -> dict:
    """Return yt-dlp's full info dict for a single video URL.

    The dict already contains 'subtitles' and 'automatic_captions' keyed by
    language with track URLs we fetch over HTTP later — so we don't ask
    yt-dlp to write subtitle/info files to disk, and we don't add
    --print-json (which would collide with the -J that run_ytdlp passes).
    """
    data = run_ytdlp([
        "--skip-download",
        "--no-playlist",
        "--use-extractors", VIDEO_EXTRACTORS,
        # Everything after "--" is a URL, never an option, whatever it starts
        # with. Playlist entries come from remote metadata.
        "--",
        url,
    ])
    if isinstance(data, list):
        data = data[0]
    return data


def extract_transcript(info: dict) -> str:
    """Pull caption text out of yt-dlp's info dict.

    yt-dlp lists captions under 'subtitles' (manual) and 'automatic_captions'
    (auto). We prefer manual English, fall back to auto English, then any
    English-prefixed track. Each track is a list of {ext, url} dicts; we ask
    for the json3 format (preferred — has clean text) or vtt as fallback.
    """
    candidates = []
    for source in ("subtitles", "automatic_captions"):
        tracks = info.get(source) or {}
        if not isinstance(tracks, dict):
            continue
        # Prefer "en", then any en-* (en-US, en-GB, en-orig, etc.)
        keys = sorted((k for k in tracks if isinstance(k, str)),
                      key=lambda k: (k != "en", not k.startswith("en"), k))
        for k in keys:
            if not (k == "en" or k.startswith("en")):
                continue
            fmts = tracks[k] if isinstance(tracks[k], list) else []
            for fmt in fmts:
                if not isinstance(fmt, dict):
                    continue
                ext = fmt.get("ext", "")
                url = fmt.get("url")
                # A track that is not a well-formed entry is skipped, never
                # an exception that loses the remaining tracks.
                if ext in ("json3", "vtt", "srt", "ttml") and isinstance(url, str):
                    candidates.append((source, k, ext, url))

    for source, lang, ext, url in candidates:
        # The URLs come from yt-dlp's parsed response -- a remote input, and
        # for a non-YouTube page an attacker's -- so this is the SSRF
        # perimeter. safe_fetch checks every hop (redirects included) with
        # is_safe_url, connects to the address it checked, and caps the body.
        raw = url_safety.safe_fetch(
            url, max_bytes=CAPTION_MAX_BYTES,
            log=lambda m: log(f"caption track: {m}", verbose=True))
        if raw is None:
            continue
        body = raw.decode("utf-8", errors="replace")
        text = parse_caption_body(body, ext)
        if text.strip():
            return text
    return ""


def parse_caption_body(body: str, ext: str) -> str:
    if ext == "json3":
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return ""
        parts = []
        for ev in data.get("events", []):
            for seg in ev.get("segs") or []:
                t = seg.get("utf8", "")
                if t and t != "\n":
                    parts.append(t)
        return collapse_whitespace(" ".join(parts))
    # vtt / srt / ttml: strip cue numbers, timestamps, tags
    lines = []
    for raw in body.splitlines():
        s = raw.strip()
        if not s or s == "WEBVTT":
            continue
        if "-->" in s:
            continue
        if re.fullmatch(r"\d+", s):
            continue
        # strip <00:00:00.000> inline tags and basic html
        s = re.sub(r"<[^>]+>", "", s)
        if s:
            lines.append(s)
    return collapse_whitespace(" ".join(lines))


def collapse_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ---------- Summarization ----------------------------------------------------

SUMMARY_PROMPT = """\
You are summarizing a YouTube video for a knowledge worker who maintains a
personal Obsidian vault. Produce a clean markdown summary with:

1. A 2-3 sentence overview at the very top (no heading, just plain prose).
2. A "## Key takeaways" section: 5-10 concise bullets, each one a complete
   thought the reader can act on or remember.
3. A "## Notable points" section: 3-6 short paragraphs (NOT bullets) covering
   nuance, surprising claims, or specific examples that matter.
4. A "## Suggested tags" section: a single comma-separated line of 3-7
   lowercase topic tags (no hashes, no quotes), suitable for an Obsidian
   tags frontmatter field. These should reflect the SUBJECT of the video,
   not generic descriptors like "video" or "summary".

Do NOT include the video title, channel name, or URL in your output — those
are already captured in the note's frontmatter. Do NOT begin with "This
video..." or "In this video..." — start directly with the substance.

Transcript follows:

---
{transcript}
---
"""


def _text_blocks(resp) -> str:
    """Join a Message's text blocks.

    Not content[0].text. Models with extended thinking enabled return a
    ThinkingBlock first, so indexing block zero raises AttributeError -- and it
    fails only against those models, so a smoke test on a different one passes
    and production breaks. Filter by type instead.
    """
    return "\n".join(b.text for b in resp.content
                     if getattr(b, "type", None) == "text").strip()


def summarize(transcript: str, *, model: str, verbose: bool) -> str:
    """Summarize via llm_endpoint -- same endpoint and credential as the tagger.

    Two deliberate choices here, both of which cost a production failure to
    learn and are easy to "clean up" back into bugs:

    1. The prompt stays a SINGLE user turn, exactly as the Gemini path sends
       it. Moving the instructions into a system prompt is the obvious
       refactor, and it measurably regressed output on a blind three-arm
       comparison (Gemini vs Claude single-turn vs Claude system-prompt, same
       transcript): the system-prompt arm was the only one that violated the
       format spec, opening with a title heading the prompt forbids. Claude
       single-turn won the read. Leave the structure alone.

    2. No `temperature`. Current Claude models reject it outright -- through a
       LiteLLM-style gateway that surfaces as a 400, "`temperature` is
       deprecated for this model", which looks like a gateway
       misconfiguration rather than a parameter problem.
    """
    import llm_endpoint

    # max_retries is explicit because it replaces a hand-rolled 429/5xx backoff
    # loop that the Gemini path carried. The SDK does the same job; saying the
    # number here keeps it a deliberate policy rather than an inherited default.
    client = llm_endpoint.client(max_retries=3)
    log(f"calling {model} via {llm_endpoint.describe()}...", verbose=verbose)
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user",
                   "content": SUMMARY_PROMPT.format(transcript=transcript)}],
    )
    try:
        import usage_log
        usage_log.record("youtube_summarize", model, resp.usage)
    except Exception:
        # Metering is observability, never a reason to lose a finished summary.
        pass

    text = _text_blocks(resp)
    if not text:
        raise RuntimeError(f"{model} returned no text blocks")
    return text


def safe_filename(title: str, max_len: int = 120) -> str:
    name = SAFE_FILENAME.sub("", title).strip()
    name = re.sub(r"\s+", " ", name)
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "Untitled"


def parse_suggested_tags(summary_md: str) -> list[str]:
    """Pull the comma list out of '## Suggested tags' and remove that section
    from the body so it doesn't appear twice."""
    m = re.search(r"##\s+Suggested tags\s*\n+(.+?)(?:\n##|\Z)",
                  summary_md, flags=re.DOTALL)
    if not m:
        return []
    raw = m.group(1).strip().splitlines()[0]
    tags = [t.strip().lower() for t in raw.split(",") if t.strip()]
    # drop bullets / quotes / leading hashes that the model might still emit
    cleaned = []
    for t in tags:
        t = t.lstrip("-* ").strip("\"'#").strip().replace(" ", "-")
        # The model's output is steerable by the transcript, and each tag is
        # written as a bare YAML list item. Keep only tag characters, so "[x",
        # "*x" or "!!python/..." cannot turn into YAML syntax (M-DASH 231).
        if SAFE_TAG.fullmatch(t):
            cleaned.append(t)
    return cleaned


def strip_suggested_tags_section(summary_md: str) -> str:
    return re.sub(r"\n*##\s+Suggested tags[\s\S]*$", "", summary_md).rstrip() + "\n"


# A letter or digit in any script, then letters, digits, _ / - (Obsidian's tag
# characters). Nothing YAML reads as syntax at the start of a plain scalar.
SAFE_TAG = re.compile(r"[^\W_][\w/-]{0,63}")

# Line breaks (YAML and str.splitlines both honour \x85, \u2028, \u2029), the
# other C0/C1 control characters, and code points PyYAML refuses to read at
# all (lone surrogates, U+FFFE/U+FFFF). One in a title could end the
# frontmatter early or make the whole block unreadable.
_CONTROL_CHARS = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff\ufffe\uffff]")


def yaml_escape(value: object) -> str:
    """A double-quoted YAML string for any value, on one line.

    Always quoted: a plain scalar is read as syntax or as another type far
    too easily -- a leading ',' ']' '}' or ' - ' breaks the block, and
    null / ~ / yes / 123 / 2026-09-01 come back as None, True, an int or a
    date. A JSON string is a valid YAML double-quoted scalar, so json.dumps
    does the escaping. Control characters become spaces first: titles and
    uploaders come from the page yt-dlp read, and a newline in one would
    close the frontmatter early (M-DASH 223).
    """
    if value is None:
        value = ""
    return json.dumps(_CONTROL_CHARS.sub(" ", str(value)), ensure_ascii=False)


def build_frontmatter(info: dict, suggested_tags: list[str], description: str) -> str:
    # Every field is remote metadata of whatever type the page produced, so
    # each is coerced to text here rather than trusted to be a str.
    title = str(info.get("title") or "Untitled")
    url = str(info.get("webpage_url") or info.get("original_url") or "")
    author = str(info.get("uploader") or info.get("channel") or "")
    upload_date = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", upload_date):
        published = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
    else:
        published = upload_date or None
    duration = info.get("duration")
    try:
        duration_str = format_duration(duration) if duration else ""
    except (TypeError, ValueError, OverflowError):
        duration_str = ""

    # 'clippings' tag retired 2026-05-27; keep the meaningful 'youtube' axis.
    base_tags = ["youtube"]
    seen = set(base_tags)
    all_tags = list(base_tags)
    for t in suggested_tags:
        if t and t not in seen:
            seen.add(t)
            all_tags.append(t)

    lines = [
        "---",
        f"title: {yaml_escape(title)}",
        f"source: {yaml_escape(url)}",
        f"author: {yaml_escape(author)}",
        f"published: {'null' if published is None else yaml_escape(published)}",
        f"created: {date.today().isoformat()}",
        f"duration: {yaml_escape(duration_str)}",
        f"description: {yaml_escape(description)}",
        # Data classification (see Knowledge/Data Classification.md if you
        # keep one). Public YouTube content defaults to `public`.
        "classification: internal-use-only",
        "tags:",
    ]
    for t in all_tags:
        lines.append(f"- {yaml_escape(t)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def format_duration(seconds: int) -> str:
    seconds = int(seconds)
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def first_paragraph(summary_md: str, max_len: int = 240) -> str:
    """Use the opening paragraph of the summary as the YAML 'description'."""
    for block in summary_md.strip().split("\n\n"):
        s = block.strip()
        if not s or s.startswith("#"):
            continue
        s = collapse_whitespace(s)
        return s if len(s) <= max_len else s[:max_len].rsplit(" ", 1)[0] + "…"
    return ""


# ---------- Per-video pipeline ----------------------------------------------

def process_video(url: str, *, out_dir: Path, model: str,
                  dry_run: bool, verbose: bool) -> Path | None:
    log(f"fetching metadata: {url}", verbose=verbose)
    info = fetch_video(url)

    title = str(info.get("title") or info.get("id") or "Untitled")
    note_path = out_dir / f"{safe_filename(title)}.md"
    if note_path.exists():
        log(f"already exists, skipping: {note_path.name}", verbose=verbose)
        return note_path

    transcript = extract_transcript(info)
    if not transcript:
        raise RuntimeError(f"no transcript available for: {title}")

    log(f"transcript: {len(transcript):,} chars", verbose=verbose)

    if dry_run:
        log("dry-run: skipping API call", verbose=verbose)
        return None

    summary = summarize(transcript, model=model, verbose=verbose)
    suggested = parse_suggested_tags(summary)
    body = strip_suggested_tags_section(summary)
    description = first_paragraph(body)

    frontmatter = build_frontmatter(info, suggested, description)
    out_dir.mkdir(parents=True, exist_ok=True)
    note_path.write_text(templater_guard.neutralize(frontmatter + "\n" + body), encoding="utf-8")
    log(f"wrote {note_path}", verbose=verbose)
    return note_path


# ---------- Main -------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Summarize YouTube videos into Obsidian notes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              youtube_summarize.py "https://youtu.be/abcd1234"
              youtube_summarize.py --playlist "https://youtube.com/playlist?list=..."
              youtube_summarize.py --max 5 --playlist "https://..."
        """),
    )
    p.add_argument("url", help="YouTube video URL (or playlist URL with --playlist)")
    p.add_argument("--playlist", action="store_true",
                   help="treat URL as a playlist and process each video")
    p.add_argument("--model", default=None,
                   help=f"model id (default: {DEFAULT_MODEL}; env: YOUTUBE_MODEL)")
    p.add_argument("--out", default=str(DEFAULT_OUT), help=f"output dir (default: {DEFAULT_OUT})")
    p.add_argument("--max", type=int, default=0, help="cap playlist runs (0 = no cap)")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch transcript but skip API call and file write")
    p.add_argument("--verbose", action="store_true", help="log progress to stderr")
    args = p.parse_args(argv)

    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        die("yt-dlp not installed. It ships in requirements.lock; re-run the "
            "installer (or update), which installs the lock hash-checked into the venv.")

    out_dir = Path(os.path.expanduser(args.out)).resolve()

    target, reason = youtube_url(args.url)
    if target is None:
        die(f"only YouTube URLs are supported ({reason})")

    model = resolve_model(args.model)
    # No credential check here: llm_endpoint raises MissingCredential naming the
    # exact secret and how to store it, which beats a second guess at it.

    if args.playlist:
        try:
            urls = enumerate_playlist(target)
        except RuntimeError as e:
            die(str(e))
        if args.max:
            urls = urls[:args.max]
        log(f"playlist: {len(urls)} videos", verbose=args.verbose)
        ok, failed = 0, []
        for i, u in enumerate(urls, 1):
            log(f"[{i}/{len(urls)}] {u}", verbose=args.verbose)
            try:
                process_video(u, out_dir=out_dir, model=model,
                              dry_run=args.dry_run,
                              verbose=args.verbose)
                ok += 1
            except Exception as e:
                print(f"[yt-sum] FAIL {u}: {e}", file=sys.stderr, flush=True)
                failed.append(u)
        print(f"[yt-sum] done: {ok} ok, {len(failed)} failed", file=sys.stderr)
        return 0 if not failed else 2

    try:
        path = process_video(target, out_dir=out_dir, model=model,
                             dry_run=args.dry_run,
                             verbose=args.verbose)
    except Exception as e:
        die(str(e))
    if path:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
