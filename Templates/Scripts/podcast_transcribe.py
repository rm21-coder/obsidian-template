#!/usr/bin/env python3
"""
podcast_transcribe.py — Local podcast transcription (MLX / faster-whisper / ONNX).

First-step tool: takes a podcast MP3 URL, RSS feed URL, Apple Podcasts URL,
or local audio file, and produces a plain markdown transcript file you can
read in any editor. No vault writes, no LLM summary — that's a follow-up
step once we like the transcription quality.

Usage:
    podcast_transcribe.py "https://example.com/episode.mp3"
    podcast_transcribe.py "https://example.com/feed.xml"           # latest episode
    podcast_transcribe.py "https://example.com/feed.xml" --episode 2  # 2nd-newest
    podcast_transcribe.py "https://podcasts.apple.com/us/podcast/.../id<show>?i=<ep>"
    podcast_transcribe.py /path/to/local/file.mp3

Backends, in the order they are tried:
    1. MLX Whisper       — Apple Silicon only, GPU-accelerated.
    2. faster-whisper    — CPU, anywhere ctranslate2 has a wheel.
    3. ONNX Runtime      — CPU, torch-free (whisper_onnx.py). The only local
                           option on Windows ARM64, where neither ctranslate2
                           nor torch publishes a win_arm64 wheel.

The default model differs per backend, since a sensible choice for a GPU
backend is a poor one on CPU. Pass --model to override on any of them.

Flags:
    --model NAME      MLX Whisper repo (default: mlx-community/whisper-large-v3-turbo)
    --out DIR         Output directory (default: /tmp/podcast_test)
    --episode N       For RSS feeds: 1=newest, 2=second-newest, etc. (default: 1)
    --keep-audio      Keep the downloaded MP3 in the cache (default: keep)
    --verbose         Log progress to stderr

Output: a single .md file at OUT/<title>.md containing minimal frontmatter
and the verbatim transcript. Path is printed to stdout on success.

Requires:
    - Python 3.10+
    - ffmpeg on PATH:  brew install ffmpeg  /  winget install Gyan.FFmpeg
    - one transcription backend:
        Apple Silicon : pip3 install --break-system-packages mlx-whisper
        elsewhere     : faster-whisper (ships in requirements.txt), or
                        onnxruntime + tokenizers + huggingface-hub for the
                        ONNX backend, which is what Windows ARM64 uses

Exit codes:
    0  success
    1  fatal error
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import url_safety

DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
# The ONNX backend runs on CPU, so the MLX default is the wrong trade there.
# Measured on a Snapdragon X Elite (12 cores), transcribing 8s of speech:
#   base   11.7x realtime, ~290 MB   small  4.2x realtime, ~1 GB
# small is the sweet spot -- roughly 14 minutes for a one-hour episode, and
# it was word-exact on the check sample where base dropped a word.
DEFAULT_ONNX_MODEL = "small"
DEFAULT_OUT = Path(tempfile.gettempdir()) / "podcast_test"
AUDIO_CACHE = Path.home() / ".cache" / "podcast_transcribe"

# ---------- Logging ----------------------------------------------------------

def log(msg: str, *, verbose: bool) -> None:
    if verbose:
        print(f"[podcast] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"[podcast] error: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# ---------- Input classification --------------------------------------------

APPLE_PODCASTS_HOST = "podcasts.apple.com"


def classify_input(arg: str) -> str:
    """Return one of: 'local', 'mp3', 'rss', 'apple'."""
    if not arg.startswith(("http://", "https://")):
        return "local"
    parsed = urlparse(arg)
    if parsed.netloc.endswith(APPLE_PODCASTS_HOST):
        return "apple"
    # Heuristic: if it ends in audio extension, treat as direct media.
    path = parsed.path.lower()
    if any(path.endswith(ext) for ext in (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac")):
        return "mp3"
    # Otherwise assume RSS/Atom feed. We'll validate by trying to parse.
    return "rss"


# ---------- Apple Podcasts resolver -----------------------------------------

_APPLE_SHOW_ID_RE = re.compile(r"/id(\d+)")


def parse_apple_url(url: str) -> tuple[str, str | None]:
    """Extract (show_id, episode_id_or_None) from an Apple Podcasts URL."""
    parsed = urlparse(url)
    m = _APPLE_SHOW_ID_RE.search(parsed.path)
    if not m:
        raise RuntimeError(f"could not parse show ID from URL: {url}")
    show_id = m.group(1)
    episode_id: str | None = None
    for kv in (parsed.query or "").split("&"):
        if kv.startswith("i="):
            episode_id = kv.split("=", 1)[1]
            break
    return show_id, episode_id


def itunes_lookup(item_id: str, *, timeout: int = 30) -> dict:
    """Hit iTunes lookup API and return the first result.

    Show lookups only. Episodes go through itunes_episode_title below —
    Apple does not resolve a podcast episode track ID on its own.
    """
    url = f"https://itunes.apple.com/lookup?id={item_id}"
    raw = fetch_url(url, timeout=timeout)
    data = json.loads(raw.decode("utf-8"))
    results = data.get("results") or []
    if not results:
        raise RuntimeError(f"iTunes lookup returned no results for id={item_id}")
    return results[0]


def itunes_episode_title(show_id: str, episode_id: str, *,
                         timeout: int = 30) -> str:
    """trackName for an Apple `?i=<episode_id>` value, or "" if not found.

    Scoped to the *show*, not looked up by the episode ID on its own. A bare
    `lookup?id=<episode_id>` returns resultCount=0 for every episode tried,
    with or without `entity`/`media`/`country` -- Apple's lookup API does not
    resolve podcast episode track IDs standalone. The old direct call
    therefore always failed, target_title was always empty, and every Apple
    Podcasts URL fell through to "newest in feed" regardless of which episode
    was actually requested. A show-scoped lookup does return the episode list
    with trackId/trackName pairs, which is what the title match below needs.
    """
    qs = f"id={show_id}&entity=podcastEpisode&limit=200"
    raw = fetch_url(f"https://itunes.apple.com/lookup?{qs}", timeout=timeout)
    data = json.loads(raw.decode("utf-8"))
    for r in data.get("results") or []:
        if (r.get("wrapperType") == "podcastEpisode"
                and str(r.get("trackId")) == str(episode_id)):
            return (r.get("trackName") or "").strip()
    return ""


def resolve_apple_podcasts_url(url: str, *, verbose: bool) -> tuple[str, str, str]:
    """For an Apple Podcasts URL, return (mp3_url, episode_title, show_title).

    Strategy:
      1. Parse show ID and (optional) episode ID from the URL.
      2. iTunes lookup the show to get the RSS feedUrl.
      3. Fetch and parse the RSS feed.
      4. Match the requested episode by:
         - iTunes-supplied trackName, resolved via a show-scoped episode
           lookup (when an episode ID is present), then
         - episode ID embedded in the GUID, then
         - newest episode -- only for a show-level URL. An unmatched
           episode-specific URL raises instead.
    """
    show_id, episode_id = parse_apple_url(url)
    log(f"apple: show={show_id} episode={episode_id}", verbose=verbose)

    show = itunes_lookup(show_id)
    feed_url = show.get("feedUrl")
    show_title = show.get("collectionName") or ""
    if not feed_url:
        raise RuntimeError("iTunes lookup did not return a feedUrl for that show")
    log(f"apple: feedUrl={feed_url}", verbose=verbose)

    target_title = ""
    if episode_id:
        try:
            target_title = itunes_episode_title(show_id, episode_id)
        except Exception as e:
            log(f"apple: episode lookup failed: {e}", verbose=verbose)
        log(f"apple: episode title={target_title!r}", verbose=verbose)

    feed_bytes = fetch_url(feed_url)
    channel_title, items = parse_rss(feed_bytes)
    if not items:
        raise RuntimeError("show feed has no episodes with audio enclosures")

    # 1. Match by exact title (Apple's trackName == feed item <title>).
    selected = None
    if target_title:
        for it in items:
            if it["title"].strip() == target_title:
                selected = it
                break
        if selected is None:
            # tolerant match — sometimes punctuation differs slightly
            norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()
            for it in items:
                if norm(it["title"]) == norm(target_title):
                    selected = it
                    break

    # 2. Match by episode ID inside guid
    if selected is None and episode_id:
        for it in items:
            if episode_id in (it.get("guid") or ""):
                selected = it
                break

    # 3. Newest — but only when the URL did not name an episode.
    #
    # When it did (?i=...) and neither match above found it, falling back to
    # the newest episode is a silently wrong answer: an hour of unrelated
    # audio, transcribed and filed under the title the user asked for, with
    # nothing in the vault to show it is not what they shared. Failing is the
    # cheaper mistake -- the drop lands in failed/ with this reason attached.
    if selected is None:
        if episode_id:
            raise RuntimeError(
                f"could not identify episode {episode_id} in {feed_url} "
                f"(iTunes title={target_title!r}); refusing to substitute the "
                f"newest episode")
        log("apple: show-level URL — using newest in feed", verbose=verbose)
        selected = items[0]

    title = f"{(channel_title or show_title or 'Podcast')} — {selected['title']}"
    return selected["enclosure_url"], title, (channel_title or show_title)


# ---------- RSS handling ----------------------------------------------------

def fetch_url(url: str, *, timeout: int = 60) -> bytes:
    """Fetch a feed/metadata document through the shared SSRF guard.

    Feed URLs are not always operator-chosen — they arrive from drop folders
    and the mail transport — so this must not use the stdlib urlopen helper,
    which follows redirects internally and would let a validated first hop be
    walked to a loopback or link-local address.
    """
    body = url_safety.safe_fetch(url, log=lambda m: log(m, verbose=True))
    if body is None:
        raise RuntimeError(f"refused or failed to fetch {url}")
    return body


def parse_rss(xml_bytes: bytes) -> tuple[str, list[dict]]:
    """Return (channel_title, [{title, enclosure_url, pub_date, ...}, ...])
    sorted newest first. Supports RSS 2.0 with <enclosure> tags."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise RuntimeError(f"could not parse RSS XML: {e}") from e

    # Strip default namespace if present
    def localname(tag: str) -> str:
        return tag.split("}", 1)[1] if "}" in tag else tag

    channel = None
    for child in root:
        if localname(child.tag) == "channel":
            channel = child
            break
    if channel is None:
        # Atom feed — handle just enough
        return parse_atom(root, localname)

    channel_title = ""
    items = []
    for child in channel:
        tag = localname(child.tag)
        if tag == "title":
            channel_title = (child.text or "").strip()
        elif tag == "item":
            item = {"title": "", "enclosure_url": "", "pub_date": "", "guid": ""}
            for sub in child:
                stag = localname(sub.tag)
                if stag == "title":
                    item["title"] = (sub.text or "").strip()
                elif stag == "enclosure":
                    url = sub.attrib.get("url") or ""
                    typ = sub.attrib.get("type", "")
                    if url and (typ.startswith("audio/") or url.lower().endswith((".mp3", ".m4a", ".aac"))):
                        item["enclosure_url"] = url
                elif stag == "pubDate":
                    item["pub_date"] = (sub.text or "").strip()
                elif stag == "guid":
                    item["guid"] = (sub.text or "").strip()
            if item["enclosure_url"]:
                items.append(item)
    return channel_title, items


