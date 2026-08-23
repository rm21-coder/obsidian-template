#!/usr/bin/env python3
"""
obsidian-rag-sync.py

Syncs an Obsidian vault to an Open WebUI "Knowledge" collection.

State (path -> hash + Open WebUI file_id) is kept at
~/.local/share/obsidian-rag-sync/state.json so re-runs only push
new/modified/deleted files, not the whole vault every time.

------------------------------------------------------------
SAFETY GUARDRAILS
------------------------------------------------------------
Bulk destructive operations are gated:

  1. Pre-mutation backup. Before each real run, state.json is copied to
     state.bak.YYYYMMDD-HHMMSS.json (last STATE_BACKUP_KEEP retained).

  2. Deletion ceiling. A run that would delete more than
     max(MAX_DELETIONS_ABS, MAX_DELETIONS_PCT * indexed_corpus_size) files
     refuses to proceed without --allow-bulk-delete.

  3. Insanity check. If a run would delete more files than it leaves
     untouched, refuse regardless of flags — that pattern is almost
     always a logic bug, not a real intent.

------------------------------------------------------------
THIN-CONTENT FILTERING
------------------------------------------------------------
Files whose extractable body text falls below MIN_BODY_CHARS are skipped on
*new add* (they're typically Obsidian template/placeholder notes with no
prose). Files already in the collection are NEVER removed just because they
fell below the threshold — only true filesystem deletions remove from the
collection.

------------------------------------------------------------
CLASSIFICATION GATING (optional — degrades gracefully if unused)
------------------------------------------------------------
If you tag notes with a `classification` frontmatter property (your own
convention — see e.g. Knowledge/Data Classification.md if you keep one),
this sync excludes any file whose classification is in
EXCLUDED_CLASSIFICATIONS from the local Open WebUI index. A previously-
indexed file that is later elevated to a blocked tier is deindexed on
the next sync — logged as DEINDEXED (classification) to distinguish from
true filesystem deletions.

Default policy: only `restricted` is blocked. A middle tier such as
`confidential` (board materials, vendor negotiations, personnel matters —
whatever your own scheme calls sensitive-but-authorized) is still allowed
into the local RAG by default, because the user is already cleared to view
it and the RAG is a single-user index on the user's own authorized
hardware. `restricted` is blocked because that tier is meant for
PHI/PII/credentials/regulated data, where mere presence in an LLM context
can carry compliance implications (e.g. HIPAA/GLBA) even for an authorized
user. Adjust INDEXABLE_CLASSIFICATIONS / EXCLUDED_CLASSIFICATIONS below to
match your own scheme.

If you don't use a `classification` property at all, every file simply
falls back to the default (`internal-use-only`) and is indexable — this
section is inert until you opt in. Unknown classification values fail
secure: they are blocked from indexing and a warning is logged so you can
fix the typo.

------------------------------------------------------------
QUARANTINE
------------------------------------------------------------
After MAX_FAILURES consecutive failures on the same file hash, a file is
quarantined until its content actually changes. --reset-quarantine clears it.

------------------------------------------------------------
RUN REPORTS
------------------------------------------------------------
Each run writes a human-readable status report into the vault at
Creations/RAG-Sync-YYYY-MM-DD_HHMMSS.md — scan counts, the new/modified/
deleted/deindexed diff, warnings, and any failures. Report filenames use
REPORT_FILENAME_PREFIX, which is also in EXCLUDE_FILENAME_PREFIXES so a
report never gets indexed back into its own collection on the next run.

------------------------------------------------------------
CONFIGURATION
------------------------------------------------------------
Configuration via env vars:
    OBSIDIAN_VAULT          Path to vault root          (default: ~/Obsidian)
    OPEN_WEBUI_URL          Open WebUI base URL         (default: http://localhost:3000)
    OPEN_WEBUI_API_KEY      Required.
    OBSIDIAN_COLLECTION_ID  Required.

Env vars are also auto-loaded from ~/dev/secrets/.env when present, so launchd
agents and manual shell runs see the same configuration without sourcing the
file by hand. Edit the load_dotenv() path below if your secrets live elsewhere.

Usage:
    python3 obsidian-rag-sync.py
    python3 obsidian-rag-sync.py --dry-run
    python3 obsidian-rag-sync.py --reset-quarantine
    python3 obsidian-rag-sync.py --allow-bulk-delete    # explicit consent for large deletes

Requirements:
    pip install --break-system-packages requests python-dotenv
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    # Load API keys from the canonical secrets file. Edit this path if your
    # secrets live elsewhere. If python-dotenv isn't installed, we silently
    # fall back to the existing os.environ — useful when the launchd plist
    # still supplies env vars directly.
    load_dotenv(Path.home() / "dev" / "secrets" / ".env")
except ImportError:
    pass

# ----- configuration -----

VAULT_PATH = Path(os.environ.get("OBSIDIAN_VAULT", str(Path.home() / "Obsidian"))).expanduser()
WEBUI_URL = os.environ.get("OPEN_WEBUI_URL", "http://localhost:3000").rstrip("/")
try:
    from secret_store import get_secret as _get_secret
    WEBUI_API_KEY = _get_secret("OPEN_WEBUI_API_KEY") or ""
except ImportError:  # keystore helper absent: env/.env only, as before
    WEBUI_API_KEY = os.environ.get("OPEN_WEBUI_API_KEY", "")
COLLECTION_ID = os.environ.get("OBSIDIAN_COLLECTION_ID", "")


def _state_dir() -> Path:
    """Runtime state dir: %LOCALAPPDATA% on Windows, ~/.local/share elsewhere."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "obsidian-rag-sync"
    return Path.home() / ".local" / "share" / "obsidian-rag-sync"


