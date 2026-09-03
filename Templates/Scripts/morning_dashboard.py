#!/usr/bin/env python3
"""
Morning Dashboard for Obsidian
==============================

Generates a single self-contained HTML page summarizing, for today:

  1. Open to-dos      -- all unchecked `- [ ]` lines anywhere in the vault
                         (mirrors Actions/To-Do.md's `tasks / not done /
                         short mode` query), displayed as a single flat list.
                         Excludes Actions/, Clippings/, Z_attachments/,
                         Templates/, Z_dashboards/, and .obsidian/.
  2. Meetings today   -- every file in Meetings/ whose filename begins with
                         today's date. Labels by type:
                           * Group       -> the group name
                           * Individual  -> "Last, First 1:1"
                           * Ad-hoc      -> the meeting title
  3. New today        -- every .md file (and .docx etc.) in Clippings/** or
                         Creations/** whose macOS file birth time is at or
                         after midnight today.

The HTML is written to <vault>/Z_dashboards/morning-YYYY-MM-DD.html (with a
stable copy at morning.html) and opened in Chrome.

Each item links back to the source note via obsidian:// URIs so a click in
Chrome jumps straight into the vault.

Run by hand for testing:
    /usr/bin/python3 ~/Obsidian/Templates/Scripts/morning_dashboard.py

The vault defaults to ~/Obsidian. Set OBSIDIAN_VAULT to point at another one --
useful for demoing the dashboard against the synthetic dataset in the template
repo, which is not where your real vault lives:

    OBSIDIAN_VAULT=~/dev/repos/obsidian-template \\
        python3 Templates/Scripts/morning_dashboard.py --no-open

The obsidian:// links follow the override: they are built from the vault
folder's name, so they open the vault that was actually scanned.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOME           = Path.home()

# Vault location. Defaults to ~/Obsidian (the documented convention on both
# macOS and Windows); override with OBSIDIAN_VAULT to point the dashboard at a
# different vault -- a second vault, or the template repo itself when demoing
# against the synthetic dataset. Same variable name the other vault-locating
# scripts use (refresh_groups.py, seed_demo_content.py).
VAULT          = Path(os.environ.get("OBSIDIAN_VAULT") or HOME / "Obsidian")
# Obsidian resolves obsidian://open?vault=<name> by the vault's folder name, so
# derive it rather than hardcoding "Obsidian" -- otherwise every link on the
# page would point at a vault that isn't the one we just scanned.
VAULT_NAME     = VAULT.name
DASHBOARDS_DIR = VAULT / "Z_dashboards"

ACTIONS_DIR    = VAULT / "Actions"
MEETINGS_DIR   = VAULT / "Meetings"
CLIPPINGS_DIR  = VAULT / "Clippings"
CREATIONS_DIR  = VAULT / "Creations"

# obsidian-rag-sync.py writes one per-run health report into Creations/,
# named RAG-Sync-YYYY-MM-DD_HHMMSS.md. The dashboard reads the newest one to
# surface sync health (see Section 4) and excludes them from "New today" so
# they don't double up as clutter there.
RAG_REPORT_PREFIX = "RAG-Sync-"

# Folders to skip when scanning the vault for task lines. Kept minimal --
# the `#task` tag filter below does the real exclusion work. We only skip
# Obsidian's own config dir, the trash, and Templates (template placeholder
# tasks aren't real work).
TODO_EXCLUDE_DIRS = {
    ".obsidian",
    ".trash",
    "Templates",
}

# Open-status checkboxes: `[ ]` = Todo, `[/]` = In Progress. Both are
# considered "not done" by the Obsidian Tasks plugin and should surface
# on the dashboard. `[x]` (Done) and `[-]` (Cancelled) are excluded.
TODO_RE = re.compile(r"^[ \t]*[-*+]\s*\[[ /]\]\s*(.+?)\s*$")

# Required tag for an item to count. Matches the Obsidian Tasks plugin's
# `globalFilter: "#task"` setting in this vault. Without this filter we
# pick up free-text checkboxes (packing lists, etc.) that the user does
# not consider real tasks.
TASK_TAG_REQUIRED = re.compile(r"(?<!\S)#task\b")

# Strips the `#task` tag from the displayed text (Tasks plugin's
# `removeGlobalFilter: true` does the same in Obsidian's renderer).
TASK_TAG_RE = re.compile(r"(?<!\S)#task\b\s*")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def obsidian_uri(rel_path: Path) -> str:
    """Build an obsidian://open URI for a vault-relative path.
    Uses urllib.parse.quote so spaces become %20, NOT `+`. Obsidian's
    URL handler treats `+` literally as a `+` character in the filename
    rather than as a space, which breaks links to any file with a space
    in its path (e.g. `Meetings/History/2026-03-04 1400.md`)."""
    vault = urllib.parse.quote(VAULT_NAME, safe="")
    file  = urllib.parse.quote(str(rel_path.with_suffix("")), safe="/")
    return f"obsidian://open?vault={vault}&file={file}"


def vault_rel(p: Path) -> Path:
    return p.relative_to(VAULT)


def strip_task_metadata(text: str) -> str:
    """Clean a task line for display: drop the `#task` tag (the Obsidian
    Tasks plugin marker) and collapse the resulting whitespace. Other
    metadata like priority emojis and due dates are intentionally kept --
    they're useful at a glance."""
    cleaned = TASK_TAG_RE.sub("", text)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def now_local() -> _dt.datetime:
    """Current time in the system's actual configured local timezone.

    Deliberately NOT hardcoded to a specific zone (this used to force
    America/New_York): every staleness check in this script compares "now"
    against file mtimes, which are always in whatever timezone the system
    itself is set to. Hardcoding a different zone here created a constant
    skew between the two -- e.g. this Mac's clock correctly follows the
    user's current location while traveling (Central while in Memphis, say),
    not always Eastern, and file mtimes followed right along. A `now_local()`
    stuck on Eastern would then look up to an hour "later" than the file it's
    judging, permanently eating into every tight cadence's staleness budget."""
    return _dt.datetime.now().astimezone()


def today_local() -> _dt.date:
    return now_local().date()


# ---------------------------------------------------------------------------
# Section 1: Open to-dos
# ---------------------------------------------------------------------------

def collect_todos() -> list[tuple[Path, str]]:
    """Walk the vault and return a flat list of (source_rel_path, text)
    tuples for each task -- matching the Obsidian Tasks plugin's behavior
    on this vault:
        - Open or in-progress status: `- [ ]` or `- [/]`
        - Must contain the `#task` tag (the plugin's globalFilter)
        - Vault-wide except .obsidian, .trash, Templates
    The displayed text has `#task` stripped (mirroring the plugin's
    removeGlobalFilter setting) but priorities and due dates are kept.
    Each item links back to its source file via obsidian://open."""
    items: list[tuple[Path, str]] = []
    for root, dirs, files in os.walk(VAULT):
        rel_root = Path(root).relative_to(VAULT)
        if rel_root.parts and rel_root.parts[0] in TODO_EXCLUDE_DIRS:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in TODO_EXCLUDE_DIRS]
        for fn in files:
            if not fn.endswith(".md"):
                continue
            fp = Path(root) / fn
            try:
                lines = fp.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            rel = vault_rel(fp)
            for ln in lines:
                m = TODO_RE.match(ln)
                if not m:
                    continue
                raw = m.group(1)
                if not TASK_TAG_REQUIRED.search(raw):
                    continue  # missing #task -- not a task per Obsidian
                text = strip_task_metadata(raw)
                if text:
                    items.append((rel, text))
    items.sort(key=lambda it: it[1].lower())
    return items


# ---------------------------------------------------------------------------
# Section 2: Today's meetings
# ---------------------------------------------------------------------------

def collect_meetings(today: _dt.date) -> list[Path]:
    """Return Meetings/<today YYYY-MM-DD>*.md files, sorted by filename
    (which sorts by time-of-day given the meeting_prep.py convention)."""
    if not MEETINGS_DIR.is_dir():
        return []
    prefix = today.isoformat()
    out = []
    for p in MEETINGS_DIR.iterdir():
        if p.is_file() and p.name.startswith(prefix) and p.suffix == ".md":
            out.append(p)
    out.sort(key=lambda p: p.name)
    return [vault_rel(p) for p in out]


def _frontmatter(p: Path) -> str:
    """Return the raw YAML frontmatter block (without the --- fences),
    or an empty string if the file has none."""
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def _fm_scalar(fm: str, key: str) -> str:
    """Pull a scalar YAML value (single line) by key. Strips quotes."""
    m = re.search(rf'^{re.escape(key)}\s*:\s*(.+?)\s*$', fm, re.MULTILINE)
    if not m:
        return ""
    val = m.group(1).strip()
    if val.startswith('"') and val.endswith('"'):
        val = val[1:-1]
    return val


def _fm_list(fm: str, key: str) -> list[str]:
    """Pull a YAML list whose items look like  - "[[Something]]" or  - bare."""
    out: list[str] = []
    in_block = False
    for ln in fm.splitlines():
        if re.match(rf"^{re.escape(key)}\s*:\s*$", ln):
            in_block = True
            continue
        if in_block:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:", ln):
                break  # next top-level key
            m = re.match(r'^\s*-\s*"?\[\[(.+?)\]\]"?\s*$', ln)
            if m:
                out.append(m.group(1).strip())
                continue
            m = re.match(r'^\s*-\s*"?(.+?)"?\s*$', ln)
            if m:
                out.append(m.group(1).strip())
    return out


def meeting_time_label(rel: Path) -> str:
    """Pull the meeting time out of the filename 'YYYY-MM-DD HHMM ...'
    and format it as 12-hour with AM/PM ('9:15 AM', '1:00 PM', '12:00 PM')."""
    m = re.match(r"^\d{4}-\d{2}-\d{2}\s+(\d{2})(\d{2})\b", rel.name)
    if not m:
        return ""
    hh, mm = int(m.group(1)), m.group(2)
    suffix = "AM" if hh < 12 else "PM"
    hh12 = hh % 12 or 12   # 0 and 12 both render as 12
    return f"{hh12}:{mm} {suffix}"


