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

Playlist (or any yt-dlp-recognized collection):
    youtube_summarize.py --playlist "https://www.youtube.com/playlist?list=YYYY"

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
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

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
if (
    sys.prefix == sys.base_prefix
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
# Mirrors url_safety.is_safe_url. The caption-track URLs we hit during
# transcript fetch come out of yt-dlp's parsed YouTube response — a remote
# input we don't fully control. A malicious or compromised upstream could
# in principle return a track URL pointing at an internal address
# (loopback, link-local 169.254.169.254 metadata, RFC 1918 LAN). This is the
# only remote-supplied URL the script fetches; the model endpoint comes from
# llm_endpoint, not from anything YouTube returns.

DISALLOWED_TLDS = (".local", ".internal", ".lan", ".intranet", ".corp",
                   ".home", ".localdomain")
LOOPBACK_NAMES = ("localhost", "ip6-localhost", "broadcasthost",
                  "ip6-loopback")


def _ip_is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def is_safe_url(url: str) -> tuple[bool, str]:
    """Return (ok, reason). Conservative: rejects any URL that could
    plausibly target an internal resource. Kept in sync with the function
    of the same name in url_safety.py.

    v1.7 (2026-05-12): added DNS resolution via socket.getaddrinfo to
    defeat DNS-rebinding attacks (attacker-controlled DNS that resolves
    a public-looking hostname to an internal IP). DNS failures are also
    treated as unsafe — conservative posture. This is the same hardening
    landed in url_safety.py the same day; the cross-module parity tests
    pin both predicates to the strong version.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "unparseable URL"
    if parsed.scheme not in ("http", "https"):
        return False, f"disallowed scheme: {parsed.scheme!r}"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "empty hostname"
    if host in LOOPBACK_NAMES:
        return False, f"loopback hostname: {host}"
    for tld in DISALLOWED_TLDS:
        if host.endswith(tld):
            return False, f"disallowed local TLD: {host}"
    # IP literal path: accept public IPs, reject internal IPs.
    try:
        ip = ipaddress.ip_address(host)
        if _ip_is_internal(ip):
            return False, f"disallowed IP literal: {ip}"
        return True, ""
    except ValueError:
        pass  # Not an IP literal — fall through to DNS resolution.
    # Hostname path: resolve and reject if ANY answer is internal.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return False, f"DNS resolution failed for {host!r}: {e}"
    if not infos:
        return False, f"DNS resolution returned no records for {host!r}"
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            resolved = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, (f"DNS returned unparseable address {ip_str!r} "
                           f"for {host!r}")
        if _ip_is_internal(resolved):
            return False, (f"{host} resolves to internal address "
                           f"{ip_str}")
    return True, ""


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
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def enumerate_playlist(url: str) -> list[str]:
    """Return ordered video URLs for a playlist."""
    data = run_ytdlp(["--flat-playlist", url])
    if isinstance(data, dict) and "entries" in data:
        return [e.get("url") or f"https://www.youtube.com/watch?v={e['id']}"
                for e in data["entries"] if e]
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
        # Prefer "en", then any en-* (en-US, en-GB, en-orig, etc.)
        keys = sorted(tracks.keys(), key=lambda k: (k != "en", not k.startswith("en"), k))
        for k in keys:
            if not (k == "en" or k.startswith("en")):
                continue
            for fmt in tracks[k]:
                ext = fmt.get("ext", "")
                if ext in ("json3", "vtt", "srt", "ttml"):
                    candidates.append((source, k, ext, fmt.get("url")))

    for source, lang, ext, url in candidates:
        # Reject caption-track URLs that point at internal/loopback/private
        # addresses before urlopen. The URLs come from yt-dlp's parsed
        # YouTube response — a remote input — so this is the SSRF perimeter.
        ok, reason = is_safe_url(url or "")
        if not ok:
            log(f"skipping caption track: unsafe url ({reason})", verbose=True)
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError):
            continue
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
        t = t.lstrip("-* ").strip("\"'#").strip()
        if t:
            cleaned.append(t.replace(" ", "-"))
    return cleaned


def strip_suggested_tags_section(summary_md: str) -> str:
    return re.sub(r"\n*##\s+Suggested tags[\s\S]*$", "", summary_md).rstrip() + "\n"


def yaml_escape(value: str) -> str:
    """Quote a value if it contains YAML-significant characters."""
    if value == "" or value is None:
        return '""'
    needs_quote = any(c in value for c in ":#&*!|>'\"%@`")
    if needs_quote or value[0] in "[{?-":
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def build_frontmatter(info: dict, suggested_tags: list[str], description: str) -> str:
    title = info.get("title") or "Untitled"
    url = info.get("webpage_url") or info.get("original_url") or ""
    author = info.get("uploader") or info.get("channel") or ""
    upload_date = info.get("upload_date") or ""
    if upload_date and re.fullmatch(r"\d{8}", upload_date):
        published = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
    else:
        published = upload_date or "null"
    duration = info.get("duration")
    duration_str = format_duration(duration) if duration else ""

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
        f"published: {published if published == 'null' else yaml_escape(published)}",
        f"created: {date.today().isoformat()}",
        f"duration: {yaml_escape(duration_str)}",
        f"description: {yaml_escape(description)}",
        # Data classification (see Knowledge/Data Classification.md if you
        # keep one). Public YouTube content defaults to `public`.
        "classification: internal-use-only",
        "tags:",
    ]
    for t in all_tags:
        lines.append(f"- {t}")
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

    title = info.get("title") or info.get("id") or "Untitled"
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
    note_path.write_text(frontmatter + "\n" + body, encoding="utf-8")
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
        die("yt-dlp not installed. It ships in requirements.txt; run: "
            f"{sys.executable} -m pip install yt-dlp")

    out_dir = Path(os.path.expanduser(args.out)).resolve()

    model = resolve_model(args.model)
    # No credential check here: llm_endpoint raises MissingCredential naming the
    # exact secret and how to store it, which beats a second guess at it.

    if args.playlist:
        try:
            urls = enumerate_playlist(args.url)
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
        path = process_video(args.url, out_dir=out_dir, model=model,
                             dry_run=args.dry_run,
                             verbose=args.verbose)
    except Exception as e:
        die(str(e))
    if path:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