STATE_DIR = _state_dir()
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "sync.log"

EXCLUDE_DIRS = {
    ".obsidian",
    ".trash",
    "Templates",
    "Excalidraw",
    "VoiceInbox",
    "Z_attachments",
}

# Files whose name starts with any of these prefixes are skipped by scan_vault.
# Used to exclude this script's own per-run reports (see write_run_report) so
# they don't self-index back into the knowledge collection on subsequent runs.
EXCLUDE_FILENAME_PREFIXES = ("RAG-Sync-",)

MIN_BODY_CHARS = 100
MAX_FAILURES = 3

# Open WebUI 0.11.0 made upload processing asynchronous: POST /api/v1/files/
# returns as soon as the bytes land, with data.status == "pending", and text
# extraction happens on a background queue. How long to wait for that queue
# before giving up on a single file.
PROCESSING_TIMEOUT_SECONDS = 120
PROCESSING_POLL_SECONDS = 1.0

# Classification values that are SAFE to send to the local Open WebUI
# index. Anything outside this set (including unknown / typo'd values)
# is blocked at scan time. See the CLASSIFICATION GATING section above
# for the rationale: only `restricted` is excluded by default; `confidential`
# material is permitted because the local RAG is single-user and the
# user is already cleared for that tier. Adjust to match your own scheme.
INDEXABLE_CLASSIFICATIONS = frozenset({"public", "internal-use-only", "confidential"})
EXCLUDED_CLASSIFICATIONS = frozenset({"restricted"})
KNOWN_CLASSIFICATIONS = INDEXABLE_CLASSIFICATIONS | EXCLUDED_CLASSIFICATIONS

# Per-run report destination inside the vault, for visibility into sync
# health. Reports use REPORT_FILENAME_PREFIX so EXCLUDE_FILENAME_PREFIXES
# can skip them on subsequent scans. Reports live in Creations/ alongside
# other vault output; bulk-delete by filename prefix when triaged.
REPORT_DIR_REL = Path("Creations")
REPORT_FILENAME_PREFIX = "RAG-Sync-"
REPORT_LIST_LIMIT = 25  # cap each operation list at this size in the report

# ----- safety guardrails -----

# Refuse runs that would delete more than this many files (absolute floor).
MAX_DELETIONS_ABS = 50
# ...or more than this fraction of the indexed corpus.
MAX_DELETIONS_PCT = 0.05
# Keep this many state backups before pruning.
STATE_BACKUP_KEEP = 10

# ----- setup -----

STATE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger("obsidian-rag-sync")

session = requests.Session()


# ----- text extraction -----

FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
WIKILINK_RE = re.compile(r"!?\[\[[^\]]*\]\]")
IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
WS_RE = re.compile(r"\s+")
# Anchor at line start (multiline) so we only match the YAML key, not any
# occurrence of "classification:" in the body of the note.
CLASSIFICATION_RE = re.compile(r"(?m)^classification\s*:\s*(.+?)\s*$")