def meeting_label(rel: Path) -> str:
    """Build the dashboard label per user spec:
        - Group:      group name (or first group entry)
        - Individual: 'Last, First 1:1'
        - Ad-hoc:     meeting title
    Falls back to the filename stem if any of those are missing.
    """
    fm     = _frontmatter(VAULT / rel)
    mtype  = _fm_scalar(fm, "type").strip()
    if mtype == "Group":
        groups = _fm_list(fm, "group")
        if groups:
            return groups[0]
    elif mtype == "Individual":
        people = _fm_list(fm, "people")
        if people:
            return f"{people[0]} 1:1"
    elif mtype == "Ad-hoc":
        title = _fm_scalar(fm, "title")
        if title:
            return title
    # Fallback: strip the leading 'YYYY-MM-DD HHMM ' from the filename.
    name = rel.name[:-3] if rel.name.endswith(".md") else rel.name
    m = re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{4}\s*(.*)$", name)
    return (m.group(1) or name) if m else name


# ---------------------------------------------------------------------------
# Section 3: New today -- Clippings/ + Creations/ from midnight onward
# ---------------------------------------------------------------------------

def file_birthtime(p: Path) -> _dt.datetime:
    st = p.stat()
    # macOS exposes birth time; fall back to mtime elsewhere.
    ts = getattr(st, "st_birthtime", st.st_mtime)
    return _dt.datetime.fromtimestamp(ts)


_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

def created_date(p: Path) -> _dt.date | None:
    """Pull the canonical clip/creation date out of frontmatter.
    Tries `created:` first (the standard Clippings field), then `date:`
    (used by the AI weekly-research Creations). Returns the date portion
    only -- the time-of-day in clip frontmatter is unreliable anyway."""
    fm = _frontmatter(p)
    for key in ("created", "date"):
        val = _fm_scalar(fm, key)
        if val:
            m = _DATE_RE.search(val)
            if m:
                try:
                    return _dt.date.fromisoformat(m.group(1))
                except ValueError:
                    pass
    return None


def _kind_for(rel: Path, source: str) -> str:
    """Classify a 'new today' entry for the tag pill in the UI."""
    if rel.parts and rel.parts[0] == "Creations":
        return "creation"
    if rel.parts and rel.parts[0] == "Clippings":
        if len(rel.parts) > 1 and rel.parts[1] == "YouTube":
            return "video"
        if "youtube.com" in source or "youtu.be" in source:
            return "video"
        return "article"
    return "note"


def _parse_meta(p: Path) -> dict:
    """Pull title + source url out of frontmatter when present."""
    info = {"title": p.stem, "source": ""}
    fm = _frontmatter(p)
    if not fm:
        return info
    title = _fm_scalar(fm, "title")
    if title:
        info["title"] = title
    src = _fm_scalar(fm, "source")
    if src:
        info["source"] = src
    return info


def collect_new_today(today: _dt.date) -> list[tuple[Path, _dt.datetime, dict]]:
    """Every file in Clippings/** and Creations/** that is actually new today.

    Primary signal is the frontmatter `created:` (or `date:`) field, since
    files often get re-touched by the tagger / linter on later days and
    their file mtime/birthtime would otherwise misclassify them as new.
    Files with no parseable frontmatter date fall back to the file
    birthtime, but only when the birthtime is *today* -- never trust
    birthtime to *include* something whose frontmatter contradicts it."""
    start = _dt.datetime.combine(today, _dt.time(0, 0))
    end   = start + _dt.timedelta(days=1)
    out: list[tuple[Path, _dt.datetime, dict]] = []
    for base in (CLIPPINGS_DIR, CREATIONS_DIR):
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            for fn in files:
                if fn.startswith(".") or fn == ".DS_Store":
                    continue
                if fn.startswith(RAG_REPORT_PREFIX):
                    continue  # RAG sync reports get their own status section
                fp = Path(root) / fn
                try:
                    bt = file_birthtime(fp)
                except OSError:
                    continue
                # Decide whether this counts as "new today".
                if fp.suffix.lower() == ".md":
                    cd = created_date(fp)
                    if cd is None:
                        # No frontmatter date -- accept only if birthtime
                        # falls in today's window.
                        if not (start <= bt < end):
                            continue
                    elif cd != today:
                        # Frontmatter says it was clipped/created on a
                        # different day -- skip, even if mtime is today.
                        continue
                else:
                    # Non-md (.docx, .pdf, ...) -- no frontmatter to check.
                    if not (start <= bt < end):
                        continue

                rel = vault_rel(fp)
                if fp.suffix.lower() == ".md":
                    info = _parse_meta(fp)
                else:
                    info = {"title": fp.stem, "source": ""}
                info["kind"] = _kind_for(rel, info["source"])
                out.append((rel, bt, info))
    out.sort(key=lambda r: r[1])
    return out


# ---------------------------------------------------------------------------
# Section 4: RAG sync health -- newest obsidian-rag-sync.py run report
# ---------------------------------------------------------------------------

RAG_REPORT_RE = re.compile(r"^RAG-Sync-(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})\.md$")

# A daily job that hasn't reported in this many hours is treated as stale.
# The nightly run is ~03:15, so by the 07:00 dashboard a healthy pipeline has
# a report only a few hours old; 28h tolerates a late run but flags a miss.
RAG_STALE_HOURS = 28


def collect_rag_sync_status() -> dict | None:
    """Read the newest Creations/RAG-Sync-*.md report and summarize its health.

    obsidian-rag-sync.py writes one report per run with a `sync_status:` of
    PASS / WARN / FAIL / ABORT in its frontmatter. Surfacing the latest here
    means a broken nightly sync -- e.g. a rejected Open WebUI API key after a
    version upgrade -- shows up on the morning dashboard instead of staying
    buried in a report file. Returns None if no report exists."""
    if not CREATIONS_DIR.is_dir():
        return None
    newest_dt: _dt.datetime | None = None
    newest_path: Path | None = None
    for p in CREATIONS_DIR.iterdir():
        if not p.is_file():
            continue
        m = RAG_REPORT_RE.match(p.name)
        if not m:
            continue
        try:
            run_dt = _dt.datetime.fromisoformat(
                f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}"
            )
        except ValueError:
            continue
        if newest_dt is None or run_dt > newest_dt:
            newest_dt = run_dt
            newest_path = p
    if newest_path is None:
        return None

    fm = _frontmatter(newest_path)
    status = (_fm_scalar(fm, "sync_status") or "UNKNOWN").upper()
    errors = _fm_scalar(fm, "errors") or "0"
    quarantined = _fm_scalar(fm, "quarantined_total") or "0"

    now_naive = now_local().replace(tzinfo=None)
    age_hours = (now_naive - newest_dt).total_seconds() / 3600.0

    return {
        "status": status,
        "errors": errors,
        "quarantined": quarantined,
        "run_dt": newest_dt,
        "age_hours": age_hours,
        "stale": age_hours > RAG_STALE_HOURS,
        "rel": vault_rel(newest_path),
    }


# ---------------------------------------------------------------------------
# Section 5: Pipeline health -- freshness + exit status of every launchd job
#            that runs a script out of Templates/Scripts/.
# ---------------------------------------------------------------------------
#
# Why this exists: the vault's automation is a set of loosely-coupled launchd
# agents that share hidden dependencies (one venv feeds the tagger, the voice
# cleanup, and the rag sync; one Tag Taxonomy.md gates the tagger). When one of
# those breaks -- e.g. the shared .venv was deleted on 2026-06-25 and silently
# killed tagging for three weeks -- nothing surfaced it until missing tags were
# noticed by hand. This section gives every pipeline a heartbeat, so a dead job
# shows up on the next morning's dashboard instead of weeks later.
#
# Signals, in order of reliability:
#   1. launchctl LastExitStatus -- a non-zero exit, or a job that is not loaded
#      at all, is a hard failure. Catches crashes and exec failures such as the
#      missing-interpreter case.
#   2. Log freshness -- the newest mtime of the job's StandardOutPath /
#      StandardErrorPath is the last time it ran. Older than the job's cadence
#      allows means it has stopped firing.
#   3. `launchctl print` -- runs, last exit code, state and minimum runtime.
#      Only consulted for a job already known to be unhealthy, because it
#      costs a subprocess each. A high run count with a clean exit code is a
#      definitive crash-loop signature that `launchctl list` cannot see.
#   4. The tail of the job's error log -- for a job failing right now, the
#      last distinct line is usually the cause stated outright.
#   5. Targeted probes of shared dependencies -- the venv behind the job's
#      interpreter, and the LLM gateway every Claude-calling script routes
#      through (Section 5b).
#
# Signals 3-5 were added on 2026-09-03 after this section reported a guess as
# a diagnosis. The standing rule since: a cause appears here only once it has
# been checked, and a failure whose cause was not established is reported as
# symptoms plus where to look. See the notes above PIPELINE_HINTS and
# "Verified diagnosis helpers".
#
# No other pipeline scripts need to change: discovery reads the installed
# .plist files directly. The two env overrides exist only so the render can be
# exercised against fixtures in a test; in production the defaults are used.

LAUNCHAGENTS_DIR = Path(os.environ.get(
    "MD_LAUNCHAGENTS_DIR", str(HOME / "Library" / "LaunchAgents")))
LAUNCHCTL_BIN = os.environ.get("MD_LAUNCHCTL", "/bin/launchctl")
PLUTIL_BIN    = os.environ.get("MD_PLUTIL", "/usr/bin/plutil")

# Only agents whose program runs a script from here are treated as ours.
PIPELINE_MARKER = "/Obsidian/Templates/Scripts/"

# The rag-sync job has its own detailed section above; don't list it twice.
PIPELINE_EXCLUDE = {"com.obsidian-rag-sync"}

# Friendly display names; anything not listed falls back to a cleaned-up label.
# Keys are the labels the shipped plists declare (rag-sync is excluded above).
PIPELINE_NAMES = {
    "com.tag-clippings":                 "Tagger",
    "com.voice-cleanup":                 "Voice cleanup",
    "com.obsidian.strip-ads":            "Ad stripper",
    "com.obsidian.meeting-prep":         "Meeting prep",
    "com.meeting-prepopulate":           "Meeting pre-populate",
    "com.obsidian.meeting-pull":         "Meeting pull (calendar)",
    "com.morning-dashboard":             "Morning dashboard",
    "com.obsidian.group-photos":         "Group photos",
    "com.obsidian.source-mail-pull":     "Source mail pull",
    "com.obsidian.podcast-watch":        "Podcast watch",
    "com.obsidian.security.integrity":   "File integrity monitor",
    "com.obsidian.security.plugin-check": "Plugin integrity check",
    "com.obsidian.vault-lint":           "Vault lint (weekly)",
    "com.obsidian.handoff-blob-pull":    "Handoff blob relay",
    # Pre-rename labels, kept so an install that predates the namespaced
    # labels still gets friendly names:
    "com.meeting-prep":                  "Meeting prep",
    "com.meeting-pull":                  "Meeting pull (calendar)",
    "com.strip-ads":                     "Ad stripper",
}

