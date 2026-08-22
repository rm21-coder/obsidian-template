#!/usr/bin/env python3
"""
podcast_watch.py — drop-folder front end for podcast_transcribe.py.

Watches an inbox folder and transcribes whatever lands in it, writing the
resulting markdown into the vault's Clippings/ folder so the existing
tag-clippings job picks it up and tags it like any other clip.

Two kinds of drop are understood:

  * an audio file            episode.mp3, interview.m4a, recording.wav, ...
  * a link file (.txt/.url)  containing an episode URL, an RSS feed URL, or
                             an Apple Podcasts URL — one per line, blank
                             lines and #-comments ignored. A .url shortcut
                             written by a browser works too; its URL= line is
                             picked out.

Source resolution is not reimplemented here — each item is handed to
podcast_transcribe.py, which already understands all four input kinds. This
module only decides *what* to hand over, *when*, and where the output goes.

Usage:
    python3 podcast_watch.py --once      # process the queue and exit (scheduled)
    python3 podcast_watch.py             # watch mode, for running by hand

Why this is throttled unlike the other watchers
-----------------------------------------------
Every other watch job in this repo is cheap — a file read, an API call. This
one is not: transcription costs real CPU for real minutes (roughly a quarter
of the audio's own duration on a modern laptop CPU). So:

  * --once processes at most --max-per-run items (default 1). A queue drains
    one episode per scheduled tick rather than seizing the machine for an
    hour on the first tick.
  * A single-instance lock means a tick that fires while a long episode is
    still transcribing exits immediately instead of starting a second one.

Processed drops are moved, never deleted: `done/` on success, `failed/` on
failure with a sibling .error.log. A dropped audio file may be the user's
only copy of a recording.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

import script_lock

SCRIPTS_DIR = Path(__file__).resolve().parent
VAULT_ROOT = SCRIPTS_DIR.parent.parent
LOCK_NAME = "podcast_watch"
TRANSCRIBE = SCRIPTS_DIR / "podcast_transcribe.py"

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac", ".mp4"}
LINK_EXTS = {".txt", ".url"}

def _configure_logging() -> None:
    """Route INFO to stdout and WARNING+ to stderr.

    The LaunchAgent sends stdout to podcast-watch.log and stderr to
    podcast-watch.err, and the installer points you at the .log. logging's
    default is a single stderr stream, so every line -- including "done in
    31s" -- landed in .err and the advertised .log sat at zero bytes. When a
    drop seemed to vanish, the first file you were told to check was the one
    guaranteed to be empty, which reads as "the job never ran".

    Splitting by level also makes .err mean something: a non-empty .err is now
    a problem worth reading, not the normal run log.
    """
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    root.addHandler(out)

    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.WARNING)
    root.addHandler(err)


_configure_logging()
log = logging.getLogger("podcast_watch")


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def default_inbox() -> Path:
    """Where podcast drops are expected.

    Resolved by source_media.drop_dir, so this watcher and the mail transport
    that fills the folder cannot disagree about its location.
    """
    import source_media
    return source_media.drop_dir("podcast")


def default_out(vault: Path) -> Path:
    """Clippings/, so the existing tag-clippings job tags transcripts too."""
    return vault / "Clippings"


# ---------------------------------------------------------------------------
# Concurrency — see script_lock.py
# ---------------------------------------------------------------------------

def acquire_lock() -> object | None:
    """Take the single-instance lock, or return None if another run holds it.

    Matters more here than in the cheaper watchers: a long episode transcribe
    can outlast the 900s StartInterval, and the shared helper's "a+" open is
    what keeps an overlapping tick reporting "already running" instead of
    crashing on Windows -- see script_lock's module docstring.
    """
    return script_lock.acquire(LOCK_NAME, warn=log.warning)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

def extract_url(path: Path) -> str | None:
    """First usable URL in a link file.

    Handles both a bare URL on a line and the INI-style .url shortcut a
    browser writes (a [InternetShortcut] section with a URL= key).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if line.lower().startswith("url="):
            line = line[4:].strip()
        if line.lower().startswith(("http://", "https://")):
            return line
    return None


def is_settled(path: Path, *, min_age: float = 20.0) -> bool:
    """True once a file has stopped changing.

    A large episode copied or synced into the folder appears immediately but
    keeps growing. Transcribing a half-written file wastes the most expensive
    step in the pipeline, so wait for it to go quiet first.
    """
    try:
        return (time.time() - path.stat().st_mtime) >= min_age
    except OSError:
        return False


