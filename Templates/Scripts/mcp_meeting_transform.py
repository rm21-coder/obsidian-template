#!/usr/bin/env python3
"""
mcp_meeting_transform.py — deterministic PRODUCER transform for the meeting
pre-population pipeline, fed by a Claude Code session with an MCP connector
to your calendar system instead of Cowork/OneDrive or a custom-built server.

This is the M365-specific reference implementation of the pattern documented
in docs/Meeting-Handoff-MCP-Producer.md: any MCP connector that exposes
calendar search + full-event-detail tools can act as the producer this way,
but the exact JSON shape each connector returns differs, so a different
tenant's connector (Google Workspace, a bespoke on-prem calendar MCP server,
etc.) needs its own small transform following this one's structure, not this
file unchanged.

It mirrors meeting_handoff_transform.js's contract (schema-version-1) field
for field, but consumes the JSON shape returned by Microsoft's official M365
MCP connector's outlook_calendar_search + read_resource tools instead of raw
Graph calendarView/$batch responses. It is NOT a Claude Code script itself —
it has no MCP access of its own. The calling session fetches events (via
outlook_calendar_search to list a day, read_resource on each URI for full
attendee/location/sensitivity detail) and writes them to a plain JSON file;
this script turns that file into the same schema-v1 payload the consumer
already validates.

Known gap vs. a Graph-API-based producer (Cowork, Power Automate, a Graph
script): most MCP connectors expose no directory/contacts lookup tool, so
attendee enrichment (title, department, office, manager) is not available
here. Every contact's directory_source is therefore always "invite-fallback"
— an already-documented, first-class degraded case in the schema-v1 contract,
not a new failure mode.

Input JSON shape (what the caller must assemble):
    {
      "user": {"display_name": str, "email": str, "tenant": str, "timezone": str (IANA)},
      "week": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
      "events": [ <one read_resource-shaped object per calendar event> ]
    }

Usage:
    python3 mcp_meeting_transform.py --input /path/to/raw_events.json --out-dir ~/MeetingIngest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = "m365-mcp"
SOURCE_VERSION = "0.1.0"

# Windows timezone IDs (what Outlook/Graph events carry) -> IANA names (what
# Python's zoneinfo understands). Extend as needed; unmapped names fall back
# to being treated as already-IANA (works for UTC etc.) with a loud warning.
WINDOWS_TZ_MAP = {
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "UTC": "UTC",
}

# Same heuristic as meeting_handoff_transform.js's isGroupMailbox — the MCP
# data's attendee "type":"resource" tag already catches most of this, but
# non-resource-tagged distribution lists still need the name/domain heuristic.
GROUP_MAILBOX_DOMAIN_RE = re.compile(r"(^|\.)exchange\.", re.I)
GROUP_MAILBOX_NAME_PREFIX_RE = re.compile(r"^(grp-|dg-|dl-|org-)", re.I)
GROUP_MAILBOX_OFFICE_RE = re.compile(r"^office of ", re.I)
GROUP_MAILBOX_ALLCAPS_RE = re.compile(r"^[A-Z]{2,6}-")

VIDEO_PATTERNS = (
    re.compile(r"https?://[a-z0-9.-]*zoom\.us/\S+", re.I),
    re.compile(r"https?://teams\.microsoft\.com/\S+", re.I),
    re.compile(r"https?://[a-z0-9.-]*\.webex\.com/\S+", re.I),
    re.compile(r"https?://meet\.google\.com/\S+", re.I),
)


def lc(s: str | None) -> str:
    return (s or "").strip().lower()


def is_group_mailbox(name: str | None, email: str) -> bool:
    n = (name or "").strip()
    e = lc(email)
    domain = e.split("@")[-1] if "@" in e else ""
    local = e.split("@")[0] if "@" in e else ""
    if GROUP_MAILBOX_DOMAIN_RE.search(domain):
        return True
    if GROUP_MAILBOX_NAME_PREFIX_RE.match(n):
        return True
    if GROUP_MAILBOX_OFFICE_RE.match(n):
        return True
    if GROUP_MAILBOX_ALLCAPS_RE.match(n) and "." not in local:
        return True
    return False


def is_external(email: str, tenant_domains: frozenset) -> bool:
    domain = lc(email).split("@")[-1] if "@" in email else ""
    return domain not in tenant_domains


def to_utc_iso(wall_clock: str | None, windows_tz: str | None) -> str | None:
    """Convert a Graph-style {dateTime, timeZone} wall-clock pair to true UTC
    ISO-8601 (DST-aware). dateTime looks like '2026-08-05T15:00:00.0000000'."""
    if not wall_clock:
        return None
    s = wall_clock.split(".")[0]  # drop fractional seconds
    naive = datetime.fromisoformat(s)
    iana = WINDOWS_TZ_MAP.get(windows_tz or "", windows_tz or "UTC")
    try:
        tz = ZoneInfo(iana)
    except Exception:
        print(f"[mcp-transform] WARNING: unmapped timezone {windows_tz!r}, "
              f"treating as UTC", file=sys.stderr)
        tz = timezone.utc
    aware = naive.replace(tzinfo=tz)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def duration_minutes(start_iso: str | None, end_iso: str | None) -> int | None:
    if not start_iso or not end_iso:
        return None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    delta = datetime.strptime(end_iso, fmt) - datetime.strptime(start_iso, fmt)
    return round(delta.total_seconds() / 60)


def split_name(display_name: str | None) -> tuple[str | None, str | None]:
    dn = (display_name or "").strip()
    if not dn or "," in dn:
        return None, None
    parts = dn.split()
    if len(parts) < 2:
        return dn, None
    return parts[0], parts[-1]


def map_role(attendee_type: str | None) -> str:
    t = (attendee_type or "").lower()
    if t == "resource":
        return "resource"
    if t == "optional":
        return "optional"
    return "required"


def extract_video(location_display: str | None, body_preview: str | None) -> tuple[bool, str | None]:
    text = f"{location_display or ''}\n{body_preview or ''}"
    for pat in VIDEO_PATTERNS:
        m = pat.search(text)
        if m:
            return True, m.group(0)
    return False, None


def build_handoff(events: list[dict], user: dict, week: dict,
                  tenant_domains: frozenset,
                  excluded_attendees: frozenset = frozenset()) -> dict:
    user_email = lc(user.get("email"))
    seen: dict[str, dict] = {}
    meetings = []
    excluded_mailboxes: dict[str, bool] = {}
    excluded_admins: dict[str, bool] = {}

    for ev in events:
        raw_attendees = ev.get("attendees") or []
        attendees = []
        n_req_human = n_optional = n_declined = n_resources = n_external = 0

        # Decided before the attendee loop because the counters below need it.
        # On a departmental-mailbox blast the coordinator is the lone
        # `required` attendee and everyone else, the user included, is
        # `optional` -- so a required-only count returns 1 and the event is
        # hinted as a 1:1 with the coordinator. Counting non-declined optional
        # attendees on these invites (and only these) keeps the hint honest.
        # The consumer applies the identical rule in classify(); the two must
        # agree or the note's `type:` and `people:` disagree.
        org_pre = ev.get("organizer") or {}
        org_is_group = is_group_mailbox(org_pre.get("name") or "",
                                        lc(org_pre.get("address")))

        for a in raw_attendees:
            email = lc(a.get("address"))
            name = a.get("name") or ""
            if not email:
                continue
            role = map_role(a.get("type"))
            resp = a.get("responseStatus") or "none"
            if is_group_mailbox(name, email):
                excluded_mailboxes[name or email] = True
                continue
            if email in excluded_attendees:
                # Scheduling assistants/EAs who show up as a required attendee
                # on every meeting they book, but aren't real participants —
                # dropped entirely (not counted, not in attendees[]/contacts[])
                # so 1:1s they schedule classify as individual, not group.
                excluded_admins[name or email] = True
                continue
            ext = is_external(email, tenant_domains)
            resource = role == "resource"
            optional = role == "optional"
            declined = lc(resp) == "declined"
            if resource:
                n_resources += 1
            if optional:
                n_optional += 1
            if declined:
                n_declined += 1
            if ext:
                n_external += 1
            counts_as_participant = not optional or org_is_group
            if not resource and not declined and counts_as_participant and email != user_email:
                n_req_human += 1
            attendees.append({
                "display_name": name or None, "email": email,
                "response_status": resp, "role": role,
                "is_resource": resource, "is_external": ext, "is_optional": optional,
            })
            if not resource and email not in seen:
                given, surname = split_name(name)
                seen[email] = {
                    "email": email, "display_name": name or None,
                    "given_name": given, "surname": surname,
                    "title": None, "department": None, "office": None, "phone": None,
                    "manager": None, "company": None, "is_external": ext,
                    "directory_source": "invite-fallback", "directory_object_id": None,
                }

        org = ev.get("organizer") or {}
        org_email, org_name = lc(org.get("address")), org.get("name") or ""
        if is_group_mailbox(org_name, org_email):
            humans = [a for a in attendees
                     if not a["is_resource"] and a["response_status"] != "declined"
                     and a["email"] != user_email]
            if len(humans) == 1:
                organizer = {"display_name": humans[0]["display_name"], "email": humans[0]["email"],
                            "is_me": humans[0]["email"] == user_email, "is_group_mailbox": False}
            else:
                organizer = {"display_name": org_name or None, "email": org_email or None,
                            "is_me": False, "is_group_mailbox": True}
        else:
            organizer = {"display_name": org_name or None, "email": org_email or None,
                        "is_me": org_email == user_email, "is_group_mailbox": False}

        start = ev.get("start") or {}
        end = ev.get("end") or {}
        start_iso = to_utc_iso(start.get("dateTime"), start.get("timeZone"))
        end_iso = to_utc_iso(end.get("dateTime"), end.get("timeZone"))

        sensitivity = ev.get("sensitivity") or "normal"
        body_preview = ev.get("bodyPreview") or ""
        if sensitivity in ("private", "confidential"):
            body_preview = f"[redacted: sensitivity={sensitivity}]"

        if n_req_human == 0:
            cls = "solo"
        elif n_req_human == 1:
            cls = "individual"
        elif n_req_human >= 12:
            cls = "broadcast"
        else:
            cls = "group"

        # read_resource's full event detail has no top-level isOrganizer flag
        # (only the search-summary view does) — derive it from the organizer
        # address instead, which read_resource always provides.
        is_organizer = org_email == user_email
        my_resp = "organizer" if is_organizer else "none"
        if not is_organizer:
            for a in attendees:
                if a["email"] == user_email:
                    my_resp = a["response_status"]
                    break

        loc = ev.get("location") or {}
        loc_display = loc.get("displayName") if isinstance(loc, dict) else loc
        is_video, join_url = extract_video(loc_display, ev.get("bodyPreview"))

        recurrence = ev.get("recurrence")
        meetings.append({
            "uid": ev.get("id"), "series_uid": None,
            "is_recurring_instance": recurrence is not None,
            "instance_index": None, "recurrence_human": None, "rrule_raw": None,
            "subject": ev.get("subject") or "", "subject_sensitivity": "normal",
            "start": start_iso, "end": end_iso,
            "duration_minutes": duration_minutes(start_iso, end_iso),
            "is_all_day": bool(ev.get("isAllDay")),
            "organizer": organizer, "my_response_status": my_resp, "attendees": attendees,
            "attendee_counts": {
                "required_non_declined_non_resource": n_req_human,
                "optional_counted_as_required": org_is_group,
                "optional": n_optional, "declined": n_declined,
                "resources": n_resources, "external": n_external,
            },
            "location": {"display": loc_display, "is_teams_meeting": is_video, "teams_join_url": join_url},
            "body_preview": body_preview, "body_format": "text",
            "categories": ev.get("categories") or [], "sensitivity": sensitivity,
            "is_cancelled": bool(ev.get("isCancelled")),
            "is_private_appointment": sensitivity == "private",
            "created_at": ev.get("createdDateTime"), "last_modified_at": ev.get("lastModifiedDateTime"),
            "producer_classification_hint": {
                "class": cls,
                "rationale": f"{n_req_human} counted non-resource human attendees"
                             f"{' (group-mailbox invite: optional counted)' if org_is_group else ' (required only)'}; "
                             f"recurring={recurrence is not None}",
            },
        })

    contacts = list(seen.values())
    notes = []
    if excluded_mailboxes:
        names = ", ".join(sorted(excluded_mailboxes))
        notes.append({"level": "info",
                      "text": f"Excluded {len(excluded_mailboxes)} group/office mailboxes "
                              f"from attendees and contacts: {names}."})
    if excluded_admins:
        names = ", ".join(sorted(excluded_admins))
        notes.append({"level": "info",
                      "text": f"Excluded {len(excluded_admins)} scheduling assistant(s)/EA(s) "
                              f"from attendees and contacts (configured via --exclude-attendees): "
                              f"{names}."})
    notes.append({"level": "info",
                  "text": "Producer is the M365 MCP connector, not Cowork/OneDrive. "
                          "All contacts are directory_source=invite-fallback — no "
                          "directory/contacts lookup tool is exposed by this connector."})

    return {
        "schema_version": 1, "source": SOURCE, "source_version": SOURCE_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user": {"display_name": user.get("display_name"), "email": user.get("email"),
                 "tenant": user.get("tenant"), "timezone": user.get("timezone")},
        "week": {"start": week.get("start"), "end": week.get("end")},
        "meetings": meetings, "contacts": contacts, "notes": notes,
    }


def _default_admin_emails() -> str:
    """Read admin_emails from the same .config/meeting_prepopulate.json the
    consumer (meeting_prepopulate.py) already uses, so there's one source of
    truth for "who is my assistant/EA" instead of two. Falls back to empty
    (no exclusion) if the config doesn't exist or has no admin_emails."""
    config_path = (Path(__file__).resolve().parent / ".config"
                   / "meeting_prepopulate.json")
    if not config_path.is_file():
        return ""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    return ",".join(config.get("admin_emails") or [])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, type=Path,
                    help="Path to the raw-events JSON assembled from MCP tool calls")
    ap.add_argument("--out-dir", required=True, type=Path,
                    help="Local drop folder to write the schema-v1 trio into")
    ap.add_argument("--run-date", default=None,
                    help="YYYY-MM-DD used in the output filename (default: today)")
    ap.add_argument("--tenant-domains", default="example.edu",
                    help="Comma-separated tenant domains (for external-attendee detection) "
                         "— set to your own org's domain(s), e.g. a multi-domain tenant "
                         "might use 'example.edu,med.example.edu,corp.example.edu'")
    ap.add_argument("--exclude-attendees", default=None,
                    help="Comma-separated emails to drop entirely from attendees/contacts "
                         "(scheduling assistants/EAs who aren't real participants). "
                         "Defaults to admin_emails from .config/meeting_prepopulate.json "
                         "(the same config the consumer uses) — pass '' to disable.")
    args = ap.parse_args()

    if args.exclude_attendees is None:
        args.exclude_attendees = _default_admin_emails()

    raw = json.loads(args.input.read_text(encoding="utf-8"))
    tenant_domains = frozenset(d.strip().lower() for d in args.tenant_domains.split(",") if d.strip())
    excluded_attendees = frozenset(
        e.strip().lower() for e in args.exclude_attendees.split(",") if e.strip())

    payload = build_handoff(raw["events"], raw["user"], raw["week"], tenant_domains,
                            excluded_attendees=excluded_attendees)

    run_date = args.run_date or datetime.now().strftime("%Y-%m-%d")
    out_dir = args.out_dir.expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"schedule-handoff-{run_date}.v1"
    payload_bytes = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    (out_dir / f"{stem}.json").write_bytes(payload_bytes)
    digest = hashlib.sha256(payload_bytes).hexdigest()
    (out_dir / f"{stem}.json.sha256").write_text(f"{digest}  {stem}.json\n", encoding="utf-8")
    (out_dir / f"{stem}.ready").touch()  # commit marker, written last

    gal = sum(1 for c in payload["contacts"] if c["directory_source"] == "tenant-gal")
    fallback = sum(1 for c in payload["contacts"] if c["directory_source"] == "invite-fallback")
    print(json.dumps({
        "meetingCount": len(payload["meetings"]), "contactCount": len(payload["contacts"]),
        "contacts_tenant_gal": gal, "contacts_invite_fallback": fallback,
        "notes": len(payload["notes"]), "sha256": digest, "out_dir": str(out_dir),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