# Short remediation hints, shown only when a job is unhealthy.
# A hint says what the job is FOR and where its own evidence lives. It must
# never name a cause -- causes are computed from checked signals further down
# (see "Verified diagnosis helpers"), and a guessed cause in a fixed string
# reads exactly like a finding.
#
# What this rule is paying for, 2026-09-03: the voice-cleanup hint used to read
# "Shares the same .venv as the tagger; if the tagger is down too, one venv
# rebuild fixes both." Both halves were wrong. Templates/Scripts/.venv was
# healthy, and the tagger was not down -- it was running on schedule and
# cleanly skipping. The actual cause was voice_cleanup.py resolving its LLM
# client once before entering its watch loop and calling sys.exit(0) when
# api.ai.example.edu did not resolve (the VPN was down), which under KeepAlive
# produced 7,152 respawns at launchd's 10s minimum-runtime floor. The hint
# had invented a shared-dependency story out of nothing but a shared path,
# and it cost a morning aimed at the wrong component.
PIPELINE_HINTS = {
    "com.tag-clippings": (
        "Tags stop appearing on new clippings. Its own account of what went "
        "wrong is in ~/Library/Logs/tag-clippings.err."
    ),
    "com.voice-cleanup": (
        "Dropped voice memos stop becoming notes. The watcher logs every "
        "cause it hits to ~/Library/Logs/voice-cleanup.err."
    ),
    "com.obsidian.meeting-pull": (  # same job, namespaced label
        "See com.meeting-pull below."
    ),
    "com.meeting-pull": (
        "The producer half: no fresh handoff means no meeting notes, even "
        "though the pre-populate consumer looks healthy. Check "
        "~/Library/Logs/meeting-pull.log. Usual causes: the Claude CLI needs "
        "re-auth, or the MCP tool names in .config/meeting_pull.json no "
        "longer match what `claude mcp list` exposes."
    ),
    "com.obsidian.security.integrity": (
        "Exits non-zero when it detects file drift -- usually your own recent "
        "script edits. Review the alert, then adopt the new baseline: "
        "/usr/bin/python3 integrity_monitor.py --update."
    ),
    "com.obsidian.vault-lint": (
        "Weekly content sweep. It runs with --exit-zero, so a non-zero status "
        "here means the lint itself failed, not that the vault is dirty -- "
        "findings are in ~/Library/Logs/vault-lint.log. Being weekly, it gets "
        "~8 days of slack before it reads as stale."
    ),
}

# Wrapper-launched jobs (e.g. /usr/bin/open running an .app) exit immediately,
# so their launchd StandardOut/ErrorPath never reflect the real worker. Point
# those at the log the worker actually writes. Value: (path under
# Templates/Scripts, stale-after hours, trigger label for display).
PIPELINE_HEARTBEAT = {
    "com.meeting-prepopulate": ("logs/meeting_prepopulate.log", 2.0, "poll 30m"),
}

# Jobs whose real cadence is enforced inside the script, not by launchd.
# meeting_prep.py polls every 5 minutes per its plist, but returns immediately
# outside weekday business hours and writes nothing -- so from Friday evening to
# Monday morning there is no log activity, and a plain "no run in 19h against a
# 5-minute cadence" check calls a perfectly healthy job stale every weekend.
# launchd cannot see a gate that lives in Python, so declare it here: the job's
# staleness window is widened by however long the gate has been shut.
# Value: (weekdays the gate allows, open time, close time).
# Both label forms appear in the wild: the repo plist declares
# com.obsidian.meeting-prep, while installs predating the namespacing carry
# com.meeting-prep. PIPELINE_NAMES/HINTS already carry the same pairs.
PIPELINE_GATED = {
    "com.obsidian.meeting-prep": ({0, 1, 2, 3, 4}, _dt.time(5, 30), _dt.time(19, 0)),
    "com.meeting-prep": ({0, 1, 2, 3, 4}, _dt.time(5, 30), _dt.time(19, 0)),
}


def _gate_closed_hours(gate: tuple, now: _dt.datetime) -> float:
    """Hours since this gate last allowed a run; 0.0 if it is open right now."""
    weekdays, start, end = gate
    if now.weekday() in weekdays and start <= now.time() <= end:
        return 0.0
    # Walk back to the most recent instant the gate was open. Today counts only
    # if we are already past its opening time.
    for back in range(0, 9):
        day = (now - _dt.timedelta(days=back)).date()
        if day.weekday() not in weekdays:
            continue
        closed_at = _dt.datetime.combine(day, end)
        if closed_at <= now:
            return max((now - closed_at).total_seconds() / 3600.0, 0.0)
    return 0.0
SCRIPTS_DIR = VAULT / "Templates" / "Scripts"

CADENCE_FLOOR_HOURS = 1.0    # even fast jobs get at least this much slack
DAILY_STALE_HOURS   = 28.0   # matches RAG_STALE_HOURS for once-a-day jobs


def _plist_load(path: Path) -> dict | None:
    """Parse a plist the way launchd does, or None if even it can't.

    Two parsers, strict first. plistlib is expat, and it rejects things
    launchd accepts: `--` inside an XML comment, and a DOCTYPE carrying a
    shell-style `\\` line continuation (Pulse Secure's installer writes one).
    An agent that loads and runs perfectly well would then vanish from this
    section, which is the same class of failure as the incident above -- a
    real state, reported as an absence. Falling back to `plutil -convert
    xml1` means this section sees what launchd sees.

    Both the fallback and the warning are reserved for a plist that would
    have been ours. ~/Library/LaunchAgents holds third-party agents too, and
    announcing that a VPN helper is "missing from pipeline health" is noise:
    it was never going to appear there. Skipping those also keeps this to at
    most one extra process per vault plist, not per agent on the machine.
    """
    import plistlib
    try:
        with path.open("rb") as fh:
            return plistlib.load(fh)
    except Exception as exc:
        # Bound to a second name deliberately: Python unbinds an `except ... as`
        # target at the end of its block, so `exc` is gone by the report below.
        strict_exc = f"{type(exc).__name__}: {exc}"

    # Decide whether this plist would have been ours BEFORE spending anything
    # else on it. Only a vault job can appear in this section, so a third-party
    # agent that expat dislikes needs neither a second parser nor a warning --
    # and this folder is shared with every installer on the machine. Matched
    # against the raw bytes because parsing is the thing that just failed.
    try:
        ours = PIPELINE_MARKER in path.read_text(encoding="utf-8",
                                                 errors="ignore")
    except OSError:
        return None
    if not ours:
        return None

    try:
        pr = subprocess.run(
            [PLUTIL_BIN, "-convert", "xml1", "-o", "-", str(path)],
            capture_output=True, timeout=10)
        if pr.returncode == 0:
            return plistlib.loads(pr.stdout)
        lenient_exc: str = (pr.stderr.decode(errors="ignore").strip()
                            or f"plutil exited {pr.returncode}")
    except Exception as exc:
        lenient_exc = f"{type(exc).__name__}: {exc}"

    print(f"[morning_dashboard] WARNING: unparseable plist "
          f"{path.name}: {strict_exc}; plutil also failed "
          f"({lenient_exc}) — job will be missing from pipeline "
          f"health", file=sys.stderr)
    return None


def _launchctl_status(label: str) -> dict:
    """Return {loaded, exit, running} for a launchd label via `launchctl list`.

    Output is captured as bytes and decoded with errors='ignore': system tools
    can emit non-UTF-8, and text=True would raise on it (a lesson learned the
    hard way on ioreg/pmset output elsewhere in this vault)."""
    try:
        p = subprocess.run([LAUNCHCTL_BIN, "list", label], capture_output=True)
    except Exception:
        return {"loaded": None, "exit": None, "running": False}
    if p.returncode != 0:
        # A non-zero return from `launchctl list <label>` means it isn't loaded.
        return {"loaded": False, "exit": None, "running": False}
    out = p.stdout.decode(errors="ignore")
    m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', out)
    exit_code = int(m.group(1)) if m else None
    running = bool(re.search(r'"PID"\s*=\s*\d+', out))
    return {"loaded": True, "exit": exit_code, "running": running}


# --- Verified diagnosis helpers ---------------------------------------------
#
# Everything below exists so this section stops guessing. The rule is that a
# cause is printed only after it has been checked; when nothing was checked,
# the dashboard says what it observed and stops. Silence costs less than a
# confident wrong answer, which sends someone at the wrong component and is
# harder to recover from than no answer at all (see the note above
# PIPELINE_HINTS for the morning that proved it).
#
# The reassuring part of that incident is that every signal needed to name the
# real cause was already sitting on disk: `launchctl print` had runs = 7152
# with last exit code = 0 -- a program returning immediately, over and over --
# and the tail of the stderr log said "gateway host api.ai.example.edu did not
# resolve" outright. Nothing had to be inferred. It just had to be read.

# How many clean restarts before "it ran again" reads as "it is crash-looping".
# A watcher that has been up since boot has runs = 1; a scheduled job earns one
# run per fire and can legitimately reach a few hundred over months. The
# signature that matters is a KeepAlive job with a large run count AND clean
# exits: launchd only respawns that fast when the program returns immediately.
CRASH_LOOP_RUNS = 20


def _launchctl_print(label: str) -> dict:
    """Restart/exit detail for a label via `launchctl print gui/<uid>/<label>`.

    `launchctl list` cannot answer "is this crash-looping?". It reports a last
    exit status and a PID, and a job respawning every ten seconds looks exactly
    like a healthy one caught between cycles. `print` carries the run counter,
    the state and the minimum runtime, which together are definitive.

    A field absent from the output comes back as None and must not be read as
    zero -- a job that has never run has no `last exit code` at all, which is a
    different fact from exiting with 0.

    Only top-level fields are parsed: `print` nests its own `state = active`
    lines under endpoints and event sources, so every pattern here anchors on a
    single leading tab."""
    blank = {"state": None, "runs": None, "exit": None, "min_runtime": None}
    try:
        pr = subprocess.run([LAUNCHCTL_BIN, "print", f"gui/{os.getuid()}/{label}"],
                            capture_output=True, timeout=10)
    except Exception:
        return blank
    if pr.returncode != 0:
        return blank
    out = pr.stdout.decode(errors="ignore")

    def field(pattern: str, cast):
        m = re.search(pattern, out, re.MULTILINE)
        if not m:
            return None
        try:
            return cast(m.group(1))
        except (TypeError, ValueError):
            return None

    return {
        "state":       field(r"^\tstate = (.+)$", lambda v: v.strip()),
        "runs":        field(r"^\truns = (\d+)$", int),
        "exit":        field(r"^\tlast exit code = (-?\d+)$", int),
        "min_runtime": field(r"^\tminimum runtime = (\d+)$", int),
    }


