#!/usr/bin/env python3
"""
script_lock.py — one single-instance file lock, shared by every scheduled job.

Four scripts each grew their own copy of the same ~25-line acquire_lock:
podcast_watch, source_mail_pull, handoff_blob_pull and meeting_prepopulate.
(There was a fifth, clip_article, retired 2026-08-18.) The copies had drifted
in ways that mattered:

  - Two opened the lock file "a+", two opened it 'w'. A truncating open of a
    file whose bytes another process has locked raises PermissionError on
    Windows *before* any locking call runs, so those two would crash with a
    traceback and a non-zero exit instead of reporting "already running" --
    on a scheduled job, a logged failure on every tick for the whole duration
    of a long run. Consolidating on "a+" fixes that for the two that had it
    wrong.
  - Two returned None on contention and let the caller decide; two logged
    and called sys.exit(0) from inside the helper. Both shapes are legitimate
    -- a drain that wants to exit 0 quietly differs from a job that wants a
    warning in its log -- so both are offered here rather than forcing one.
  - Only two warned when no locking primitive existed at all, which is the
    one case where the caller silently has no guard.

Usage — caller decides what contention means:

    lock = script_lock.acquire("podcast_watch")
    if lock is None:
        return 0
    try:
        ...
    finally:
        lock.close()

Usage — log a warning and exit 0, for jobs that want that shape:

    _lock = script_lock.acquire_or_exit("meeting_prepopulate", warn=log.warning)

The handle must stay referenced for as long as the lock is needed: the flock
is tied to the open file description, so letting the handle be garbage
collected releases the lock silently. That is why callers assign it even when
they never touch it again.

Lock files live in .locks/ next to the scripts, named "<name>.lock", matching
what each script used before this module existed -- so an in-flight run of an
older copy still excludes a newer one during a partial deploy.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, IO

try:
    import fcntl as _fcntl
except ImportError:                                  # non-POSIX
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:                                  # non-Windows
    _msvcrt = None

LOCK_DIR = Path(__file__).resolve().parent / ".locks"


def lock_path(name: str, *, dir: Path | None = None) -> Path:
    """Where the lock for `name` lives. Kept public so tests and callers can
    point at it without rebuilding the convention themselves."""
    return (dir or LOCK_DIR) / f"{name}.lock"


def acquire(name: str, *,
            warn: Callable[[str], None] | None = None,
            dir: Path | None = None) -> IO | None:
    """Take the single-instance lock for `name`, or None if another run holds it.

    `warn` is called only in the one genuinely surprising case: neither fcntl
    nor msvcrt is available, so there is no guard at all and the caller
    proceeds unprotected. Contention itself is not warned about here -- it is
    normal, and the caller is better placed to phrase it.

    `dir` overrides where the lock file lives. Needed because two callers
    (meeting_prepopulate, handoff_blob_pull) resolve their scripts directory
    through MEETING_PREPOP_SCRIPTS_DIR, so their lock has always followed that
    env var. Defaulting to this module's own directory instead would quietly
    merge two deliberately separate installs onto one lock -- or, worse, split
    one install across two if the env var is set for some invocations only.

    Opened "a+" rather than "w" deliberately; see the module docstring for why
    truncation breaks the guard on Windows.
    """
    target = lock_path(name, dir=dir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        if warn:
            warn(f"could not create lock directory {target.parent}")
        return None
    try:
        handle = open(target, "a+")
    except OSError:
        # Can't even open the lock file. Report contention rather than running
        # unguarded: whatever is wrong with the directory won't be fixed by
        # two copies of the job discovering it simultaneously.
        if warn:
            warn(f"could not open lock file {target}")
        return None

    if _fcntl is not None:
        try:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        except OSError:
            handle.close()
            return None
    elif _msvcrt is not None:
        try:
            # Ensure at least one byte exists before locking a one-byte range.
            # Two of the original copies wrote a sentinel line first and two
            # locked byte 0 of a possibly-empty file; do the safe union, and
            # only write when the file is actually empty so we never disturb
            # a file another process may hold.
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write("lock\n")
                handle.flush()
            handle.seek(0)
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return None
    else:
        if warn:
            warn("no file-locking primitive available; running without a "
                 "single-instance guard")
    return handle


def acquire_or_exit(name: str, *,
                    warn: Callable[[str], None] | None = None,
                    dir: Path | None = None) -> IO:
    """acquire(), but log and exit 0 when another run holds the lock.

    Exit 0, not non-zero: overlapping a still-running instance is the guard
    doing its job, and schedulers surface a non-zero exit as a failed job --
    which reads as a broken pipeline rather than a healthy one that declined
    to run twice.
    """
    handle = acquire(name, warn=warn, dir=dir)
    if handle is None:
        if warn:
            warn(f"another {name} instance is running; exiting")
        sys.exit(0)
    return handle
