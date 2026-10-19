"""email_task_transform.py - turn the email session's requests into #task lines.

The writing half of email-to-tasks (email_tasks.py is the reading half). The
session reads mail and returns candidate requests as JSON; it has no tool that
writes anything. This module is the only writer: it validates each request,
drops the ones already handled, writes the task text in a fixed shape with
everything an email could use to act inside Obsidian removed, decides the
priority from the user's own data, and appends the lines to one note.

Every field it reads came out of a model that read mail anyone can send, so
nothing is trusted as markup: tags, wikilinks, Tasks emoji, HTML and links are
stripped from the text; the only link a task carries is the message's own
Outlook link, accepted only on Microsoft's hosts; a due date must be a real
date near today; and the output passes through templater_guard.neutralize like
any other outside text.

Stdlib only, Python 3.9-compatible (the macOS job runs on /usr/bin/python3).
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import templater_guard

KINDS = ("approval", "request", "team_ask")
OUTLOOK_HOSTS = ("outlook.office.com", "outlook.office365.com", "outlook.live.com")
MAX_TASK_CHARS = 200
MAX_NAME_CHARS = 60
MAX_SUBJECT_CHARS = 60
MAX_REQUESTS = 100
MAX_TASKS_PER_MESSAGE = 5
MAX_FLAGGED = 25

# Tasks plugin markers: priority, dates, recurrence. An email must not be able
# to plant one, so every one is stripped from model text before the job adds
# its own.
_TASK_EMOJI = "⏫\U0001f53c\U0001f53d⏬\U0001f53a\U0001f4c5⏳\U0001f6eb✅❌\U0001f501➕\U0001f194⛔"
_STRIP_RE = re.compile("[#\\[\\]`<>|*_~^{}\\\\" + _TASK_EMOJI + "]")
# Links: anything with "://", and the schemes that act without one. Prose like
# "Budget review: Friday" keeps its colon.
_SCHEME_RE = re.compile(
    r"\S*://\S*|\b(?:mailto|tel|sms|obsidian|file|javascript|vbscript|data|"
    r"ms-[a-z]+):\S*", re.I)
_SPACE_RE = re.compile(r"\s+")
_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_FM_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 _-]*):[ \t]*(.*)$")

PRIORITY_MARK = {"high": "⏫", "medium": "\U0001f53c"}


def clean_text(value, limit: int) -> str:
    """Plain words only: no markup, no links, no Tasks markers, one line."""
    text = "" if value is None else str(value)
    text = _SCHEME_RE.sub(" ", text)          # any URI, link or scheme:thing
    text = _STRIP_RE.sub(" ", text)
    text = "".join(ch for ch in text if ch.isprintable() or ch == " ")
    text = _SPACE_RE.sub(" ", text).strip(" -")
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def clean_address(value) -> str:
    text = ("" if value is None else str(value)).strip().lower()
    return text if _ADDRESS_RE.match(text) and len(text) <= 254 else ""


def outlook_link(value) -> str:
    """The message's own Outlook link, or nothing. Only https on Microsoft's
    Outlook hosts; anything else would be a link an email chose.

    Python's parser and a browser must agree on the host. A backslash is a
    path separator to a browser but not to urlsplit, so
    `https://evil.example\\@outlook.office.com/` was accepted here and opened
    evil.example (F-20261019). So no backslash, no `@`, no userinfo or port,
    and the link written is rebuilt from the parts that were checked."""
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    if any(c in value for c in " \t\r\n<>()[]\\@") or "%5c" in value.lower():
        return ""
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in OUTLOOK_HOSTS or parts.netloc.lower() != host:
        return ""
    rebuilt = "https://%s%s" % (host, parts.path or "/")
    if parts.query:
        rebuilt += "?" + parts.query
    if parts.fragment:
        rebuilt += "#" + parts.fragment
    return rebuilt


def clean_due(value, today: _dt.date) -> str:
    """A YYYY-MM-DD within a year of today, or nothing."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return ""
    try:
        day = _dt.date.fromisoformat(value)
    except ValueError:
        return ""
    return value if abs((day - today).days) <= 366 else ""


def people_priorities(vault: Path) -> dict:
    """{address: "high" | "medium" | "none"} from People notes' frontmatter:
    Email-Priority, keyed by each address field the meeting pipeline uses.

    Email-Priority ships as a checkbox: checked (`true`) is high, unchecked
    or empty is no opinion. The words high, medium and none are still read,
    for notes written before it became a checkbox."""
    out = {}
    people = vault / "People"
    if not people.is_dir():
        return out
    for note in people.rglob("*.md"):
        try:
            text = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end < 0:
            continue
        fields = {}
        for line in text[3:end].splitlines():
            m = _FM_LINE_RE.match(line.strip())
            if m:
                fields[m.group(1).strip()] = m.group(2).strip().strip("'\"")
        level = fields.get("Email-Priority", "").lower()
        level = {"true": "high"}.get(level, level)
        if level not in ("high", "medium", "none"):
            continue
        for key in ("Email-Work", "Email-Work-2", "Email-Personal"):
            addr = clean_address(fields.get(key))
            if addr:
                out[addr] = level
    return out


