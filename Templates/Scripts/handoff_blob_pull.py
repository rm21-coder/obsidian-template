#!/usr/bin/env python3
"""handoff_blob_pull.py — Tier B relay puller for the meeting pre-population
handoff (see docs/HANDOFF-ARCHITECTURE.md and docs/Azure-Blob-Handoff-Relay.md).

Mirrors complete handoff sets (<name>.json, .json.sha256, .sig, .ready) from an
Azure Blob Storage container down to a local folder, using a read/list/delete
-scoped SAS token — no OneDrive sync client, no tenant credentials, and (unlike
the OneDrive tier) no Full Disk Access grant needed on this Mac.

Deliberately dumb: this script only moves bytes and enforces the commit-marker
(.ready) convention. Integrity (SHA-256) and authenticity (HMAC) verification
happen downstream, in handoff_source.DropFolderSource.load(), exactly as they
would for any other drop-folder relay (rsync, SFTP, manual import). Point
meeting_prepopulate.py at the same local folder with:

    MEETING_PREPOP_SOURCE=drop
    MEETING_PREPOP_HANDOFF_DIR=<same path as HANDOFF_BLOB_LOCAL_DIR below>

Manual usage:
    python3 handoff_blob_pull.py                # one pull pass
    python3 handoff_blob_pull.py --dry-run
    python3 handoff_blob_pull.py --verbose
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from dotenv import load_dotenv

# Sibling module; make its directory importable rather than relying on
# sys.path[0] happening to be this script's directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import script_lock  # noqa: E402

load_dotenv(Path.home() / "dev" / "secrets" / ".env")

# ============================================================
# Config
# ============================================================

def _path_env(env_key: str, default: Path) -> Path:
    v = os.environ.get(env_key)
    return Path(v) if v else default


HOME = Path.home()
SCRIPTS_DIR = _path_env(
    'MEETING_PREPOP_SCRIPTS_DIR', HOME / 'Obsidian' / 'Templates' / 'Scripts')
LOCAL_DIR = _path_env('HANDOFF_BLOB_LOCAL_DIR', HOME / 'MeetingIngest')
LOG_DIR = _path_env('MEETING_PREPOP_LOG_DIR', SCRIPTS_DIR / 'logs')
LOG_FILE = LOG_DIR / 'handoff_blob_pull.log'

ACCOUNT_URL = os.environ.get('HANDOFF_BLOB_ACCOUNT_URL', '').rstrip('/')
CONTAINER = os.environ.get('HANDOFF_BLOB_CONTAINER', '')
SAS = os.environ.get('HANDOFF_BLOB_SAS', '').lstrip('?')

# Suffixes that make up one handoff set, longest-first so basename stripping
# doesn't stop at a shorter accidental match (".json" vs ".json.sha256").
SET_SUFFIXES = ('.json.sha256', '.ready', '.sig', '.json')

REQUEST_TIMEOUT = 30

log = logging.getLogger('handoff_blob_pull')


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter(
        '%(asctime)s %(levelname)s [%(funcName)s] %(message)s')
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def acquire_lock() -> object:
    """Single-instance guard; logs and exits 0 if another run holds it.

    The lock lives under this script's SCRIPTS_DIR, which honours
    MEETING_PREPOP_SCRIPTS_DIR -- so an install relocated by that env var
    keeps its own lock rather than sharing one with the default location.
    """
    return script_lock.acquire_or_exit(
        'handoff_blob_pull', warn=log.warning, dir=SCRIPTS_DIR / '.locks')


# ============================================================
# Blob REST helpers (SAS auth is just a query string — no SDK needed)
# ============================================================

def _blob_url(name: str) -> str:
    return f'{ACCOUNT_URL}/{CONTAINER}/{name}?{SAS}'


def list_blob_names(session: requests.Session) -> list[str]:
    url = f'{ACCOUNT_URL}/{CONTAINER}?restype=container&comp=list&{SAS}'
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    return [el.text for el in root.iter('Name') if el.text]


def download_blob(session: requests.Session, name: str, dest: Path) -> None:
    resp = session.get(_blob_url(name), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    tmp = dest.with_suffix(dest.suffix + '.part')
    tmp.write_bytes(resp.content)
    os.replace(tmp, dest)


def delete_blob(session: requests.Session, name: str, *, dry_run: bool) -> None:
    if dry_run:
        log.info('DRY-RUN: would delete blob %s', name)
        return
    resp = session.delete(_blob_url(name), timeout=REQUEST_TIMEOUT)
    if resp.status_code not in (202, 404):
        resp.raise_for_status()


# ============================================================
# Set discovery + pull
# ============================================================

def group_by_handoff_id(blob_names: list[str]) -> dict[str, set[str]]:
    """Map handoff id (e.g. 'schedule-handoff-2026-08-05.v1') -> the set of
    suffixes present remotely for it (e.g. {'.json', '.ready'})."""
    groups: dict[str, set[str]] = {}
    for name in blob_names:
        for suffix in SET_SUFFIXES:
            if name.endswith(suffix):
                handoff_id = name[: -len(suffix)]
                groups.setdefault(handoff_id, set()).add(suffix)
                break
    return groups


def pull_one(session: requests.Session, handoff_id: str, suffixes: set[str],
             *, dry_run: bool) -> None:
    local_ready = LOCAL_DIR / f'{handoff_id}.ready'
    if local_ready.exists():
        # Already landed locally in a prior run (e.g. crashed before the
        # remote cleanup below finished) — skip re-download, just clean up.
        log.info('%s already present locally; cleaning up remote copy',
                 handoff_id)
    elif '.ready' not in suffixes:
        log.debug('%s not yet complete remotely (have: %s); skipping',
                  handoff_id, sorted(suffixes))
        return
    else:
        log.info('pulling %s (%s)', handoff_id, sorted(suffixes))
        if dry_run:
            log.info('DRY-RUN: would download %s to %s', handoff_id, LOCAL_DIR)
        else:
            LOCAL_DIR.mkdir(parents=True, exist_ok=True)
            # Payload first, commit marker last — a crash mid-pull then never
            # leaves a local .ready for an incomplete set.
            for suffix in ('.json.sha256', '.sig', '.json', '.ready'):
                if suffix not in suffixes:
                    continue
                name = f'{handoff_id}{suffix}'
                download_blob(session, name, LOCAL_DIR / name)
            log.info('landed %s locally', handoff_id)

    # Delete the commit marker first so a crash mid-cleanup can't leave a
    # dangling remote set that looks "ready" to a second consumer.
    for suffix in ('.ready', '.json', '.json.sha256', '.sig'):
        if suffix in suffixes:
            delete_blob(session, f'{handoff_id}{suffix}', dry_run=dry_run)


def run(*, dry_run: bool) -> int:
    if not (ACCOUNT_URL and CONTAINER and SAS):
        log.error('HANDOFF_BLOB_ACCOUNT_URL, HANDOFF_BLOB_CONTAINER, and '
                  'HANDOFF_BLOB_SAS must all be set (see '
                  'docs/Azure-Blob-Handoff-Relay.md)')
        return 1

    session = requests.Session()
    try:
        blob_names = list_blob_names(session)
    except requests.RequestException as e:
        log.error('failed to list container %s/%s: %s', ACCOUNT_URL,
                  CONTAINER, e)
        return 1

    groups = group_by_handoff_id(blob_names)
    if not groups:
        log.info('no handoff blobs found')
        return 0

    for handoff_id, suffixes in sorted(groups.items()):
        try:
            pull_one(session, handoff_id, suffixes, dry_run=dry_run)
        except requests.RequestException as e:
            log.error('failed to pull %s: %s', handoff_id, e)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    setup_logging(args.verbose)
    lock_fd = acquire_lock()
    log.info('---- run start (dry_run=%s) ----', args.dry_run)
    try:
        return run(dry_run=args.dry_run)
    finally:
        del lock_fd  # release on process exit


if __name__ == '__main__':
    sys.exit(main())
