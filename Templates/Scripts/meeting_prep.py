#!/usr/bin/env python3
"""meeting_prep.py — Insert/refresh open follow-up tasks in today's meeting notes.

Two passes, both run by a single LaunchAgent polling every 5 minutes during
business hours:

  1. MORNING pass: the first time the script sees a today-dated INDIVIDUAL meeting
     note that lacks an auto-block, it scans the vault for open `- [ ] ... [[Person]]`
     follow-ups and inserts a `> [!todo]+` callout above the user's agenda.

  2. PRE-MEETING pass: 20-30 minutes before each individual meeting starts, the
     script does ONE refresh — re-scanning the vault and replacing the morning
     block. This catches follow-ups created earlier on the same day.

Group meetings (anything where `type` in frontmatter is not exactly `Individual`)
are skipped entirely.

The auto-block carries an internal HTML-comment marker indicating which pass
generated it, which is how the pre-meeting pass knows whether it already ran.

See ~/Obsidian/Knowledge/Meeting Prep Auto-Insert Pipeline.md for manual-run
examples, security properties, and full configuration reference.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

VAULT = Path(os.path.expanduser("~/Obsidian"))
MEETINGS = VAULT / "Meetings"
EXCLUDE_DIRS = {".obsidian", ".trash", "Z_archive", "Z_attachments"}

BEGIN_MARK = "<!-- BEGIN: AUTO-INSERTED OPEN FOLLOW-UPS -->"
END_MARK = "<!-- END: AUTO-INSERTED OPEN FOLLOW-UPS -->"
GEN_MARK_RE = re.compile(r"<!--\s*generated:\s*([^|]+)\s*\|\s*mode:\s*(\w+)\s*-->")

# Tolerates leading whitespace (indented YAML lists) and either single, double,
# or no quotes around the wikilink. Obsidian's Properties editor produces
# 2-space-indented double-quoted entries; older notes used unindented single
# quotes. We accept both.
PERSON_LINE_RE = re.compile(r"""^[ \t]*-[ \t]+['"]?\[\[([^\]]+)\]\]['"]?\s*$""")
OPEN_TASK_RE = re.compile(r"^\s*- \[ \].*$")
TYPE_RE = re.compile(r"(?m)^type\s*:\s*(\S+)\s*$")
FILENAME_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2})(\d{2})\b")

# Business-hour gate (local time, weekdays only).
BUSINESS_START = dt.time(5, 30)
BUSINESS_END = dt.time(19, 0)
PRE_MEETING_LEAD_MIN = 25
PRE_MEETING_WINDOW = 5  # +/- 5 minutes => fires once during 20..30 min pre-start


def now_local() -> dt.datetime:
    return dt.datetime.now()


def today_str() -> str:
    return now_local().date().strftime("%Y-%m-%d")