def extractable_body_chars(text: str) -> int:
    body = FRONTMATTER_RE.sub("", text, count=1)
    body = WIKILINK_RE.sub("", body)
    body = IMG_RE.sub("", body)
    body = WS_RE.sub(" ", body).strip()
    return len(body)


def parse_classification(text: str) -> str | None:
    """Return the lowercased classification value from frontmatter, or None
    if there is no frontmatter or no `classification:` key.

    The value is parsed only from within the leading `---...---` frontmatter
    block so a stray "classification: ..." line in the body cannot influence
    the gate. Strings are not unquoted — `confidential` and `"confidential"`
    both resolve to `confidential`.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    cm = CLASSIFICATION_RE.search(m.group(0))
    if not cm:
        return None
    value = cm.group(1).strip().strip('"').strip("'").lower()
    return value or None


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_vault() -> tuple[dict[str, dict], dict[str, str]]:
    """Walk the vault and return (all_files, excluded_by_classification).

    `all_files` contains every .md file that is eligible for the RAG index,
    keyed by vault-relative path. Each entry includes 'indexable' indicating
    whether the file passes the body-chars threshold for fresh indexing.

    `excluded_by_classification` is a separate dict mapping vault-relative
    paths of files whose `classification` is in EXCLUDED_CLASSIFICATIONS,
    or whose value is unknown (fail-secure). These paths are NOT in
    all_files. The value is the (lowercased) classification string for log
    output. If a previously-indexed file is in this dict, the caller is
    expected to deindex it on this sync.
    """
    out: dict[str, dict] = {}
    excluded: dict[str, str] = {}
    for md in VAULT_PATH.rglob("*.md"):
        rel = md.relative_to(VAULT_PATH)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if rel.name.startswith(EXCLUDE_FILENAME_PREFIXES):
            continue
        try:
            stat = md.stat()
        except OSError:
            continue
        if stat.st_size == 0:
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # Classification gate (see CLASSIFICATION GATING in module docstring).
        # Missing → indexable (default == internal-use-only).
        # Known indexable value → indexable.
        # Known excluded value → blocked.
        # Unknown value → blocked + warning (fail-secure).
        classification = parse_classification(text)
        if classification is not None:
            if classification in EXCLUDED_CLASSIFICATIONS:
                excluded[str(rel)] = classification
                continue
            if classification not in KNOWN_CLASSIFICATIONS:
                log.warning(
                    f"unknown classification '{classification}' on {rel}; "
                    f"excluding from index (fail-secure). Expected one of: "
                    f"{', '.join(sorted(KNOWN_CLASSIFICATIONS))}."
                )
                excluded[str(rel)] = classification
                continue

        body_chars = extractable_body_chars(text)
        out[str(rel)] = {
            "hash": file_hash(md),
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "body_chars": body_chars,
            "indexable": body_chars >= MIN_BODY_CHARS,
            "classification": classification or "internal-use-only",
        }
    return out, excluded


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("state file corrupt, starting fresh")
    return {"files": {}, "quarantine": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def backup_state() -> Path | None:
    """Create a timestamped backup of state.json. Prune older than STATE_BACKUP_KEEP."""
    if not STATE_FILE.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = STATE_DIR / f"state.bak.{ts}.json"
    shutil.copy2(STATE_FILE, backup_path)
    # Prune old backups
    backups = sorted(STATE_DIR.glob("state.bak.*.json"))
    for old in backups[:-STATE_BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return backup_path


def safe_filename(rel_path: str) -> str:
    return rel_path.replace("/", "__")


def upload_file(local_path: Path, rel_path: str) -> str:
    url = f"{WEBUI_URL}/api/v1/files/"
    with open(local_path, "rb") as f:
        files = {"file": (safe_filename(rel_path), f, "text/markdown")}
        r = session.post(url, files=files, timeout=120)
    r.raise_for_status()
    return r.json()["id"]


class DuplicateContent(Exception):
    """The collection already holds a file with exactly this content.

    0.11.0 rejects such an add with a 400. It means the index is already
    correct for this note, so it is a no-op, not a failure.
    """


def add_to_collection(file_id: str) -> None:
    url = f"{WEBUI_URL}/api/v1/knowledge/{COLLECTION_ID}/file/add"
    r = session.post(url, json={"file_id": file_id}, timeout=60)
    if r.status_code == 400 and "Duplicate content" in r.text:
        raise DuplicateContent(r.text[:200])
    if not r.ok:
        # requests' HTTPError carries only the status line and the URL. Both
        # 400s this endpoint returns look identical there, which is exactly
        # what made a wave of them impossible to tell apart in the log.
        raise requests.HTTPError(
            f"{r.status_code} from /file/add: {r.text[:200]}", response=r)


def remove_from_collection(file_id: str) -> None:
    """Remove a file from the collection. 400/404 is treated as success (already gone)."""
    url = f"{WEBUI_URL}/api/v1/knowledge/{COLLECTION_ID}/file/remove"
    r = session.post(url, json={"file_id": file_id}, timeout=60)
    if r.status_code in (400, 404):
        return
    r.raise_for_status()


def delete_file(file_id: str) -> None:
    """Delete a file. 400/404 is treated as success (already gone)."""
    url = f"{WEBUI_URL}/api/v1/files/{file_id}"
    r = session.delete(url, timeout=60)
    if r.status_code in (400, 404):
        return
    r.raise_for_status()


def wait_for_processing(file_id: str) -> str:
    """Block until Open WebUI has extracted the uploaded file's text.

    Since 0.11.0 the upload returns immediately with data.status "pending"
    and extraction runs on a background queue. Calling /file/add before that
    finishes fails with 400 "The content provided is empty", and in a bulk
    run — where the queue falls far behind the uploads — that is the common
    case rather than the rare one. Poll until the status leaves "pending".
    """
    url = f"{WEBUI_URL}/api/v1/files/{file_id}"
    deadline = time.monotonic() + PROCESSING_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        r = session.get(url, timeout=60)
        r.raise_for_status()
        status = ((r.json().get("data") or {}).get("status")) or "unknown"
        if status != "pending":
            return status
        time.sleep(PROCESSING_POLL_SECONDS)
    raise TimeoutError(
        f"upload {file_id} still 'pending' after {PROCESSING_TIMEOUT_SECONDS}s"
    )


def push_file(local_path: Path, rel_path: str) -> str:
    file_id = upload_file(local_path, rel_path)
    try:
        wait_for_processing(file_id)
        add_to_collection(file_id)
    except Exception:
        try:
            delete_file(file_id)
        except Exception:
            pass
        raise
    return file_id


# ----- safeguard checks -----

def evaluate_safeguards(new: list, modified: list, deleted: list, previous_count: int,
                        allow_bulk_delete: bool) -> tuple[bool, list[str]]:
    """Return (ok_to_proceed, warnings). If not ok and warnings, the run aborts."""
    blockers: list[str] = []
    warnings: list[str] = []

    deletion_ceiling = max(MAX_DELETIONS_ABS,
                           int(MAX_DELETIONS_PCT * previous_count) if previous_count > 0 else 0)

    # Insanity check — refuse regardless of flags
    unchanged_or_modified = previous_count - len(deleted)
    if previous_count > 50 and len(deleted) > unchanged_or_modified:
        blockers.append(
            f"INSANITY CHECK: would delete {len(deleted)} files but only "
            f"{unchanged_or_modified} would remain. This pattern is almost "
            f"certainly a logic bug, not a legitimate sync. Refusing regardless of flags. "
            f"To override, restore state from a backup or manually edit state.json."
        )

    # Deletion ceiling — overridable with --allow-bulk-delete
    if len(deleted) > deletion_ceiling and not allow_bulk_delete:
        blockers.append(
            f"DELETION CEILING: would delete {len(deleted)} files, ceiling is "
            f"{deletion_ceiling} (max({MAX_DELETIONS_ABS}, {MAX_DELETIONS_PCT:.0%} of "
            f"{previous_count} indexed)). Re-run with --allow-bulk-delete if intentional."
        )

    # Soft warning when not blocking
    if len(deleted) > 0 and len(deleted) <= deletion_ceiling and len(deleted) >= MAX_DELETIONS_ABS // 2:
        warnings.append(
            f"approaching deletion ceiling: {len(deleted)} of {deletion_ceiling} allowed"
        )

    return (len(blockers) == 0, blockers + warnings)


# ----- run report -----

def _fmt_list(items: list, limit: int = REPORT_LIST_LIMIT) -> str:
    if not items:
        return "_(none)_"
    shown = items[:limit]
    out = "\n".join(f"- `{p}`" for p in shown)
    if len(items) > limit:
        out += f"\n- _… and {len(items) - limit} more_"
    return out


def _fmt_error_list(items: list, limit: int = REPORT_LIST_LIMIT) -> str:
    if not items:
        return "_(none)_"
    shown = items[:limit]
    out = "\n".join(f"- `{p}` — {err}" for p, err in shown)
    if len(items) > limit:
        out += f"\n- _… and {len(items) - limit} more_"
    return out


def write_run_report(start_time: datetime, summary: dict, dry_run: bool) -> Path | None:
    """Write a per-run human-readable status report into the vault.

    Lives at <vault>/Creations/RAG-Sync-YYYY-MM-DD_HHMMSS.md so it's
    visible in Obsidian alongside other Creations output. The filename
    prefix is in EXCLUDE_FILENAME_PREFIXES so reports are not re-indexed
    into the knowledge collection.

    Wrapped in try/except — a report failure must never crash the sync.
    Returns the written path, or None on failure.
    """
    try:
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        errors = summary.get("errors", 0)
        safeguard_blocked = summary.get("safeguard_blocked", False)
        if safeguard_blocked:
            status = "ABORT"
        elif errors > 0:
            status = "FAIL"
        elif summary.get("warnings"):
            status = "WARN"
        else:
            status = "PASS"

        ts_filename = start_time.strftime("%Y-%m-%d_%H%M%S")
        ts_human = start_time.strftime("%Y-%m-%d %H:%M:%S")
        target = VAULT_PATH / REPORT_DIR_REL / f"{REPORT_FILENAME_PREFIX}{ts_filename}.md"
        target.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []
        lines.append("---")
        lines.append(f"title: RAG Sync Report — {ts_human}")
        lines.append(f"date: {ts_human}")
        lines.append("tags:")
        lines.append("  - rag-sync")
        lines.append("  - system-log")
        lines.append(f"sync_status: {status}")
        lines.append(f"errors: {errors}")
        lines.append(f"quarantined_total: {summary.get('quarantined_total', 0)}")
        if dry_run:
            lines.append("dry_run: true")
        lines.append("---")
        lines.append("")
        lines.append(f"# RAG Sync Report — {ts_human}")
        lines.append("")
        suffix = "  *(dry run)*" if dry_run else ""
        lines.append(f"**Status:** {status}{suffix}")
        lines.append(f"**Duration:** {duration:.1f}s")
        lines.append("")
        lines.append("## Scan")
        lines.append(f"- Total markdown files: {summary.get('total_files', 0)}")
        lines.append(f"- Indexable: {summary.get('indexable', 0)}")
        lines.append(f"- Below threshold: {summary.get('below_threshold', 0)}")
        lines.append(f"- Excluded by classification: {summary.get('excluded_by_classification', 0)}")
        lines.append("")
        lines.append("## Diff")
        lines.append(f"- New: {summary.get('new_count', 0)}")
        lines.append(f"- Modified: {summary.get('modified_count', 0)}")
        lines.append(f"- Deleted (filesystem): {summary.get('deleted_fs_count', 0)}")
        lines.append(f"- Deindexed (classification): {summary.get('deindexed_by_class_count', 0)}")
        lines.append(f"- Unchanged: {summary.get('unchanged_count', 0)}")
        lines.append(f"- Quarantined (skipped): {summary.get('quarantined_skip_count', 0)}")
        lines.append("")

        if summary.get("warnings"):
            lines.append("## Warnings")
            for w in summary["warnings"]:
                lines.append(f"- {w}")
            lines.append("")

        if safeguard_blocked:
            lines.append("## Safeguard Block")
            for b in summary.get("blockers", []):
                lines.append(f"- {b}")
            lines.append("")
            lines.append("_No files were mutated this run._")
            lines.append("")
        elif dry_run:
            lines.append("## Operations (would-be)")
            lines.append("### Would Add")
            lines.append(_fmt_list(summary.get("new_dry_run", [])))
            lines.append("")
            lines.append("### Would Update")
            lines.append(_fmt_list(summary.get("modified_dry_run", [])))
            lines.append("")
            lines.append("### Would Delete (filesystem)")
            lines.append(_fmt_list(summary.get("deleted_fs_dry_run", [])))
            lines.append("")
            lines.append("### Would Deindex (classification)")
            lines.append(_fmt_list(summary.get("deindexed_by_class_dry_run", [])))
            lines.append("")
            lines.append("### Quarantined (would skip)")
            lines.append(_fmt_list(summary.get("quarantined_skip_dry_run", [])))
            lines.append("")
        else:
            lines.append("## Operations")
            lines.append("### Added")
            lines.append(_fmt_list(summary.get("added_paths", [])))
            lines.append("")
            lines.append("### Updated")
            lines.append(_fmt_list(summary.get("updated_paths", [])))
            lines.append("")
            lines.append("### Deleted (filesystem)")
            lines.append(_fmt_list(summary.get("deleted_fs_paths", [])))
            lines.append("")
            lines.append("### Deindexed (classification)")
            lines.append(_fmt_list(summary.get("deindexed_by_class_paths", [])))
            lines.append("")

        if summary.get("update_failures") or summary.get("add_failures"):
            lines.append("## Failures")
            if summary.get("update_failures"):
                lines.append("### Update failures")
                lines.append(_fmt_error_list(summary["update_failures"]))
                lines.append("")
            if summary.get("add_failures"):
                lines.append("### Add failures")
                lines.append(_fmt_error_list(summary["add_failures"]))
                lines.append("")

        if summary.get("new_quarantine_entries"):
            lines.append("## Newly Quarantined This Run")
            lines.append(_fmt_list(summary["new_quarantine_entries"]))
            lines.append("")

        lines.append("## Metadata")
        if summary.get("reset_quarantine"):
            lines.append(
                f"- Quarantine reset: yes (cleared "
                f"{summary.get('reset_quarantine_count', 0)} entries)"
            )
        if summary.get("backup"):
            lines.append(f"- State backup: `{summary['backup']}`")
        lines.append(f"- Vault: `{VAULT_PATH}`")
        lines.append(f"- Collection: `{COLLECTION_ID}`")
        lines.append(f"- Started: {ts_human}")
        lines.append(f"- Completed: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        target.write_text("\n".join(lines), encoding="utf-8")
        log.info(f"run report written to {target.relative_to(VAULT_PATH)}")
        return target
    except Exception as exc:
        log.warning(f"could not write run report: {exc}")
        return None


# ----- main -----

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show diff without pushing")
    parser.add_argument("--reset-quarantine", action="store_true",
                        help="Clear the quarantine list and retry all files")
    parser.add_argument("--allow-bulk-delete", action="store_true",
                        help="Permit deletions exceeding the safety ceiling. Use with caution.")
    args = parser.parse_args()

    if not WEBUI_API_KEY:
        log.error("OPEN_WEBUI_API_KEY env var not set")
        return 1
    if not COLLECTION_ID:
        log.error("OBSIDIAN_COLLECTION_ID env var not set")
        return 1
    if not VAULT_PATH.is_dir():
        log.error(f"vault not found: {VAULT_PATH}")
        return 1

    session.headers.update({"Authorization": f"Bearer {WEBUI_API_KEY}"})

    log.info(f"vault={VAULT_PATH} webui={WEBUI_URL} collection={COLLECTION_ID}")

    # Per-run summary accumulator (consumed by write_run_report at every
    # completion path after this point — success, errors, safeguard abort,
    # and dry-run).
    start_time = datetime.now()
    summary: dict = {
        "errors": 0,
        "warnings": [],
        "blockers": [],
        "safeguard_blocked": False,
        "added_paths": [],
        "updated_paths": [],
        "deleted_fs_paths": [],
        "deindexed_by_class_paths": [],
        "update_failures": [],
        "add_failures": [],
        "new_quarantine_entries": [],
        "reset_quarantine": False,
        "reset_quarantine_count": 0,
    }

    state = load_state()
    if args.reset_quarantine:
        n = len(state.get("quarantine", {}))
        state["quarantine"] = {}
        log.info(f"cleared quarantine ({n} files)")
        summary["reset_quarantine"] = True
        summary["reset_quarantine_count"] = n

    quarantine = state.setdefault("quarantine", {})
    previous = state.setdefault("files", {})
    previous_count = len(previous)

    all_files, excluded_by_class = scan_vault()
    indexable = {p: d for p, d in all_files.items() if d["indexable"]}
    log.info(
        f"scanned {len(all_files) + len(excluded_by_class)} markdown files "
        f"({len(indexable)} indexable, "
        f"{len(all_files) - len(indexable)} below threshold, "
        f"{len(excluded_by_class)} excluded by classification)"
    )
    summary["total_files"] = len(all_files) + len(excluded_by_class)
    summary["indexable"] = len(indexable)
    summary["below_threshold"] = len(all_files) - len(indexable)
    summary["excluded_by_classification"] = len(excluded_by_class)

    new = sorted(set(indexable) - set(previous))
    modified = sorted(p for p in (set(indexable) & set(previous))
                      if indexable[p]["hash"] != previous[p]["hash"])
    # Files needing removal from Open WebUI on this run:
    #   (a) truly gone from the filesystem
    #   (b) still on disk but newly excluded by classification (e.g., user
    #       elevated `internal-use-only` → `confidential`)
    # A file cannot be in both — scan_vault routes by classification first.
    deleted_fs = sorted(set(previous) - set(all_files) - set(excluded_by_class))
    deindexed_by_class = sorted(set(previous) & set(excluded_by_class))
    deleted = deleted_fs + deindexed_by_class  # combined for API + safeguard accounting

    # Quarantine filter. Skip only once a file has actually reached
    # MAX_FAILURES on this exact content. record_failure() writes an entry
    # from the very first failure, so testing for the entry alone exiles a
    # file after one transient error and keeps it out until its content
    # happens to change — which is how a single bad sync run silently
    # dropped ~40% of the vault out of the index.
    def is_quarantined(p: str) -> bool:
        q = quarantine.get(p)
        return bool(q
                    and q["hash"] == indexable[p]["hash"]
                    and q.get("failures", 0) >= MAX_FAILURES)

    quarantined_skip = []
    for p in list(new):
        if is_quarantined(p):
            quarantined_skip.append(p)
            new.remove(p)
    for p in list(modified):
        if is_quarantined(p):
            quarantined_skip.append(p)
            modified.remove(p)

    unchanged = len(set(indexable) & set(previous)) - len(modified)

    log.info(
        f"diff: new={len(new)} modified={len(modified)} "
        f"deleted_fs={len(deleted_fs)} deindexed_by_class={len(deindexed_by_class)} "
        f"unchanged={unchanged} quarantined_skip={len(quarantined_skip)}"
    )
    summary["new_count"] = len(new)
    summary["modified_count"] = len(modified)
    summary["deleted_fs_count"] = len(deleted_fs)
    summary["deindexed_by_class_count"] = len(deindexed_by_class)
    summary["unchanged_count"] = unchanged
    summary["quarantined_skip_count"] = len(quarantined_skip)

    # Safeguard evaluation
    ok, messages = evaluate_safeguards(new, modified, deleted, previous_count,
                                       args.allow_bulk_delete)
    for m in messages:
        if not ok:
            log.error(m)
            summary["blockers"].append(m)
        else:
            log.warning(m)
            summary["warnings"].append(m)

    if not ok:
        if args.dry_run:
            log.error("DRY-RUN ABORT: safeguards would block this sync. See errors above.")
        else:
            log.error("ABORT: safeguards triggered. No changes made. See errors above.")
        # Also show what would have happened, for debugging
        for p in deleted[:10]:
            log.error(f"  would-delete: {p}")
        if len(deleted) > 10:
            log.error(f"  ... and {len(deleted) - 10} more")
        summary["safeguard_blocked"] = True
        summary["quarantined_total"] = len(quarantine)
        write_run_report(start_time, summary, args.dry_run)
        return 3

    if args.dry_run:
        for p in new:
            log.info(f"NEW      {p}")
        for p in modified:
            log.info(f"MODIFIED {p}")
        for p in deleted_fs:
            log.info(f"DELETED  {p}")
        for p in deindexed_by_class:
            log.info(f"DEINDEXED (classification: {excluded_by_class[p]}) {p}")
        for p in quarantined_skip:
            log.info(f"QUARANTINED (skip) {p}")
        summary["new_dry_run"] = list(new)
        summary["modified_dry_run"] = list(modified)
        summary["deleted_fs_dry_run"] = list(deleted_fs)
        summary["deindexed_by_class_dry_run"] = list(deindexed_by_class)
        summary["quarantined_skip_dry_run"] = list(quarantined_skip)
        summary["quarantined_total"] = len(quarantine)
        write_run_report(start_time, summary, dry_run=True)
        return 0

    # Backup state before any mutation
    if new or modified or deleted:
        backup = backup_state()
        if backup:
            log.info(f"state backed up to {backup.name}")
            summary["backup"] = backup.name

    errors = 0

    def record_failure(path: str, exc: Exception) -> None:
        h = (indexable.get(path) or all_files.get(path) or previous.get(path, {})).get("hash", "")
        existing = quarantine.get(path, {})
        prev_failures = existing.get("failures", 0) if existing.get("hash") == h else 0
        new_failures = prev_failures + 1
        quarantine[path] = {
            "hash": h,
            "failures": new_failures,
            "last_error": str(exc)[:200],
            "last_attempt": datetime.now().isoformat(),
        }
        if new_failures >= MAX_FAILURES:
            log.warning(f"  quarantined after {new_failures} failures: {path}")
            summary["new_quarantine_entries"].append(path)

    # Removals: always release the local state entry, even if the API
    # calls error out — the file is gone from the indexable set, holding the
    # reference in state forever just causes this same loop on every run.
    # Distinguish filesystem-deletes from classification-driven deindexing
    # in the log (and report) so the operator can audit either category
    # cleanly.
    for path in deleted:
        is_class_deindex = path in excluded_by_class
        kind = (f"deindexed (classification: {excluded_by_class[path]})"
                if is_class_deindex else "deleted")
        file_id = previous[path].get("file_id")
        api_warnings: list[str] = []
        if file_id:
            try:
                remove_from_collection(file_id)
            except Exception as exc:
                api_warnings.append(f"remove_from_collection: {exc}")
            try:
                delete_file(file_id)
            except Exception as exc:
                api_warnings.append(f"delete_file: {exc}")
        previous.pop(path, None)
        quarantine.pop(path, None)
        if is_class_deindex:
            summary["deindexed_by_class_paths"].append(path)
        else:
            summary["deleted_fs_paths"].append(path)
        if api_warnings:
            log.warning(f"{kind} (with API warnings): {path} | {' | '.join(api_warnings)}")
        else:
            log.info(f"{kind}: {path}")

    # Modifications. Push the new copy FIRST, and purge the old one only
    # once the new one is actually in the collection. The previous ordering
    # removed the old file up front, so any failure on the way back in — a
    # 400 from an upload the server had not finished processing, a dropped
    # connection — left the note with no copy in the index at all, and the
    # loss was silent because the file still existed in the vault.
    for path in modified:
        old_id = previous[path].get("file_id")
        try:
            new_id = push_file(VAULT_PATH / path, path)
        except DuplicateContent:
            # The collection already holds this exact content, so the local
            # state was stale rather than the index. Keep the association we
            # have and leave the collection untouched — re-pushing would
            # only trade a good entry for a rejected one.
            if not old_id:
                log.warning(f"duplicate content, no known file id: {path}")
                continue
            previous[path] = {**indexable[path], "file_id": old_id}
            quarantine.pop(path, None)
            log.info(f"already current: {path}")
            summary["updated_paths"].append(path)
            continue
        except Exception as exc:
            log.error(f"update failed {path}: {exc}")
            record_failure(path, exc)
            errors += 1
            summary["update_failures"].append((path, str(exc)[:200]))
            continue
        if old_id:
            try:
                remove_from_collection(old_id)
                delete_file(old_id)
            except Exception as exc:
                log.warning(f"could not purge old {path}: {exc}")
        previous[path] = {**indexable[path], "file_id": new_id}
        quarantine.pop(path, None)
        log.info(f"updated: {path}")
        summary["updated_paths"].append(path)

    # New files
    for path in new:
        try:
            file_id = push_file(VAULT_PATH / path, path)
            previous[path] = {**indexable[path], "file_id": file_id}
            quarantine.pop(path, None)
            log.info(f"added: {path}")
            summary["added_paths"].append(path)
        except DuplicateContent:
            # Byte-identical to a note already in the collection. Not an
            # error worth quarantining over, but worth seeing in the log.
            log.warning(f"duplicate of an already-indexed note, skipped: {path}")
            continue
        except Exception as exc:
            log.error(f"add failed {path}: {exc}")
            record_failure(path, exc)
            errors += 1
            summary["add_failures"].append((path, str(exc)[:200]))

    state["files"] = previous
    state["quarantine"] = quarantine
    state["last_sync"] = datetime.now().isoformat()
    save_state(state)

    log.info(f"sync complete. errors={errors} quarantined_total={len(quarantine)}")
    summary["errors"] = errors
    summary["quarantined_total"] = len(quarantine)
    write_run_report(start_time, summary, args.dry_run)
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