def parse_atom(root, localname) -> tuple[str, list[dict]]:
    title = ""
    items = []
    for child in root:
        tag = localname(child.tag)
        if tag == "title" and not title:
            title = (child.text or "").strip()
        elif tag == "entry":
            item = {"title": "", "enclosure_url": "", "pub_date": "", "guid": ""}
            for sub in child:
                stag = localname(sub.tag)
                if stag == "title":
                    item["title"] = (sub.text or "").strip()
                elif stag == "link":
                    if (sub.attrib.get("rel") == "enclosure"
                            and sub.attrib.get("type", "").startswith("audio/")):
                        item["enclosure_url"] = sub.attrib.get("href", "")
                elif stag == "published" or stag == "updated":
                    if not item["pub_date"]:
                        item["pub_date"] = (sub.text or "").strip()
                elif stag == "id":
                    item["guid"] = (sub.text or "").strip()
            if item["enclosure_url"]:
                items.append(item)
    return title, items


# ---------- Audio download --------------------------------------------------

def safe_filename(s: str, max_len: int = 120) -> str:
    s = re.sub(r"[^A-Za-z0-9 _\-.,&!]", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:max_len].rstrip() if len(s) > max_len else s) or "Untitled"


def cache_path_for(url: str, title_hint: str = "") -> Path:
    AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
    # Derive stable filename from URL path; prepend safe title hint for human legibility.
    path = urlparse(url).path
    ext = ""
    for cand in (".mp3", ".m4a", ".aac", ".wav", ".opus", ".ogg", ".flac"):
        if path.lower().endswith(cand):
            ext = cand
            break
    if not ext:
        ext = ".mp3"  # best guess
    base = safe_filename(title_hint, max_len=80) if title_hint else "audio"
    # Append a short hash of the URL so distinct episodes with same title don't collide.
    import hashlib
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    return AUDIO_CACHE / f"{base} [{h}]{ext}"