def in_business_hours(now: dt.datetime) -> bool:
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False
    t = now.time()
    return BUSINESS_START <= t <= BUSINESS_END


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block_with_fences, body_no_leading_newline)."""
    if not text.startswith("---\n"):
        return "", text
    end_idx = text.find("\n---", 4)
    if end_idx == -1:
        return "", text
    return text[: end_idx + 4], text[end_idx + 4 :].lstrip("\n")


def parse_people(fm: str) -> list[str]:
    people: list[str] = []
    # Allow indented or unindented bullets after `people:`. This matters because
    # Obsidian's native Properties editor emits 2-space-indented lists.
    m = re.search(r"(?ms)^people\s*:\s*\n((?:^[ \t]*-[ \t]+.*\n?)+)", fm)
    if m:
        for line in m.group(1).splitlines():
            pm = PERSON_LINE_RE.match(line)
            if pm:
                people.append(pm.group(1))
    return people


def parse_type(fm: str) -> str | None:
    m = TYPE_RE.search(fm)
    return m.group(1) if m else None


def parse_aliases(fm: str) -> list[str]:
    """Return alias strings declared in YAML frontmatter.

    Supports three YAML shapes:
        aliases: [Foo, Bar]
        aliases:
          - Foo
          - Bar
        aliases: Foo
    """
    # block style (allow indented or unindented bullets)
    m = re.search(r"(?ms)^aliases\s*:\s*\n((?:^[ \t]*-[ \t]+.*\n?)+)", fm)
    if m:
        out = []
        for line in m.group(1).splitlines():
            ln = line.strip()
            if ln.startswith("- "):
                v = ln[2:].strip().strip("'").strip('"')
                if v:
                    out.append(v)
        return out
    # inline flow style or scalar
    m = re.search(r"(?m)^aliases\s*:\s*(.+)$", fm)
    if m:
        raw = m.group(1).strip()
        if raw.startswith("[") and raw.endswith("]"):
            return [
                v.strip().strip("'").strip('"')
                for v in raw[1:-1].split(",")
                if v.strip()
            ]
        return [raw.strip().strip("'").strip('"')]
    return []


def parse_preferred_name(fm: str) -> str | None:
    """Return the preferred_name string from frontmatter, or None.

    Honors the convention added 2026-05-15: People notes may carry a
    `preferred_name:` field for the name the person likes to be called
    (e.g., `preferred_name: Kate` on the canonical `Kowalski, Katherine.md`).
    """
    m = re.search(r"(?m)^preferred_name\s*:\s*(.+)$", fm)
    if not m:
        return None
    v = m.group(1).strip().strip("'").strip('"')
    return v or None


def get_person_aliases(person: str) -> set[str]:
    """Return {person} U aliases U {preferred_name} declared in People/<person>.md.

    Falls back gracefully when the People note is missing.
    """
    aliases = {person}
    people_file = VAULT / "People" / f"{person}.md"
    if not people_file.exists():
        return aliases
    try:
        text = people_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return aliases
    fm, _ = split_frontmatter(text)
    for alias in parse_aliases(fm):
        aliases.add(alias)
    pref = parse_preferred_name(fm)
    if pref:
        aliases.add(pref)
    return aliases


def parse_meeting_start(path: Path) -> dt.datetime | None:
    m = FILENAME_TIME_RE.match(path.name)
    if not m:
        return None
    date_s, hh, mm = m.groups()
    try:
        return dt.datetime.strptime(f"{date_s} {hh}{mm}", "%Y-%m-%d %H%M")
    except ValueError:
        return None


def find_open_tasks_for(person: str, skip_path: Path) -> list[tuple[str, str]]:
    """Find open `- [ ]` tasks whose line contains [[person]] or any People alias.

    Aliases are pulled from `People/<person>.md` frontmatter (`aliases:` field).
    Each match is returned at most once, even if multiple needles hit the same line.
    """
    needles = [f"[[{a}]]" for a in get_person_aliases(person)]
    results: list[tuple[str, str]] = []
    for root, dirs, files in os.walk(VAULT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for fname in files:
            if not fname.endswith(".md"):
                continue
            fp = Path(root) / fname
            if fp == skip_path:
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if any(n in line for n in needles) and OPEN_TASK_RE.match(line):
                            results.append((fp.stem, line.rstrip()))
            except OSError:
                continue
    results.sort(key=lambda x: x[0])
    return results


def render_task_line(src: str, raw: str) -> str:
    cleaned = re.sub(r"^\s*- \[ \]\s*(#task\s+)?", "", raw)
    return f"> - _from [[{src}]]:_ {cleaned}"


def build_block(now: dt.datetime, mode: str,
                attendee_tasks: dict[str, list[tuple[str, str]]]) -> str:
    total = sum(len(v) for v in attendee_tasks.values())
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    label = "auto-inserted" if mode == "morning" else "refreshed pre-meeting"
    lines = [
        BEGIN_MARK,
        f"<!-- generated: {stamp} | mode: {mode} -->",
        f"> [!todo]+ Open follow-ups ({total}) — {label} {stamp}",
        "> Move items down into the agenda or ignore. Check off the source task to drop it from this list.",
    ]
    for person, tasks in attendee_tasks.items():
        lines.append(">")
        lines.append(f"> **[[{person}]]**")
        for src, raw in tasks:
            lines.append(render_task_line(src, raw))
    lines.append(END_MARK)
    return "\n".join(lines)


def existing_block_mode(text: str) -> str | None:
    """Return the mode of the existing block, or None if no block."""
    if BEGIN_MARK not in text:
        return None
    m = GEN_MARK_RE.search(text)
    return m.group(2) if m else "unknown"


def strip_existing_block(text: str) -> str:
    """Remove an existing BEGIN..END block (and surrounding blank lines) from text."""
    pattern = re.compile(
        re.escape(BEGIN_MARK) + r".*?" + re.escape(END_MARK) + r"\n*",
        re.DOTALL,
    )
    return pattern.sub("", text, count=1)


def find_today_meetings() -> list[Path]:
    today = today_str()
    if not MEETINGS.is_dir():
        return []
    return sorted(p for p in MEETINGS.glob("*.md") if p.name.startswith(today))


def collect_attendee_tasks(path: Path) -> tuple[list[str], dict[str, list[tuple[str, str]]]]:
    text = path.read_text(encoding="utf-8")
    fm, _ = split_frontmatter(text)
    people = parse_people(fm)
    out: dict[str, list[tuple[str, str]]] = {}
    for person in people:
        tasks = find_open_tasks_for(person, path)
        if tasks:
            out[person] = tasks
    return people, out


def is_individual(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    fm, _ = split_frontmatter(text)
    return parse_type(fm) == "Individual"


def write_block(path: Path, mode: str, dry_run: bool, now: dt.datetime) -> str:
    text = path.read_text(encoding="utf-8")
    cleaned_text = strip_existing_block(text)
    fm, body = split_frontmatter(cleaned_text)
    if not fm:
        return f"skip (malformed frontmatter) {path.name}"
    people, attendee_tasks = collect_attendee_tasks(path)
    if not people:
        return f"skip (no people) {path.name}"
    if not attendee_tasks:
        # If there's an existing block but no tasks now, strip it.
        if BEGIN_MARK in text and not dry_run:
            path.write_text(f"{fm}\n\n{body}" if body else f"{fm}\n", encoding="utf-8")
            return f"removed empty block {path.name}"
        return f"skip (no open follow-ups) {path.name}"
    block = build_block(now, mode, attendee_tasks)
    new_text = f"{fm}\n\n{block}\n\n{body}" if body else f"{fm}\n\n{block}\n"
    total = sum(len(v) for v in attendee_tasks.values())
    if dry_run:
        return f"DRY-RUN would write {mode} block ({total}) to {path.name}"
    path.write_text(new_text, encoding="utf-8")
    return f"wrote {mode} block ({total}) to {path.name}"


def decide_mode(path: Path, now: dt.datetime) -> str | None:
    """Return 'morning', 'pre-meeting', or None for skip-this-cycle."""
    text = path.read_text(encoding="utf-8")
    existing = existing_block_mode(text)
    if existing is None:
        return "morning"
    if existing == "pre-meeting":
        return None  # already refreshed today
    # existing block is morning — only refresh if we're 20..30 min before start
    start = parse_meeting_start(path)
    if start is None:
        return None
    minutes_until = (start - now).total_seconds() / 60.0
    lo = PRE_MEETING_LEAD_MIN - PRE_MEETING_WINDOW
    hi = PRE_MEETING_LEAD_MIN + PRE_MEETING_WINDOW
    if lo <= minutes_until <= hi:
        return "pre-meeting"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force-mode", choices=["auto", "morning", "pre-meeting"], default="auto",
                    help="Override the auto-decided mode (manual testing).")
    ap.add_argument("--ignore-business-hours", action="store_true",
                    help="Skip the weekday/business-hour gate (manual testing).")
    args = ap.parse_args()

    now = now_local()

    if not args.ignore_business_hours and not in_business_hours(now):
        # Silent exit during off-hours; LaunchAgent polls every 5 min all day.
        return 0

    meetings = find_today_meetings()
    if not meetings:
        print(f"[meeting_prep] {today_str()}: no meeting notes for today", file=sys.stderr)
        return 0

    for m in meetings:
        if not is_individual(m):
            print(f"[meeting_prep] skip (not Individual) {m.name}", file=sys.stderr)
            continue
        mode = args.force_mode if args.force_mode != "auto" else decide_mode(m, now)
        if mode is None:
            continue
        msg = write_block(m, mode, args.dry_run, now)
        print(f"[meeting_prep] {msg}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