def _log_tail_lines(path: str, max_bytes: int = 65536) -> list[str]:
    """Non-empty lines from the end of a log, reading at most max_bytes.

    Bounded on purpose: a crash-looping job writes an enormous log -- the one
    that prompted this reached 19MB -- and the dashboard must not read all of
    it to find out what it says. The first line after a seek is dropped
    because it is almost certainly a partial one."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            data = fh.read()
    except OSError:
        return []
    return [ln.rstrip() for ln in data.decode(errors="ignore").splitlines()
            if ln.strip()]


# Lines shaped like an error report that carry no diagnosis: the traceback
# header names nothing, a frame names only a file, a caret line only a column.
# The final exception line below them is the part that says what went wrong.
_LOG_NOISE_RE = re.compile(
    r'^(?:Traceback \(most recent call last\):?|\s*File ".*", line \d+.*|\s*\^+\s*)$')


def last_error_line(path: str) -> str | None:
    """The most informative single line from the end of an error log, or None.

    A crash-looping job repeats one line thousands of times, so the last
    distinct line IS the diagnosis. In the case this was built for it read
    "gateway host api.ai.example.edu did not resolve" -- which points straight at
    the VPN, and which the dashboard was previously ignoring in favour of
    telling the operator to go read the log themselves."""
    for ln in reversed(_log_tail_lines(path)):
        if _LOG_NOISE_RE.match(ln):
            continue
        return ln.strip()[:300]
    return None


# Probing a venv costs a subprocess and a dozen jobs share one, so remember the
# verdict per interpreter for the life of the run.
_VENV_VERDICT: dict[str, str | None] = {}


def venv_defect(interpreter: str) -> str | None:
    """A CHECKED defect in the venv behind `interpreter`, or None if it works.

    Returns a sentence only for something actually observed: the interpreter
    does not exist, or it cannot import the SDK every LLM-backed vault script
    needs. "Two jobs share this path and both are unhealthy" is not evidence
    and never reaches this function -- that inference is precisely what aimed a
    morning's debugging at a perfectly healthy venv.

    A probe that fails for its own reasons (timeout, no permission to exec)
    also returns None. Not knowing is reported as not knowing."""
    if interpreter in _VENV_VERDICT:
        return _VENV_VERDICT[interpreter]
    verdict: str | None = None
    if not os.path.exists(interpreter):
        verdict = (f"Verified: the interpreter {interpreter} does not exist, so "
                   f"the job cannot start at all. Rebuild the venv with "
                   f"Homebrew python (3.10+), not /usr/bin/python3.")
    else:
        try:
            probe = subprocess.run([interpreter, "-c", "import anthropic"],
                                   capture_output=True, timeout=30)
            if probe.returncode != 0:
                verdict = (f"Verified: {interpreter} exists but cannot import "
                           f"anthropic, so the venv's packages are missing or "
                           f"broken. Reinstall from "
                           f"Templates/Scripts/requirements.txt.")
        except Exception:
            verdict = None
    _VENV_VERDICT[interpreter] = verdict
    return verdict


# Matches making a call, not merely knowing the module exists: a top-level
# import of the SDK, or a call to the endpoint module's client factory.
# Importing llm_endpoint alone does NOT count -- this very file imports it to
# ask whether a hostname resolves, and under a looser pattern reported itself
# as a job whose Claude calls were failing.
#
# It is a text scan, so it also matches a mention inside a comment or a
# docstring. That is a deliberate trade: the failure mode is naming one extra
# job in a banner that is already true, whereas a hand-maintained label list
# would go stale silently and put a wrong cause on a specific job. Keep prose
# in this file from spelling out either pattern verbatim -- an earlier draft
# of this very comment matched itself.
# A venv interpreter, by its path shape: <something>/[.]venv*/bin/python*.
# Matched on the path rather than by stat because the case that matters most
# is the interpreter having been deleted, when there is nothing left to stat.
# A job running /usr/bin/python3 must not match: probing the system
# interpreter for `anthropic` would report a defect that is not one.
_VENV_INTERPRETER_RE = re.compile(r"/\.?venv[^/]*/bin/python")

_LLM_CALL_RE = re.compile(
    r"^\s*(?:import|from)\s+anthropic\b|llm_endpoint\.client\s*\(",
    re.MULTILINE)
_LLM_SCRIPT_CACHE: dict[str, bool] = {}


def _script_uses_llm(script: str | None) -> bool:
    """True when this job's script actually routes through the LLM endpoint.

    Read out of the source rather than kept as a hand-maintained list of
    labels: a list goes stale the first time a script gains or loses an LLM
    call, and a stale list here would pin a wrong cause on a job -- the exact
    failure this whole section was rewritten to stop committing."""
    if not script:
        return False
    if script not in _LLM_SCRIPT_CACHE:
        try:
            src = Path(script).read_text(encoding="utf-8", errors="ignore")
            _LLM_SCRIPT_CACHE[script] = bool(_LLM_CALL_RE.search(src))
        except OSError:
            _LLM_SCRIPT_CACHE[script] = False
    return _LLM_SCRIPT_CACHE[script]


def _keepalive_diagnosis(label: str) -> str:
    """Why an always-on job is loaded but not running, from `launchctl print`.

    The old text here was "it may be crash-looping or throttled by launchd;
    check its error log" -- two guesses and a chore, when the run counter and
    exit code that settle it are one subprocess away."""
    pr = _launchctl_print(label)
    runs, code, state = pr["runs"], pr["exit"], pr["state"]
    if runs is not None and runs >= CRASH_LOOP_RUNS and code == 0:
        floor = pr["min_runtime"]
        pace = (f" launchd is respawning it at its {floor}s minimum-runtime "
                f"floor." if floor else "")
        return (f"Crash-looping: {runs} runs with last exit code 0 -- the "
                f"program starts and returns immediately instead of staying "
                f"up.{pace}")
    seen = []
    if state:
        seen.append(f"state {state}")
    if runs is not None:
        seen.append(f"{runs} runs")
    if code is not None:
        seen.append(f"last exit code {code}")
    detail = "; ".join(seen) if seen else "launchctl print returned no detail"
    return f"Always-on job is loaded but not running ({detail})."


def _scheduled_weekdays(plist: dict) -> set[int] | None:
    """Weekdays (Python: Mon=0 .. Sun=6) a StartCalendarInterval job is limited
    to, or None when it has no Weekday keys (runs every day).

    launchd numbers weekdays Sun=0/7, Mon=1 .. Sat=6; map those to Python's
    Mon=0 .. Sun=6 so the result compares with datetime.weekday()."""
    sci = plist.get("StartCalendarInterval")
    if sci is None:
        return None
    entries = sci if isinstance(sci, list) else [sci]
    wds: set[int] = set()
    for e in entries:
        if isinstance(e, dict) and "Weekday" in e:
            try:
                wds.add((int(e["Weekday"]) - 1) % 7)
            except (TypeError, ValueError):
                continue
    return wds or None


def _next_scheduled_gap_hours(sched: set[int] | None, from_weekday: int) -> float:
    """Hours from `from_weekday` forward to the next day in `sched` (wrapping
    through the week, always at least one day ahead -- never 0).

    Anchored on the weekday the job last actually ran (walking forward from
    there), not on "today" (walking backward from now). For a Mon-Fri job
    that last ran Monday, the next scheduled day is Tuesday: 24h. For a job
    scheduled only on Monday, the next scheduled day is next Monday: 168h --
    regardless of what day of the week it happens to be when this is
    checked.

    This replaced an earlier version that walked backward from "today" and
    stopped the instant it found a scheduled day. For a Monday-only job
    checked on a Tuesday, that immediately-preceding day *is* Monday, so it
    returned 0 extra slack -- mistaking "yesterday was a scheduled day" for
    "today should also produce a fresh run". That assumption holds for a
    Mon-Fri job (most days ARE scheduled) but is wrong for anything sparser:
    a once-a-week job isn't due again until next week, not tomorrow, and the
    dashboard flagged it stale every single day except the one it actually
    ran on."""
    if not sched:
        return 0.0
    d = from_weekday
    for step in range(1, 8):
        d = (d + 1) % 7
        if d in sched:
            return step * 24.0
    return 7 * 24.0  # unreachable: sched is always non-empty here


def _subdaily_max_gap_hours(plist: dict) -> float | None:
    """For a StartCalendarInterval that runs several times a day, the largest gap
    (hours) between consecutive runs. Entries with no Hour key fire every hour at
    their Minute, so e.g. Minutes {0, 30} -> a 30-minute cadence. Returns None
    when every entry pins an Hour (a once/few-times-daily schedule)."""
    sci = plist.get("StartCalendarInterval")
    if sci is None:
        return None
    entries = sci if isinstance(sci, list) else [sci]
    if all(isinstance(e, dict) and "Hour" in e for e in entries):
        return None                      # fixed-time daily schedule
    minutes = sorted({int(e.get("Minute", 0)) for e in entries
                      if isinstance(e, dict) and "Hour" not in e})
    if not minutes:
        return None
    if len(minutes) == 1:
        return 1.0                       # once per hour
    gaps = [minutes[i + 1] - minutes[i] for i in range(len(minutes) - 1)]
    gaps.append(minutes[0] + 60 - minutes[-1])   # wrap across the hour
    return max(gaps) / 60.0


def _cadence(plist: dict, now: _dt.datetime,
             last_run: _dt.datetime | None) -> tuple[str, float | None]:
    """Return (trigger_label, max_age_hours or None) from a plist's schedule.

    max_age_hours is None for event-driven (WatchPaths) or always-on
    (KeepAlive) jobs, which can't be judged stale by elapsed time alone.

    `last_run` anchors the weekday-restricted-schedule case (see
    _next_scheduled_gap_hours) -- falls back to `now`'s weekday if no run
    has been observed yet."""
    # WatchPaths first: a file-watch job's StartInterval is only a safety poll,
    # not its true cadence, and such jobs log only when there is actual input --
    # so elapsed time can't tell "idle" from "broken".
    if "WatchPaths" in plist:
        return ("on change", None)
    if "StartInterval" in plist:
        secs = float(plist["StartInterval"])
        hours = max(secs * 6.0 / 3600.0, CADENCE_FLOOR_HOURS)
        label = f"every {int(secs // 60)}m" if secs >= 60 else f"every {int(secs)}s"
        return (label, hours)
    if "StartCalendarInterval" in plist:
        # Sub-daily calendar schedule (e.g. every :00/:30) -> judge like an
        # interval job, not a once-a-day one.
        gap = _subdaily_max_gap_hours(plist)
        if gap is not None:
            mins = int(round(gap * 60))
            label = "hourly" if mins == 60 else f"every {mins}m"
            return (label, max(gap * 6.0, CADENCE_FLOOR_HOURS))
        # A weekday-restricted job (e.g. Mon-Fri, or a single weekly day)
        # legitimately skips the days it isn't scheduled on, so widen its
        # staleness window by the real gap to its next scheduled day --
        # otherwise a once-a-week job looks stale on six days out of seven.
        sched = _scheduled_weekdays(plist)
        anchor_weekday = (last_run.weekday() if last_run is not None
                         else now.weekday())
        slack = _next_scheduled_gap_hours(sched, anchor_weekday)
        if sched == {0, 1, 2, 3, 4}:
            label = "weekdays"
        elif sched:
            label = "scheduled days"
        else:
            label = "daily"
        return (label, DAILY_STALE_HOURS + slack)
    if plist.get("KeepAlive"):
        return ("always-on", None)
    return ("manual", None)


# --- Windows: pipeline health from Task Scheduler (\Obsidian\ tasks) ---------
# launchd/launchctl have no analog on Windows, so job health comes from Task
# Scheduler instead: per-task State + LastTaskResult + LastRunTime for tasks
# under the \Obsidian\ folder. Produces the same result shape as the macOS path.

WIN_PIPELINE_NAMES = {
    "tag-clippings":         "Tagger",
    "voice-cleanup":         "Voice cleanup",
    "strip-ads":             "Ad stripper",
    "meeting-prep":          "Meeting prep",
    "meeting-prepopulate":   "Meeting pre-populate",
    "group-photos":          "Group photos",
    "morning-dashboard":     "Morning dashboard",
    "security-plugin-check": "Plugin integrity",
    "security-integrity":    "File integrity monitor",
}
# rag-sync has its own detailed section above (mirrors PIPELINE_EXCLUDE).
WIN_PIPELINE_EXCLUDE = {"rag-sync"}

# Task Scheduler LastTaskResult sentinels that are NOT real failures.
_WIN_TASK_NOT_RUN = 267011   # 0x00041303 SCHED_S_TASK_HAS_NOT_RUN
_WIN_TASK_RUNNING = 267009   # 0x00041301 SCHED_S_TASK_RUNNING


def _win_cadence(repetition: str | None,
                 trigger_type: str | None) -> tuple[str, float | None]:
    """(trigger_label, max_age_hours) from a Task Scheduler trigger.

    `repetition` is an ISO-8601 duration (PT30M, PT5M, PT1H) for repeating
    triggers; `trigger_type` is the CIM class name (…DailyTrigger/…WeeklyTrigger).
    Mirrors _cadence(): the staleness window is 6x the cadence."""
    if repetition:
        m = re.match(r"P(?:T)?(?:(\d+)H)?(?:(\d+)M)?", repetition)
        if m and (m.group(1) or m.group(2)):
            secs = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
            if secs > 0:
                window = max(secs * 6.0 / 3600.0, CADENCE_FLOOR_HOURS)
                label = f"every {secs // 60}m" if secs >= 60 else f"every {secs}s"
                return (label, window)
    tt = (trigger_type or "").lower()
    if "weekly" in tt:
        return ("weekly", DAILY_STALE_HOURS)
    if "daily" in tt:
        return ("daily", DAILY_STALE_HOURS)
    return ("scheduled", DAILY_STALE_HOURS)


def _collect_pipeline_health_windows() -> list[dict]:
    """Job health from Windows Task Scheduler tasks under \\Obsidian\\."""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$o=foreach($t in Get-ScheduledTask -TaskPath '\\Obsidian\\'){"
        "$i=$t|Get-ScheduledTaskInfo;$rep=$null;"
        "foreach($g in $t.Triggers){if($g.Repetition -and $g.Repetition.Interval){$rep=$g.Repetition.Interval;break}}"
        "$tt=$null;if($t.Triggers){$tt=$t.Triggers[0].CimClass.CimClassName};"
        "[pscustomobject]@{name=$t.TaskName;state=\"$($t.State)\";"
        "lastRun=if($i.LastRunTime){$i.LastRunTime.ToString('s')}else{$null};"
        "lastResult=$i.LastTaskResult;repetition=$rep;triggerType=$tt}};"
        "$o|ConvertTo-Json -Depth 4 -Compress"
    )
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=30)
    except Exception:
        return []
    raw = p.stdout.decode("utf-8", "ignore").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]

    now_naive = now_local().replace(tzinfo=None)
    results: list[dict] = []
    for t in data:
        name = t.get("name") or ""
        if name in WIN_PIPELINE_EXCLUDE:
            continue
        # Disabled tasks are intentionally off (everything ships disabled), so
        # skip them rather than flagging a failure — like an uninstalled agent.
        if (t.get("state") or "").strip().lower() == "disabled":
            continue

        trigger, max_age = _win_cadence(t.get("repetition"), t.get("triggerType"))
        result = t.get("lastResult")

        last_run = None
        lr = t.get("lastRun")
        if lr and result != _WIN_TASK_NOT_RUN:
            try:
                last_run = _dt.datetime.fromisoformat(lr)
            except ValueError:
                last_run = None
            # Never-run tasks report a sentinel epoch date; treat as no run.
            if last_run is not None and last_run.year < 2000:
                last_run = None
        age_hours = ((now_naive - last_run).total_seconds() / 3600.0
                     if last_run else None)

        problems: list[str] = []
        status = "pass"
        if result not in (0, None, _WIN_TASK_NOT_RUN, _WIN_TASK_RUNNING):
            status = "fail"
            problems.append(
                f"Last run exited with code {result} (non-zero means it failed).")
        if max_age is not None and age_hours is not None and age_hours > max_age:
            if status == "pass":
                status = "stale"
            problems.append(
                f"No run in {age_hours:.0f}h (expected {trigger}); it may have stopped firing.")
        elif last_run is None and result == _WIN_TASK_NOT_RUN:
            problems.append("Has not run yet.")

        hint = PIPELINE_HINTS.get(f"com.{name}")
        if hint and status != "pass":
            problems.append(hint)

        fallback = name.replace("-", " ").strip().capitalize()
        results.append({
            "label": name,
            "name": WIN_PIPELINE_NAMES.get(name, fallback),
            "trigger": trigger,
            "status": status,
            "last_run": last_run,
            "age_hours": age_hours,
            "problems": problems,
        })

    order = {"fail": 0, "stale": 1, "pass": 2}
    results.sort(key=lambda r: (order.get(r["status"], 3), r["name"].lower()))
    return results


# ---------------------------------------------------------------------------
# Section 5b: LLM gateway reachability -- one cause behind many symptoms
# ---------------------------------------------------------------------------
#
# Nearly every LLM-backed job in this vault routes its Claude calls through
# LLM_BASE_URL (set in ~/dev/secrets/.env, resolved by llm_endpoint.py). When
# that host does not resolve -- the ordinary case being that the VPN is down --
# each of those jobs independently gives up and logs its own "Skipped" warning.
# The dashboard then reports N failures with N apparent causes, and whoever
# reads it goes looking for N bugs.
#
# Checking the gateway once, before any job is judged, collapses that cluster
# into the single fact that explains all of it. This is the same shape of fix
# as everything in Section 5: ask the cheap question that has a real answer
# instead of narrating a plausible one per job.

SECRETS_ENV = Path(os.environ.get(
    "MD_SECRETS_ENV", str(HOME / "dev" / "secrets" / ".env")))

# The endpoint config this check needs -- deliberately an allowlist. That file
# also holds credentials, and nothing here should pull a secret into the
# dashboard's environment just to learn whether a hostname resolves.
_ENDPOINT_ENV_KEYS = ("LLM_BASE_URL", "LLM_API_KEY_NAME", "LLM_SKIP_PREFLIGHT")


def _load_endpoint_env() -> None:
    """Copy the non-secret endpoint config out of ~/dev/secrets/.env.

    Parsed by hand rather than with python-dotenv because the dashboard runs
    under /usr/bin/python3 (see com.morning-dashboard.plist), which does not
    have it -- and a check that only works when run from the venv would be
    missing exactly when the venv is the thing in question. An existing
    environment value wins, matching load_dotenv's default."""
    try:
        text = SECRETS_ENV.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key in _ENDPOINT_ENV_KEYS and key not in os.environ:
            os.environ[key] = val.strip().strip('"').strip("'")