def download_audio(url: str, dest: Path, *, verbose: bool) -> None:
    """Download an episode through the shared SSRF guard.

    Streamed to disk with its own (much larger) cap rather than safe_fetch's
    in-memory one — episodes routinely run 50-150 MB. Every redirect hop is
    re-validated, so an enclosure URL from an untrusted feed can't be walked
    to an internal address.
    """
    if dest.exists() and dest.stat().st_size > 0:
        log(f"audio cached: {dest} ({dest.stat().st_size:,} bytes)", verbose=verbose)
        return
    log(f"downloading: {url}", verbose=verbose)
    ok = url_safety.safe_download(url, dest,
                                  log=lambda m: log(m, verbose=verbose))
    if not ok:
        raise RuntimeError(f"refused or failed to download {url}")


# ---------- Transcription ---------------------------------------------------

def _mlx_available() -> bool:
    """MLX Whisper is Apple-Silicon-only; use it when present, else faster-whisper."""
    if sys.platform != "darwin":
        return False
    try:
        import mlx_whisper  # type: ignore  # noqa: F401
        return True
    except ImportError:
        return False


def _transcribe_faster_whisper(audio_path: Path, *, model: str, verbose: bool) -> dict:
    """Transcribe with faster-whisper (CPU). Returns an mlx-compatible dict."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("faster-whisper not installed. It ships in requirements.txt; run:\n"
            f"    {sys.executable} -m pip install faster-whisper")

    # MLX repo ids (e.g. 'mlx-community/whisper-large-v3-turbo') aren't valid
    # faster-whisper sizes; fall back to a CPU-friendly default. Override with
    # --model small / medium / large-v3 as your hardware allows.
    size = model
    if "/" in model or model.startswith("mlx"):
        size = "base"
        log(f"'{model}' isn't a faster-whisper model; using '{size}' (override with --model)",
            verbose=True)

    log(f"loading faster-whisper model: {size} (CPU, int8)", verbose=verbose)
    wm = WhisperModel(size, device="cpu", compute_type="int8")
    segments, info = wm.transcribe(str(audio_path), beam_size=5)
    segs: list[dict] = []
    parts: list[str] = []
    for s in segments:
        text = (s.text or "").strip()
        segs.append({"start": float(s.start or 0.0),
                     "end": float(s.end or 0.0), "text": text})
        parts.append(text)
    return {
        "segments": segs,
        "text": " ".join(parts).strip(),
        "language": getattr(info, "language", None),
        "_audio_duration": float(getattr(info, "duration", 0.0) or 0.0),
    }


def _faster_whisper_available() -> bool:
    """faster-whisper needs ctranslate2, which has no Windows-ARM64 wheel."""
    try:
        import faster_whisper  # type: ignore  # noqa: F401
        return True
    except ImportError:
        return False


def _transcribe_onnx(audio_path: Path, *, model: str, verbose: bool) -> dict:
    """Transcribe via ONNX Runtime (whisper_onnx.py). Torch-free, and the only
    local backend available on Windows ARM64."""
    try:
        import whisper_onnx
    except ImportError:
        die("whisper_onnx.py not importable — it should sit beside this script "
            "in Templates/Scripts/.")

    try:
        audio = whisper_onnx.decode_audio(audio_path)
        log(f"resolving ONNX model for '{model}'", verbose=verbose)
        model_dir = whisper_onnx.ensure_model(model)
        log(f"loading ONNX model from {model_dir}", verbose=verbose)
        engine = whisper_onnx.WhisperOnnx(model_dir)

        def progress(done: int, total: int) -> None:
            log(f"  window {done}/{total}", verbose=verbose)

        return engine.transcribe(audio, progress=progress if verbose else None)
    except whisper_onnx.WhisperOnnxError as exc:
        die(str(exc))
    return {}  # unreachable


def transcribe(audio_path: Path, *, model: str | None, verbose: bool) -> dict:
    """Transcribe locally, picking the best backend the platform can run:
    MLX on Apple Silicon, else faster-whisper, else ONNX Runtime.

    ONNX is last because faster-whisper is better optimised where it can be
    installed at all — but it cannot on Windows ARM64, where ctranslate2 has
    no wheel, so ONNX is what makes local transcription work there.

    `model` of None means "whatever suits the chosen backend", since a good
    default for a GPU backend is a bad one for a CPU backend. The resolved
    name comes back as `_model` so the transcript records what actually ran.
    """
    log(f"transcribing: {audio_path}", verbose=verbose)
    started = time.time()
    if _mlx_available():
        import mlx_whisper  # type: ignore
        resolved = model or DEFAULT_MODEL
        log(f"loading MLX model: {resolved}", verbose=verbose)
        result = mlx_whisper.transcribe(  # type: ignore[name-defined]
            str(audio_path), path_or_hf_repo=resolved, verbose=verbose)
    elif _faster_whisper_available():
        resolved = model or "base"
        result = _transcribe_faster_whisper(audio_path, model=resolved, verbose=verbose)
    else:
        resolved = model or DEFAULT_ONNX_MODEL
        result = _transcribe_onnx(audio_path, model=resolved, verbose=verbose)
    result["_model"] = resolved
    result["_elapsed_seconds"] = time.time() - started
    log(f"transcription done in {result['_elapsed_seconds']:.1f}s", verbose=verbose)
    return result


# ---------- ffprobe duration ------------------------------------------------

def audio_duration(audio_path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip())
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError):
        return 0.0


# ---------- Output ----------------------------------------------------------

def yaml_escape(value: str) -> str:
    if not value:
        return '""'
    needs_quote = any(c in value for c in ":#&*!|>'\"%@`")
    if needs_quote or value[0] in "[{?-":
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def format_duration(seconds: float) -> str:
    s = int(seconds)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# Timestamp markers: aim for one every MARKER_INTERVAL seconds, but slip to
# the next sentence end so paragraphs start on a thought. MARKER_MAX bounds
# the slip for speech that never punctuates.
MARKER_INTERVAL = 30.0
MARKER_MAX = 45.0
SENTENCE_ENDINGS = (".", "?", "!", '."', '?"', '!"', ".'", "?'", "!'", "\u2026")


def write_transcript_md(*, out_dir: Path, title: str, source_url: str,
                        result: dict, audio_seconds: float, model: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = safe_filename(title) + ".md"
    path = out_dir / fname

    # ffprobe may be absent on Windows; faster-whisper reports duration itself.
    if not audio_seconds:
        audio_seconds = float(result.get("_audio_duration") or 0.0)

    transcribe_seconds = float(result.get("_elapsed_seconds") or 0.0)
    speedup = (audio_seconds / transcribe_seconds) if transcribe_seconds and audio_seconds else 0.0

    lines = [
        "---",
        f"title: {yaml_escape(title)}",
        f"source: {yaml_escape(source_url)}",
        f"created: {date.today().isoformat()}",
        f"duration: {yaml_escape(format_duration(audio_seconds))}" if audio_seconds else 'duration: ""',
        f"model: {yaml_escape(model)}",
        f"transcription_time: {yaml_escape(format_duration(transcribe_seconds))}" if transcribe_seconds else 'transcription_time: ""',
        f"realtime_speedup: {speedup:.1f}x" if speedup else 'realtime_speedup: ""',
        f"language: {yaml_escape(result.get('language') or 'unknown')}",
        # Matches the other clippers: externally published material defaults to
        # public. Raise it by hand for a private recording.
        "classification: public",
        "tags:",
        "- podcast",
        "- transcript",
        "---",
        "",
        "## Transcript",
        "",
    ]

    # mlx-whisper returns segments with timestamps; emit them as a readable
    # transcript with timestamp markers every ~30s for easy navigation.
    #
    # Each marker's segments are joined into a single paragraph rather than
    # written one line apiece. Whisper ends a segment where its decoder does,
    # not where a sentence does, so a line-per-segment transcript reads as
    # prose chopped mid-clause -- a lone "it?" or "on Apple Podcasts." on its
    # own line, several times a minute. Joining also lets the reader's own
    # width apply: a Markdown paragraph reflows on a phone, where lines hard-
    # wrapped at whatever column Whisper happened to emit do not.
    segments = result.get("segments") or []
    if segments:
        last_marker = -MARKER_INTERVAL
        block: list[str] = []
        for seg in segments:
            start = float(seg.get("start") or 0.0)
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            elapsed = start - last_marker
            # Break on the first sentence end at or after the interval, rather
            # than on the interval itself. The clock hits 30s wherever it hits
            # it, which is usually mid-sentence, and a paragraph opening with
            # "wave of concern that we're still in right now" reads as badly as
            # the chopped lines this layout replaced. Waiting costs a few
            # seconds of marker precision and buys a paragraph that starts
            # where a thought does. MARKER_MAX caps the wait, since a stretch
            # of speech can carry no terminal punctuation at all.
            if elapsed >= MARKER_INTERVAL and (
                    not block
                    or block[-1].endswith(SENTENCE_ENDINGS)
                    or elapsed >= MARKER_MAX):
                if block:
                    lines.extend([" ".join(block), ""])
                    block = []
                lines.append(f"**[{format_duration(start)}]**")
                last_marker = start
            block.append(text)
        if block:
            lines.append(" ".join(block))
    else:
        # Fall back to raw "text"
        lines.append((result.get("text") or "").strip())

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------- Main ------------------------------------------------------------

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Transcribe a podcast locally (MLX / faster-whisper / ONNX).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("source",
                   help="MP3 URL, RSS feed URL, Apple Podcasts URL, or local audio path")
    p.add_argument("--model", default=None,
                   help=f"model size (base/small/medium/large-v3-turbo) or an "
                        f"explicit repo id. Left unset, each backend picks its "
                        f"own default: MLX uses {DEFAULT_MODEL}, the CPU/ONNX "
                        f"backend uses '{DEFAULT_ONNX_MODEL}' (a GPU-sized "
                        f"default is a poor fit on CPU)")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help=f"output directory (default: {DEFAULT_OUT})")
    p.add_argument("--episode", type=int, default=1,
                   help="for RSS feeds: 1=newest, 2=second-newest, etc.")
    p.add_argument("--no-keep-audio", action="store_true",
                   help="delete the downloaded audio after transcription")
    p.add_argument("--verbose", action="store_true", help="log progress to stderr")
    args = p.parse_args(argv)

    # Transcription backend is auto-selected in transcribe(): MLX Whisper on
    # Apple Silicon, faster-whisper (CPU) everywhere else. No hard arch gate.

    out_dir = Path(os.path.expanduser(args.out)).resolve()
    kind = classify_input(args.source)

    audio_path: Path
    title: str
    source_url: str

    if kind == "local":
        audio_path = Path(os.path.expanduser(args.source)).resolve()
        if not audio_path.exists():
            die(f"local file not found: {audio_path}")
        title = audio_path.stem
        source_url = str(audio_path)

    elif kind == "mp3":
        # Direct media URL
        title_hint = Path(urlparse(args.source).path).stem or "Episode"
        audio_path = cache_path_for(args.source, title_hint=title_hint)
        try:
            download_audio(args.source, audio_path, verbose=args.verbose)
        except urllib.error.HTTPError as e:
            die(f"download failed: HTTP {e.code} {e.reason}")
        except Exception as e:
            die(f"download failed: {e}")
        title = title_hint
        source_url = args.source

    elif kind == "apple":
        try:
            ep_url, title_hint, _show_title = resolve_apple_podcasts_url(
                args.source, verbose=args.verbose)
        except Exception as e:
            die(f"could not resolve Apple Podcasts URL: {e}")
        log(f"apple: episode mp3 = {ep_url}", verbose=args.verbose)
        audio_path = cache_path_for(ep_url, title_hint=title_hint)
        try:
            download_audio(ep_url, audio_path, verbose=args.verbose)
        except urllib.error.HTTPError as e:
            die(f"download failed: HTTP {e.code} {e.reason}")
        except Exception as e:
            die(f"download failed: {e}")
        title = title_hint
        source_url = args.source  # keep the canonical Apple URL as source

    else:  # rss
        log(f"fetching RSS: {args.source}", verbose=args.verbose)
        try:
            xml_bytes = fetch_url(args.source)
        except Exception as e:
            die(f"could not fetch feed: {e}")
        try:
            channel_title, items = parse_rss(xml_bytes)
        except RuntimeError as e:
            die(str(e))
        if not items:
            die("no episodes with audio enclosures found in feed")
        if args.episode < 1 or args.episode > len(items):
            die(f"--episode {args.episode} out of range (feed has {len(items)} episodes)")
        episode = items[args.episode - 1]
        log(f"selected: [{args.episode}] {episode['title']}", verbose=args.verbose)
        ep_url = episode["enclosure_url"]
        title_hint = f"{channel_title} — {episode['title']}" if channel_title else episode["title"]
        audio_path = cache_path_for(ep_url, title_hint=title_hint)
        try:
            download_audio(ep_url, audio_path, verbose=args.verbose)
        except urllib.error.HTTPError as e:
            die(f"download failed: HTTP {e.code} {e.reason}")
        except Exception as e:
            die(f"download failed: {e}")
        title = title_hint
        source_url = ep_url

    audio_seconds = audio_duration(audio_path)
    if audio_seconds:
        log(f"audio duration: {format_duration(audio_seconds)} ({audio_seconds:.0f}s)",
            verbose=args.verbose)

    try:
        result = transcribe(audio_path, model=args.model, verbose=args.verbose)
    except Exception as e:
        die(f"transcription failed: {e}")

    out_path = write_transcript_md(
        out_dir=out_dir, title=title, source_url=source_url,
        result=result, audio_seconds=audio_seconds,
        model=result.get("_model", args.model),
    )
    log(f"wrote: {out_path}", verbose=args.verbose)
    if args.no_keep_audio and kind != "local":
        try:
            audio_path.unlink()
        except OSError:
            pass

    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