def priority(req: dict, config: dict, people: dict) -> str:
    """"high", "medium" or "" from the user's data, never the email's say."""
    sender = req["sender_address"]
    if sender in {clean_address(a) for a in config.get("high_priority_senders", [])}:
        return "high"
    level = people.get(sender)
    if level in ("high", "medium"):
        return level
    if level == "none":
        return ""
    if config.get("title_priority") and req.get("senior_title") is True:
        return "medium"
    return ""


def _message_id(value):
    if isinstance(value, str) and value.startswith("mail:///messages/") and len(value) <= 512:
        return value
    return None


def validate(raw, handled: set, own: set) -> list:
    """The usable requests, in a fixed shape. Anything malformed, already
    handled or sent by the user is dropped, never repaired into something.

    One email can carry several asks, so a message may yield up to
    MAX_TASKS_PER_MESSAGE tasks; the same task twice is one. `handled` holds
    message ids from earlier runs: a message is handled as a whole, in the
    run that first wrote its tasks."""
    items = raw.get("requests") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ValueError("the reply had no 'requests' list")
    out, seen, per_message = [], set(), {}
    for item in items[:MAX_REQUESTS]:
        if not isinstance(item, dict):
            continue
        mid = _message_id(item.get("message_id"))
        if not mid or mid in handled:
            continue
        kind = item.get("kind")
        sender = clean_address(item.get("sender_address"))
        task = clean_text(item.get("task"), MAX_TASK_CHARS)
        if kind not in KINDS or not sender or not task or sender in own:
            continue
        key = (mid, task.lower())
        if key in seen or per_message.get(mid, 0) >= MAX_TASKS_PER_MESSAGE:
            continue
        seen.add(key)
        per_message[mid] = per_message.get(mid, 0) + 1
        out.append({
            "message_id": mid, "kind": kind, "sender_address": sender,
            "sender_name": clean_text(item.get("sender_name"), MAX_NAME_CHARS) or sender,
            "task": task, "due": item.get("due"), "weblink": item.get("weblink"),
            "subject": clean_text(item.get("subject"), MAX_SUBJECT_CHARS),
            "senior_title": item.get("senior_title") is True,
            "phishing_suspect": item.get("phishing_suspect") is True,
            "needs_review": item.get("needs_review") is True,
        })
    return out


def validate_flagged(raw, handled: set, own: set) -> list:
    """Emails the session reports as trying to instruct it. Only who sent it
    and the message's own link are taken: the line that reports it is fixed
    text, so a flagged email cannot write its own warning."""
    items = raw.get("flagged") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return []
    out, seen = [], set()
    for item in items[:MAX_FLAGGED]:
        if not isinstance(item, dict):
            continue
        mid = _message_id(item.get("message_id"))
        sender = clean_address(item.get("sender_address"))
        if not mid or mid in handled or mid in seen or not sender or sender in own:
            continue
        seen.add(mid)
        out.append({"message_id": mid, "sender_address": sender,
                    "sender_name": clean_text(item.get("sender_name"), MAX_NAME_CHARS) or sender,
                    "weblink": item.get("weblink")})
    return out


def render(reqs: list, config: dict, people: dict, today: _dt.date) -> list:
    """One task line per request, approvals first."""
    order = {"approval": 0, "request": 1, "team_ask": 2}
    lines = []
    for r in sorted(reqs, key=lambda r: order[r["kind"]]):
        text = r["task"]
        if r["phishing_suspect"]:
            text = "Verify (possible phishing): " + text
        if r["kind"] == "team_ask":
            text += " (team ask)"
        if r.get("needs_review"):
            text += " (check)"
        line = "- [ ] %s, from %s" % (text, r["sender_name"])
        if r.get("subject"):
            line += ', "%s"' % r["subject"].replace('"', "'")
        link = outlook_link(r["weblink"])
        if link:
            line += " ([open in Outlook](%s))" % link
        mark = PRIORITY_MARK.get(priority(r, config, people))
        if mark:
            line += " " + mark
        due = clean_due(r["due"], today)
        if due:
            line += " \U0001f4c5 " + due
        lines.append(line + " #task")
    return lines


def render_flagged(flagged: list) -> list:
    """One fixed-text review line per email that tried to instruct the
    session. Nothing was done with those instructions; the line says so."""
    lines = []
    for f in flagged:
        line = ("- [ ] Review: an email from %s may contain instructions aimed at the "
                "email assistant (treated as data, not acted on)" % f["sender_name"])
        link = outlook_link(f["weblink"])
        if link:
            line += " ([open in Outlook](%s))" % link
        lines.append(line + " #task")
    return lines


NOTE_HEADER = (
    "---\n"
    "classification: confidential\n"
    "tags:\n"
    "  - actions\n"
    "---\n"
    "# Email Tasks\n\n"
    "Written by `email_tasks.py`: requests found in your mail, one task each. "
    "Tick them off here or on the To-Do page. New runs append below.\n")


def append_tasks(note: Path, lines: list, today: _dt.date) -> None:
    """Append under today's heading, creating the note if needed. Atomic, and
    the appended block is neutralized like any other outside text."""
    note.parent.mkdir(parents=True, exist_ok=True)
    existing = note.read_text(encoding="utf-8") if note.exists() else NOTE_HEADER
    heading = "## %s" % today.isoformat()
    block = templater_guard.neutralize("\n".join(lines))
    if heading not in existing.splitlines():
        block = "\n%s\n\n%s" % (heading, block)
    text = existing.rstrip("\n") + "\n" + block + "\n"
    tmp = note.with_name(note.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(note))