def pending_items(inbox: Path, *, min_age: float = 20.0) -> list[Path]:
    """Drops ready to process, oldest first. Sub-folders are ignored, which is
    what keeps done/ and failed/ out of the queue."""
    if not inbox.is_dir():
        return []
    items = [
        p for p in inbox.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.suffix.lower() in (AUDIO_EXTS | LINK_EXTS)
        and is_settled(p, min_age=min_age)
    ]
    return sorted(items, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def _retire(item: Path, target_dir: Path) -> Path:
    """Move a processed drop aside, keeping both copies if names collide."""
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / item.name
    if dest.exists():
        stem, suffix = item.stem, item.suffix
        dest = target_dir / f"{stem}-{int(time.time())}{suffix}"
    return Path(shutil.move(str(item), str(dest)))


def process_item(item: Path, *, out_dir: Path, model: str | None,
                 python: str, timeout: int) -> bool:
    """Transcribe one drop. Returns True on success."""
    if item.suffix.lower() in LINK_EXTS:
        source = extract_url(item)
        if not source:
            log.error("%s contains no http(s) URL", item.name)
            failed = _retire(item, item.parent / "failed")
            failed.with_suffix(failed.suffix + ".error.log").write_text(
                "No http(s) URL found in this file.\n", encoding="utf-8")
            return False
        log.info("%s -> %s", item.name, source)
    else:
        source = str(item)
        log.info("%s -> local audio", item.name)

    cmd = [python, str(TRANSCRIBE), source, "--out", str(out_dir), "--verbose"]
    if model:
        cmd += ["--model", model]

    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.error("%s timed out after %ss", item.name, timeout)
        failed = _retire(item, item.parent / "failed")
        failed.with_suffix(failed.suffix + ".error.log").write_text(
            f"Transcription exceeded the {timeout}s limit.\n", encoding="utf-8")
        return False

    elapsed = time.time() - started
    if proc.returncode != 0:
        log.error("%s failed (exit %s) after %.0fs", item.name,
                  proc.returncode, elapsed)
        failed = _retire(item, item.parent / "failed")
        failed.with_suffix(failed.suffix + ".error.log").write_text(
            f"exit {proc.returncode}\n\n--- stderr ---\n{proc.stderr}\n",
            encoding="utf-8")
        return False

    written = (proc.stdout or "").strip().splitlines()
    log.info("%s done in %.0fs -> %s", item.name, elapsed,
             written[-1] if written else out_dir)
    _retire(item, item.parent / "done")
    return True


def run_once(inbox: Path, *, out_dir: Path, model: str | None, python: str,
             max_per_run: int, timeout: int, min_age: float) -> int:
    items = pending_items(inbox, min_age=min_age)
    if not items:
        log.info("Nothing pending in %s", inbox)
        return 0
    if max_per_run > 0 and len(items) > max_per_run:
        log.info("%d pending; taking %d this run (the rest wait for the next "
                 "tick — transcription is expensive)", len(items), max_per_run)
        items = items[:max_per_run]

    ok = 0
    for item in items:
        if process_item(item, out_dir=out_dir, model=model, python=python,
                        timeout=timeout):
            ok += 1
    log.info("Done — %d/%d succeeded.", ok, len(items))
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Transcribe podcasts dropped into an inbox folder.")
    p.add_argument("--once", action="store_true",
                   help="process pending drops and exit (for scheduled runs)")
    p.add_argument("--inbox", default=None,
                   help=f"drop folder (default: {default_inbox()})")
    p.add_argument("--out", default=None,
                   help="output folder (default: <vault>/Clippings)")
    p.add_argument("--vault", default=str(VAULT_ROOT),
                   help=f"vault root (default: {VAULT_ROOT})")
    p.add_argument("--model", default=None,
                   help="passed through to podcast_transcribe.py")
    p.add_argument("--max-per-run", type=int, default=1,
                   help="max drops per --once run; 0 for no limit (default: 1)")
    p.add_argument("--timeout", type=int, default=4 * 60 * 60,
                   help="per-episode timeout in seconds (default: 14400)")
    p.add_argument("--min-age", type=float, default=20.0,
                   help="seconds a file must be unmodified before it is "
                        "considered fully copied (default: 20)")
    p.add_argument("--interval", type=int, default=60,
                   help="seconds between checks in watch mode (default: 60)")
    args = p.parse_args(argv)

    vault = Path(args.vault).resolve()
    inbox = Path(args.inbox).expanduser() if args.inbox else default_inbox()
    out_dir = Path(args.out).expanduser() if args.out else default_out(vault)

    if not TRANSCRIBE.exists():
        log.error("podcast_transcribe.py not found at %s", TRANSCRIBE)
        return 1

    inbox.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    lock = acquire_lock()
    if lock is None:
        log.info("Another podcast_watch run is still going; exiting.")
        return 0

    try:
        if args.once:
            run_once(inbox, out_dir=out_dir, model=args.model,
                     python=sys.executable, max_per_run=args.max_per_run,
                     timeout=args.timeout, min_age=args.min_age)
            return 0

        log.info("Watching %s -> %s (Ctrl-C to stop)", inbox, out_dir)
        try:
            while True:
                run_once(inbox, out_dir=out_dir, model=args.model,
                         python=sys.executable, max_per_run=args.max_per_run,
                         timeout=args.timeout, min_age=args.min_age)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("Stopped.")
            return 0
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