def collect_gateway_status() -> dict | None:
    """Reachability of the configured LLM gateway, or None when there is
    nothing worth saying.

    None covers three cases, all of which should stay off the dashboard:
      * No gateway configured. A stock Anthropic install that cannot resolve
        api.anthropic.com has no working internet, and answering that with
        "connect to the VPN" sends someone the wrong way -- llm_endpoint
        declines to preflight it for the same reason.
      * The preflight is stood down (LLM_SKIP_PREFLIGHT, or an egress proxy
        that does its own resolution). Local DNS predicts nothing there, so
        reporting either verdict would be a guess.
      * llm_endpoint is unavailable. Then we know nothing and say nothing.
    """
    _load_endpoint_env()
    try:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        import llm_endpoint
    except Exception:
        return None

    url = llm_endpoint.base_url()
    if not url:
        return None
    skip = getattr(llm_endpoint, "_skip_preflight", None)
    if callable(skip) and skip():
        return None

    host = urllib.parse.urlparse(url).hostname
    try:
        llm_endpoint.check_reachable(url)
    except llm_endpoint.GatewayUnreachable:
        return {"host": host, "url": url, "reachable": False}
    except Exception:
        return None
    return {"host": host, "url": url, "reachable": True}


def collect_pipeline_health(gateway: dict | None = None) -> list[dict]:
    """Discover vault jobs and classify each pass / stale / fail.

    `gateway` is collect_gateway_status()'s verdict, threaded in so an
    unreachable gateway is reported against the jobs it actually explains
    rather than inferred separately by each of them."""
    if sys.platform == "win32":
        return _collect_pipeline_health_windows()
    if not LAUNCHAGENTS_DIR.is_dir():
        return []
    now_naive = now_local().replace(tzinfo=None)
    results: list[dict] = []
    for plist_path in sorted(LAUNCHAGENTS_DIR.glob("*.plist")):
        plist = _plist_load(plist_path)
        if not plist:
            continue
        label = plist.get("Label") or plist_path.stem
        if label in PIPELINE_EXCLUDE:
            continue
        args = plist.get("ProgramArguments") or []
        if not any(PIPELINE_MARKER in str(a) for a in args):
            continue

        hb = PIPELINE_HEARTBEAT.get(label)
        last_run: _dt.datetime | None = None
        if hb:
            # Wrapper-launched job: use the worker's own log as the heartbeat,
            # since the launchd log path never updates.
            hb_rel, hb_stale, hb_trigger = hb
            trigger, max_age = hb_trigger, hb_stale
            try:
                last_run = _dt.datetime.fromtimestamp(
                    os.stat(SCRIPTS_DIR / hb_rel).st_mtime)
            except OSError:
                last_run = None
        else:
            for key in ("StandardOutPath", "StandardErrorPath"):
                lp = plist.get(key)
                if not lp:
                    continue
                try:
                    mt = _dt.datetime.fromtimestamp(os.stat(lp).st_mtime)
                except OSError:
                    continue
                if last_run is None or mt > last_run:
                    last_run = mt
            # last_run must be known before calling _cadence: a
            # weekday-restricted schedule anchors its staleness window on
            # the weekday the job last actually ran, not on today.
            trigger, max_age = _cadence(plist, now_naive, last_run)

        # A script-side gate (see PIPELINE_GATED) means silence is expected
        # while the gate is shut, so give the job back exactly that much time.
        gate = PIPELINE_GATED.get(label)
        if gate is not None:
            shut_for = _gate_closed_hours(gate, now_naive)
            if max_age is not None:
                max_age += shut_for
            trigger = f"{trigger}, gated" if shut_for else trigger

        age_hours = ((now_naive - last_run).total_seconds() / 3600.0
                     if last_run else None)

        st = _launchctl_status(label)
        is_keepalive = bool(plist.get("KeepAlive"))
        interpreter = str(args[0]) if args else ""
        script = next((str(a) for a in args
                       if PIPELINE_MARKER in str(a) and str(a).endswith(".py")),
                      None)
        err_log = plist.get("StandardErrorPath") or plist.get("StandardOutPath")

        problems: list[str] = []
        status = "pass"
        # Set when the failure is one the job has just been writing about, so
        # its log tail is current and worth quoting. A job that is not loaded
        # has not run, so its log describes some older life and is left alone.
        log_is_current = False
        if st["loaded"] is False:
            status = "fail"
            problems.append(
                "Not loaded in launchd -- the agent isn't installed or was unloaded.")
        elif is_keepalive:
            # An always-on watcher settles into a long-lived run, so its
            # LastExitStatus is stale once it's up. Judge it on whether it is
            # actually running now, not on the exit code of its last cycle.
            if not st["running"]:
                status = "fail"
                problems.append(_keepalive_diagnosis(label))
                log_is_current = True
        elif st["exit"] not in (None, 0):
            status = "fail"
            problems.append(
                f"Last run exited with status {st['exit']} (non-zero means it failed).")
            log_is_current = True
        if max_age is not None and age_hours is not None and age_hours > max_age:
            if status == "pass":
                status = "stale"
            problems.append(
                f"No run in {age_hours:.0f}h (expected {trigger}); it may have stopped firing.")
        elif max_age is not None and last_run is None and st["loaded"]:
            problems.append("No log activity found yet.")

        # --- Checked causes, most specific first --------------------------
        # Each of these is silent unless it has something real to report. A
        # failing job with no verified cause gets its observed symptoms and
        # the hint, which is the honest outcome -- see the note above
        # PIPELINE_HINTS for what happens when this section fills that gap
        # with a story instead.

        # The job's own last words. For the crash-loop this was built for, the
        # repeated line named the unreachable host outright and would have
        # pointed straight at the VPN.
        if log_is_current and err_log:
            line = last_error_line(err_log)
            if line:
                # "Last line", not "last error": several vault jobs send all
                # of their logging to stderr, so this is what the job last
                # said, which is not the same claim as it being the fault.
                problems.append(f"Last line of {err_log}: {line}")

        # Only ever raised about a venv that has been probed and found broken,
        # and only for a job that actually runs out of one.
        if status == "fail" and _VENV_INTERPRETER_RE.search(interpreter):
            defect = venv_defect(interpreter)
            if defect:
                problems.append(defect)

        # One unreachable gateway explains every LLM-backed job at once, so say
        # it on each of them rather than letting each invent its own reason.
        if (status != "pass" and gateway and not gateway["reachable"]
                and _script_uses_llm(script)):
            problems.append(
                f"LLM gateway {gateway['host']} is not resolving. This job "
                f"sends its Claude calls through it, so it will skip that work "
                f"until the gateway is reachable again -- usually meaning the "
                f"VPN is down.")

        hint = PIPELINE_HINTS.get(label)
        if hint and status != "pass":
            problems.append(hint)

        fallback_name = (label.replace("com.", "").replace("-", " ")
                              .replace(".", " ").strip().capitalize())
        results.append({
            "label": label,
            "name": PIPELINE_NAMES.get(label, fallback_name),
            "trigger": trigger,
            "status": status,
            "last_run": last_run,
            "age_hours": age_hours,
            "problems": problems,
        })

    order = {"fail": 0, "stale": 1, "pass": 2}
    results.sort(key=lambda r: (order.get(r["status"], 3), r["name"].lower()))
    return results


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: light dark;
  --bg:           #f5f6f8;
  --fg:           #1a1f2e;
  --muted:        #6b7280;
  --card-bg:      #ffffff;
  --card-border:  #e3e7ee;
  --shadow:       0 1px 2px rgba(15,23,42,0.04), 0 6px 18px rgba(15,23,42,0.05);

  --todo:         #dc2626;
  --todo-soft:    #fee2e2;
  --meet:         #0e8a6a;
  --meet-soft:    #e1f5ec;
  --new:          #c2410c;
  --new-soft:     #fdece1;

  /* All "new today" pills share the orange palette -- the kind labels
     stay distinct (creation/article/video) but visually unified. */
  --tag-creation: var(--new);
  --tag-creation-soft: var(--new-soft);
  --tag-article:  var(--new);
  --tag-article-soft: var(--new-soft);
  --tag-video:    var(--new);
  --tag-video-soft: var(--new-soft);

  --accent: var(--todo);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:          #0f1218;
    --fg:          #ecedf0;
    --muted:       #9aa0a6;
    --card-bg:     #1a1f2a;
    --card-border: #2a3140;
    --shadow:      0 1px 2px rgba(0,0,0,0.5), 0 8px 20px rgba(0,0,0,0.35);

    --todo:        #ff8585;
    --todo-soft:   #3a1a1c;
    --meet:        #5ed3a8;
    --meet-soft:   #14302a;
    --new:         #ffa775;
    --new-soft:    #3a2117;

    --tag-creation: var(--new);
    --tag-creation-soft: var(--new-soft);
    --tag-article:  var(--new);
    --tag-article-soft: var(--new-soft);
    --tag-video:    var(--new);
    --tag-video-soft: var(--new-soft);
  }
}
* { box-sizing: border-box; }
html, body { background: var(--bg); color: var(--fg); margin: 0; }
body {
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
  padding: 28px 36px 64px;
  max-width: 1400px;
  margin: 0 auto;
}
header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 24px; }
h1 { font-size: 28px; margin: 0; font-weight: 700; letter-spacing: -0.02em;
     background: linear-gradient(120deg, var(--todo) 0%, var(--meet) 55%, var(--new) 100%);
     -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
.subtitle { color: var(--muted); font-size: 13px; }

/* Action buttons -- obsidian-dashboard://run/<action> links, handled by
   DashboardActions.app (see build_dashboard_actions_app.sh). A static
   file:// page can't run local commands from a click any other way. */
.actions { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 22px; }
.actions a {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border-radius: 999px;
  border: 1px solid var(--card-border);
  background: var(--card-bg);
  color: var(--fg);
  font-size: 12.5px;
  font-weight: 600;
  text-decoration: none;
  box-shadow: var(--shadow);
}
.actions a:hover { border-color: var(--accent); color: var(--accent); }

/* 2/3 left + 1/3 right. Collapses to one column under 900px. */
.grid {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 24px;
  align-items: start;
}
@media (max-width: 900px) {
  .grid { grid-template-columns: 1fr; }
}
.right-col { display: flex; flex-direction: column; }
.left-col { display: flex; flex-direction: column; gap: 24px; }

.card {
  background: var(--card-bg);
  border: 1px solid var(--card-border);
  border-radius: 14px;
  padding: 20px 22px 22px;
  box-shadow: var(--shadow);
  position: relative;
  overflow: hidden;
}
/* Colored stripe across the top of each card -- the section's accent. */
.card::before {
  content: "";
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  background: var(--section-accent, var(--todo));
}
.card h2 {
  font-size: 17px;
  margin: 0 0 4px;
  font-weight: 700;
  color: var(--section-accent, var(--fg));
  letter-spacing: -0.005em;
}
.card .meta { color: var(--muted); font-size: 12px; margin-bottom: 14px; }
.card .meta a { color: var(--section-accent, var(--accent)); text-decoration: none; }
.card .meta a:hover { text-decoration: underline; }

/* Right column: combined card with internal divider between sub-sections. */
.combined .subsection { padding: 0; }
.combined .subsection + .subsection {
  margin-top: 18px;
  padding-top: 18px;
  border-top: 1px dashed var(--card-border);
}
.combined .subsection h2 { margin-top: 0; }

/* To-do list (left card). */
.section-todos { --section-accent: var(--todo); }
ul.items { list-style: none; padding: 0; margin: 0; columns: 2; column-gap: 28px; }
@media (max-width: 1100px) { ul.items { columns: 1; } }
ul.items li {
  padding: 5px 0 5px 22px;
  position: relative;
  font-size: 14px;
  break-inside: avoid;
}
ul.items li::before {
  content: "";
  position: absolute;
  left: 2px; top: 9px;
  width: 12px; height: 12px;
  border: 1.5px solid var(--todo);
  border-radius: 3px;
  background: var(--todo-soft);
}
ul.items li a { color: var(--fg); text-decoration: none; }
ul.items li a:hover { color: var(--todo); }

/* Meetings sub-section. */
.section-meetings { --section-accent: var(--meet); }
.meeting {
  display: flex;
  flex-direction: column;
  padding: 9px 12px;
  margin-bottom: 6px;
  border-radius: 8px;
  background: var(--meet-soft);
  border-left: 3px solid var(--meet);
}
.meeting:last-child { margin-bottom: 0; }
.meeting a { color: var(--fg); text-decoration: none; font-weight: 600; font-size: 14px; }
.meeting a:hover { color: var(--meet); }
.meeting .time { color: var(--meet); font-weight: 700; margin-right: 6px; }

/* New today sub-section. */
.section-new { --section-accent: var(--new); }
.clip { padding: 8px 0; border-bottom: 1px solid var(--card-border); }
.clip:last-child { border-bottom: none; padding-bottom: 0; }
.clip:first-child { padding-top: 0; }
.clip a { color: var(--fg); text-decoration: none; font-weight: 500; font-size: 14px; }
.clip a:hover { color: var(--new); }
.clip .src { font-size: 11px; color: var(--muted); margin-top: 3px; }

/* Tag pills, colored by kind. */
.tag {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 10px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin-right: 7px;
  font-weight: 700;
  vertical-align: 1px;
}
.tag.creation { background: var(--tag-creation-soft); color: var(--tag-creation); }
.tag.article  { background: var(--tag-article-soft);  color: var(--tag-article);  }
.tag.video    { background: var(--tag-video-soft);    color: var(--tag-video);    }
.tag.note     { background: var(--card-border);       color: var(--muted);        }

.empty { color: var(--muted); font-style: italic; font-size: 13px; }
footer { color: var(--muted); font-size: 11px; margin-top: 32px; text-align: right; }

/* RAG sync health sub-section (top of right column). */
.section-rag { --section-accent: var(--meet); }
.rag-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.rag-when { color: var(--muted); font-size: 12px; }
.status-pill {
  display: inline-block;
  padding: 2px 9px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.status-pill.pass { background: var(--meet-soft); color: var(--meet); }
.status-pill.warn { background: var(--new-soft);  color: var(--new);  }
.status-pill.fail { background: var(--todo-soft); color: var(--todo); }
.rag-alert {
  margin-top: 8px;
  padding: 9px 11px;
  border: 1px solid var(--todo);
  border-radius: 8px;
  background: var(--todo-soft);
  font-size: 12.5px;
  line-height: 1.4;
}
.rag-alert div + div { margin-top: 5px; }

/* Full-width banner above the grid, for a single fact that explains a whole
   column of failures below it (currently: the LLM gateway being unreachable).
   Deliberately louder than a pipe-row alert -- it is the thing to read first. */
.banner {
  margin-bottom: 20px;
  padding: 11px 14px;
  border: 1px solid var(--todo);
  border-left: 4px solid var(--todo);
  border-radius: 8px;
  background: var(--todo-soft);
  font-size: 13.5px;
  line-height: 1.45;
}

/* Pipeline health sub-section (top of right column). */
.section-pipes { --section-accent: var(--meet); }
.pipe-row { display: flex; align-items: center; gap: 8px; padding: 5px 0; }
.pipe-name { font-weight: 600; font-size: 13.5px; }
.pipe-when { color: var(--muted); font-size: 11.5px; margin-left: auto; text-align: right; }
.pipe-row + .rag-alert { margin-top: 4px; margin-bottom: 6px; }
"""


def dashboard_actions_available() -> bool:
    """True when the obsidian-dashboard:// URL-scheme handler is installed.

    The action buttons are links to a custom URL scheme; without the
    handler app (built by build_dashboard_actions_app.sh / installer
    component 57-dashboard-actions) they are dead clicks, so the bar is
    simply not rendered. macOS-only — no Windows handler exists yet."""
    if sys.platform != "darwin":
        return False
    candidates = (
        Path.home() / "Applications" / "DashboardActions.app",
        Path("/Applications/DashboardActions.app"),
        Path(__file__).resolve().parent / "DashboardActions.app",
    )
    return any(p.exists() for p in candidates)


def collect_llm_usage() -> list[dict]:
    """Last 7 days of per-pipeline token usage, via usage_log. Empty list
    when the log is absent (fresh install) or unreadable — the section
    renders as 'no usage recorded', never breaks the dashboard."""
    try:
        import usage_log
        return usage_log.summarize(days=7)
    except Exception:
        return []


def collect_claude_code_usage() -> dict | None:
    """30-day Claude Code usage from this machine's local transcripts
    (usage_log.summarize_claude_code). None hides the section — no Claude
    Code on this box, or nothing in the window."""
    try:
        import usage_log
        return usage_log.summarize_claude_code(days=30)
    except Exception:
        return None


def render(today: _dt.date,
           todos: list[tuple[Path, str]],
           meetings: list[Path],
           new_today: list[tuple[Path, _dt.datetime, dict]],
           rag: dict | None,
           health: list[dict],
           gateway: dict | None = None,
           usage: list[dict] | None = None,
           cc_usage: dict | None = None,
           show_actions: bool | None = None) -> str:
    now = now_local()
    pretty_date = today.strftime("%A, %B %d, %Y")
    tz_label = now.strftime("%Z") or "ET"

    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Morning Dashboard — {today.isoformat()}</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <div>
    <h1>Morning Dashboard</h1>
    <div class="subtitle">{html.escape(pretty_date)}</div>
  </div>
  <div class="subtitle">Generated {now.strftime('%H:%M')} {html.escape(tz_label)}</div>
</header>
""")

    # Above everything, because it is the one fact that reinterprets the rest
    # of the page: with the gateway down, every LLM-backed job below is
    # skipping rather than broken, and each of their individual complaints is
    # a symptom of this line.
    if gateway and not gateway["reachable"]:
        parts.append(
            '<div class="banner">\n'
            f'  <strong>LLM gateway unreachable</strong> — '
            f'{html.escape(gateway["host"] or gateway["url"])} is not '
            'resolving, so every LLM-backed vault job will skip its work and '
            'log a warning. Usual cause: the VPN is down.\n'
            '</div>\n')

    # Action buttons only render when the obsidian-dashboard:// handler is
    # installed — otherwise they'd be dead clicks (see
    # dashboard_actions_available for what installs the handler).
    if show_actions is None:
        show_actions = dashboard_actions_available()
    if show_actions:
        parts.append("""<div class="actions">
  <a href="obsidian-dashboard://run/pull-meetings">Pull meetings</a>
  <a href="obsidian-dashboard://run/rebaseline-security">Rebaseline security harness</a>
  <a href="obsidian-dashboard://run/refresh-dashboard">Refresh dashboard</a>
  <a href="obsidian-dashboard://run/refresh-rag">Refresh RAG index</a>
</div>
""")
    parts.append('<div class="grid">\n')

    # ===== LEFT COLUMN: Open to-dos + Today's meetings (2/3 width) =====
    parts.append('<div class="left-col">\n')
    parts.append('<section class="card section-todos">\n')
    parts.append('  <h2>Open to-dos</h2>\n')
    todo_link = html.escape(obsidian_uri(Path("Actions/To-Do.md")))
    parts.append(f'  <div class="meta">{len(todos)} open · <a href="{todo_link}">Actions/To-Do</a></div>\n')
    if not todos:
        parts.append('  <p class="empty">No open <code>- [ ]</code> items.</p>\n')
    else:
        parts.append('  <ul class="items">\n')
        for src, text in todos:
            uri = html.escape(obsidian_uri(src))
            parts.append(
                f'    <li><a href="{uri}">{html.escape(text)}</a></li>\n'
            )
        parts.append('  </ul>\n')
    parts.append('</section>\n')

    # ----- Today's meetings card (under the to-dos) -----
    parts.append('<section class="card section-meetings">\n')
    parts.append('  <h2>Today\'s meetings</h2>\n')
    parts.append(f'  <div class="meta">{len(meetings)} scheduled</div>\n')
    if not meetings:
        parts.append('  <p class="empty">No meeting notes for today.</p>\n')
    else:
        for rel in meetings:
            uri      = obsidian_uri(rel)
            time_lbl = meeting_time_label(rel)
            label    = meeting_label(rel)
            parts.append('  <div class="meeting">\n')
            parts.append(f'    <a href="{html.escape(uri)}">')
            if time_lbl:
                parts.append(f'<span class="time">{html.escape(time_lbl)}</span>')
            parts.append(f'{html.escape(label)}</a>\n')
            parts.append('  </div>\n')
    parts.append('</section>\n')
    parts.append('</div>\n')  # close .left-col

    # ===== RIGHT COLUMN: Pipeline health + RAG sync + New today =====
    parts.append('<div class="right-col">\n')
    parts.append('  <section class="card combined">\n')

    # ----- Pipeline health sub-section (very top: a dead job must be unmissable) -----
    n_bad = sum(1 for p in health if p["status"] != "pass")
    pipes_accent = "var(--todo)" if n_bad else "var(--meet)"
    parts.append(
        f'    <div class="subsection section-pipes" style="--section-accent: {pipes_accent}">\n')
    parts.append('      <h2>Pipeline health</h2>\n')
    if not health:
        parts.append('      <p class="empty">No vault pipelines discovered.</p>\n')
    else:
        summary = f'{len(health)} monitored'
        summary += (f' · <strong>{n_bad} need attention</strong>' if n_bad
                    else ' · all healthy')
        parts.append(f'      <div class="meta">{summary}</div>\n')
        for p in health:
            pill = {"pass": "pass", "stale": "warn", "fail": "fail"}[p["status"]]
            when = f'{p["last_run"]:%a %H:%M}' if p["last_run"] else "no runs"
            parts.append('      <div class="pipe-row">\n')
            parts.append(f'        <span class="status-pill {pill}">{html.escape(p["status"])}</span>\n')
            parts.append(f'        <span class="pipe-name">{html.escape(p["name"])}</span>\n')
            parts.append(
                f'        <span class="pipe-when">{html.escape(when)} · {html.escape(p["trigger"])}</span>\n')
            parts.append('      </div>\n')
            if p["problems"]:
                parts.append('      <div class="rag-alert">\n')
                for pr in p["problems"]:
                    parts.append(f'        <div>{html.escape(pr)}</div>\n')
                parts.append('      </div>\n')
    parts.append('    </div>\n')

    # ----- RAG sync health sub-section -----
    if rag is None:
        parts.append('    <div class="subsection section-rag">\n')
        parts.append('      <h2>RAG sync</h2>\n')
        parts.append('      <p class="empty">No sync reports found.</p>\n')
        parts.append('    </div>\n')
    else:
        status = rag["status"]
        # Stale overrides status colour: a job that hasn't run can't be "green",
        # even if its last recorded run passed.
        if rag["stale"] or status in ("FAIL", "ABORT"):
            pill = "fail"
        elif status == "PASS":
            pill = "pass"
        else:
            pill = "warn"
        accent = {"pass": "var(--meet)", "warn": "var(--new)", "fail": "var(--todo)"}[pill]
        run_lbl = rag["run_dt"].strftime("%a %H:%M")
        report_uri = html.escape(obsidian_uri(rag["rel"]))
        parts.append(
            f'    <div class="subsection section-rag" style="--section-accent: {accent}">\n'
        )
        parts.append('      <h2>RAG sync</h2>\n')
        parts.append('      <div class="rag-head">\n')
        parts.append(
            f'        <span class="status-pill {pill}">{html.escape(status)}</span>\n'
        )
        parts.append(f'        <span class="rag-when">last run {html.escape(run_lbl)}</span>\n')
        parts.append('      </div>\n')
        parts.append(
            f'      <div class="meta">{html.escape(str(rag["errors"]))} errors · '
            f'{html.escape(str(rag["quarantined"]))} quarantined · '
            f'<a href="{report_uri}">report</a></div>\n'
        )
        problems: list[str] = []
        if rag["stale"]:
            problems.append(
                f'No sync in {rag["age_hours"]:.0f}h — the nightly job may not be running.'
            )
        if status in ("FAIL", "ABORT"):
            problems.append(
                'Last run did not complete cleanly. A 401 here usually means the Open '
                'WebUI API key was rejected — re-mint it and update OPEN_WEBUI_API_KEY '
                'in dev/secrets/.env.'
            )
        if problems:
            parts.append('      <div class="rag-alert">\n')
            for pr in problems:
                parts.append(f'        <div>{html.escape(pr)}</div>\n')
            parts.append('      </div>\n')
        parts.append('    </div>\n')

    # ----- LLM usage sub-section -----
    # Relative cost per pipeline is the actionable number; the $ figure is a
    # list-price estimate (gateway installs pay their contract rate instead).
    parts.append('    <div class="subsection section-usage">\n')
    parts.append('      <h2>LLM usage · 7 days</h2>\n')
    if not usage:
        parts.append('      <p class="empty">No usage recorded yet.</p>\n')
    else:
        total_usd = sum(b["est_usd"] for b in usage)
        total_calls = sum(b["calls"] for b in usage)
        total_billable = sum(b["billable_tokens"] for b in usage)
        parts.append(
            f'      <div class="meta">{total_calls} calls · '
            f'{total_billable:,} billable tok · '
            f'~${total_usd:.2f} at list price (estimate)</div>\n')
        for b in usage:
            cache_note = ""
            if b["cache_read_input_tokens"]:
                cache_note = f' · {b["cache_read_input_tokens"]:,} cached-read'
            parts.append('      <div class="pipe-row">\n')
            parts.append(f'        <span class="pipe-name">{html.escape(b["pipeline"])}'
                         f' <span class="pipe-when">({html.escape(b["model"])})</span></span>\n')
            parts.append(f'        <span class="pipe-when">'
                         f'{b["billable_tokens"]:,} billable'
                         f'{html.escape(cache_note)} · ~${b["est_usd"]:.2f}</span>\n')
            parts.append('      </div>\n')

    # Claude Code (this machine, 30 days) — a different meter than the
    # pipelines above: local session transcripts, not the gateway scripts.
    # On a seat plan the $ figure is a list-price estimate, not a bill.
    #
    # Billable tokens lead; raw throughput is the secondary line. The raw total
    # runs ~35x the billable one because every turn re-reads the whole cached
    # context, so it tracks session length rather than work done — a headline
    # number in the billions reads as alarming and optimizes nothing.
    if cc_usage:
        parts.append('      <h2 style="margin-top:14px">Claude Code · 30 days (this machine)</h2>\n')
        parts.append(
            f'      <div class="meta">{cc_usage["sessions"]} sessions · '
            f'{cc_usage["calls"]:,} responses · '
            f'{cc_usage["billable_tokens"]:,} billable tok '
            f'(input + output + cache writes) · '
            f'~${cc_usage["est_usd"]:,.0f} at list price (estimate; '
            f'seat plans are not billed per token)</div>\n')
        cc_total = cc_usage["total_tokens"]
        read_share = (cc_usage["cache_read_input_tokens"] / cc_total
                      if cc_total else 0.0)
        cost_share = (cc_usage["cache_read_usd"] / cc_usage["est_usd"]
                      if cc_usage["est_usd"] else 0.0)
        parts.append(
            f'      <div class="meta">context throughput {cc_total:,} tok · '
            f'{read_share:.0%} of it cache re-reads, priced at 0.1x input '
            f'({cost_share:.0%} of the estimate)</div>\n')
        for b in cc_usage["by_model"][:4]:
            parts.append('      <div class="pipe-row">\n')
            parts.append(f'        <span class="pipe-name">{html.escape(b["model"])}</span>\n')
            parts.append(f'        <span class="pipe-when">'
                         f'{b["billable_tokens"]:,} billable · '
                         f'{b["total_tokens"]:,} tok · '
                         f'~${b["est_usd"]:,.2f}</span>\n')
            parts.append('      </div>\n')
    parts.append('    </div>\n')

    # ----- New today sub-section -----
    parts.append('    <div class="subsection section-new">\n')
    parts.append('      <h2>New today</h2>\n')
    parts.append(f'      <div class="meta">{len(new_today)} item{"s" if len(new_today)!=1 else ""} since midnight</div>\n')
    if not new_today:
        parts.append('      <p class="empty">Nothing new yet today.</p>\n')
    else:
        for rel, bt, info in new_today:
            if rel.suffix.lower() == ".md":
                uri = obsidian_uri(rel)
            else:
                uri = "file://" + urllib.parse.quote(str(VAULT / rel))
            kind = info["kind"]
            time_label = bt.strftime("%H:%M")
            title = html.escape(info["title"])
            parts.append('      <div class="clip">\n')
            parts.append(f'        <span class="tag {kind}">{kind}</span>\n')
            parts.append(f'        <a href="{html.escape(uri)}">{title}</a>\n')
            src = info["source"]
            line_bits = [time_label]
            if src:
                try:
                    host = urllib.parse.urlparse(src).netloc
                    if host.startswith("www."):
                        host = host[4:]
                    if host:
                        line_bits.append(host)
                except Exception:
                    pass
            elif kind == "creation":
                if len(rel.parts) > 1:
                    line_bits.append("/".join(rel.parts[:-1]))
            parts.append(f'        <div class="src">{html.escape(" · ".join(line_bits))}</div>\n')
            parts.append('      </div>\n')
    parts.append('    </div>\n')

    parts.append('  </section>\n')
    parts.append('</div>\n')

    parts.append('</div>\n')  # close .grid
    parts.append(f'<footer>morning_dashboard.py · {today.isoformat()}</footer>\n')
    parts.append('</body></html>\n')
    return "".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    today = today_local()

    DASHBOARDS_DIR.mkdir(parents=True, exist_ok=True)

    todos     = collect_todos()
    meetings  = collect_meetings(today)
    new_today = collect_new_today(today)
    rag       = collect_rag_sync_status()
    # Before pipeline health, which uses the verdict: an unreachable gateway
    # is the cause of every LLM-backed job's failure, not a separate finding.
    gateway   = collect_gateway_status()
    health    = collect_pipeline_health(gateway=gateway)
    usage     = collect_llm_usage()
    cc_usage  = collect_claude_code_usage()

    html_text = render(today, todos, meetings, new_today, rag, health,
                       gateway=gateway, usage=usage, cc_usage=cc_usage)

    dated_path  = DASHBOARDS_DIR / f"morning-{today.isoformat()}.html"
    stable_path = DASHBOARDS_DIR / "morning.html"
    dated_path.write_text(html_text, encoding="utf-8")
    stable_path.write_text(html_text, encoding="utf-8")

    # Open the dashboard in the default browser (Chrome on macOS).
    # Pass --no-open to skip (useful for the sample-review step).
    if "--no-open" not in sys.argv:
        try:
            if sys.platform == "darwin":
                # dated_path's name is stable across same-day re-runs
                # (morning-YYYY-MM-DD.html), so `open` on the bare path
                # re-focuses whatever tab already has that exact URL rather
                # than loading the just-rewritten content -- macOS/Chrome
                # treat "open this file again" as "show me that tab", not
                # "reload it". A cache-busting query param makes every run's
                # URL distinct, forcing a real (re)load each time.
                cache_bust = int(_dt.datetime.now().timestamp())
                url = f"file://{urllib.parse.quote(str(dated_path))}?_={cache_bust}"
                subprocess.run(
                    ["/usr/bin/open", "-a", "Google Chrome", url],
                    check=False,
                )
            elif sys.platform == "win32":
                os.startfile(str(dated_path))  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", str(dated_path)], check=False)
        except Exception as e:
            print(f"warn: could not open dashboard: {e}", file=sys.stderr)

    print(f"wrote {dated_path}")
    print(f"  todos:     {len(todos)} open")
    print(f"  meetings:  {len(meetings)}")
    print(f"  new today: {len(new_today)} (Clippings + Creations since midnight)")
    if rag is not None:
        print(f"  rag sync:  {rag['status']} (last run {rag['run_dt']:%Y-%m-%d %H:%M}, "
              f"{rag['errors']} errors)")
    else:
        print("  rag sync:  no reports found")
    if gateway is not None:
        print(f"  gateway:   {gateway['host']} "
              + ("reachable" if gateway["reachable"]
                 else "UNREACHABLE -- LLM jobs will skip"))
    bad = [p for p in health if p["status"] != "pass"]
    print(f"  pipelines: {len(health)} monitored, {len(bad)} need attention"
          + (": " + ", ".join(f"{p['name']}={p['status']}" for p in bad) if bad else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
