# Meeting Pre-Population (Optional)

This is an optional, advanced feature. It takes a weekly (or nightly) export of
your calendar, produced on *your* side of whatever calendar/tenant system you
use, and turns it into Obsidian meeting notes and People stubs automatically —
attendee wikilinks, group-vs-individual classification, recurring-series
roots, and reschedule / cancellation handling.

It is **off by default** and is not required for any other part of the vault.

## Architecture: producer vs. consumer

The pipeline is deliberately split in two so the calendar-specific,
tenant-specific work stays out of the template:

- **Producer (yours to build):** anything that can read your calendar and
  write a JSON "handoff" file, following the delivery convention below. The
  only requirement is that it emits the JSON contract below.
  **Recommended:** a Claude Code session with an MCP connector to your
  calendar system (Microsoft 365, Google Workspace, etc.) doing the read and
  running a deterministic transform — no relay or custom server needed. See
  [`Meeting-Handoff-MCP-Producer.md`](Meeting-Handoff-MCP-Producer.md), with
  a ready-to-use M365 reference transform at
  [`Templates/Scripts/mcp_meeting_transform.py`](../Templates/Scripts/mcp_meeting_transform.py).
  For any other producer, a deterministic reference transform ships as
  [`Templates/Scripts/meeting_handoff_transform.js`](../Templates/Scripts/meeting_handoff_transform.js)
  to build against.
- **Consumer (shipped here):** `Templates/Scripts/meeting_prepopulate.py`, run
  on a poll (5-minute Task Scheduler task on Windows; a `com.meeting-prepopulate`
  LaunchAgent on macOS). It watches a local drop folder, validates and
  de-duplicates each handoff, and writes/updates notes in the vault.

This feature works with any calendar system that can produce the contract —
it is not locked to any one tenant or vendor.

The consumer's transport is pluggable (`MEETING_PREPOP_SOURCE`): the default
`drop` reads a plain local folder, fed by whatever relay you like — see
[`Azure-Blob-Handoff-Relay.md`](Azure-Blob-Handoff-Relay.md) for a worked
Azure Blob Storage relay (`handoff_blob_pull.py`) that needs no cloud-sync
client and no special filesystem permissions on the consumer side; `mcp` is a
stub for a future tenant MCP endpoint. See
[`HANDOFF-ARCHITECTURE.md`](HANDOFF-ARCHITECTURE.md) for the full tier
comparison, including why a cloud-drive-sync-client transport (Tier A) is
described there but deliberately not implemented in this repo — it's fragile
in practice (sync-timing races, conflict copies, online-only placeholders)
and the specifics are too tenant/vendor-specific to generalize as a shipped
reference implementation.

## Requirements

- **A local folder for the consumer to watch** — created automatically by
  `meeting_prepopulate.py` on first run (`~/MeetingIngest` by
  default). No special filesystem permissions needed; it's a plain vault
  subfolder.
- **A producer** that writes the contract below into that folder (directly,
  or via a relay — see [`Azure-Blob-Handoff-Relay.md`](Azure-Blob-Handoff-Relay.md)).

## Install

Run the opt-in component:

```bash
./install.sh --only 52-meeting-prepopulate
```

**macOS:** loads the `com.meeting-prepopulate` LaunchAgent.
**Windows:** the `meeting-prepopulate` Task Scheduler task is registered
disabled by `install.ps1`; enable it once your producer/relay is in place:
`Enable-ScheduledTask -TaskName meeting-prepopulate -TaskPath '\Obsidian\'`.

The one remaining manual step either way: **stand up your producer** so
handoff files actually arrive. The recommended path needs no relay at all —
see [`Meeting-Handoff-MCP-Producer.md`](Meeting-Handoff-MCP-Producer.md). A
relay (below) is only needed if your producer's environment can't reach the
drop folder directly.

## Configuration

All paths are environment-overridable (defaults in parentheses). Set these in the
LaunchAgent's `EnvironmentVariables` block or your shell:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MEETING_PREPOP_HANDOFF_DIR` | `~/MeetingIngest` | The local drop folder to watch (auto-created on first run). |
| `MEETING_PREPOP_VAULT` | `~/Obsidian` | Vault root. |
| `MEETING_PREPOP_PEOPLE_DIR` | `<vault>/People` | Where People stubs are written. |
| `MEETING_PREPOP_MEETINGS_DIR` | `<vault>/Meetings` | Where meeting notes are written. |
| `MEETING_PREPOP_GROUPS_DIR` | `<vault>/Groups` | Group rosters, used for group-vs-individual classification. |
| `MEETING_PREPOP_SCRIPTS_DIR` | `<vault>/Templates/Scripts` | Home for the logs, lock, state, and config sub-folders. |
| `MEETING_PREPOP_LOG_DIR` | `<scripts>/logs` | Log directory. |
| `MEETING_PREPOP_SOURCE` | `drop` | Transport: `drop` (plain local folder, this doc — see [`Azure-Blob-Handoff-Relay.md`](Azure-Blob-Handoff-Relay.md) for a worked relay into it), or `mcp` (stub). |

Optional JSON config at `<scripts>/.config/meeting_prepopulate.json`:

```json
{
  "admin_emails": ["assistant@example.com"],
  "skip_subject_prefixes": ["fyi"],
  "treat_start_as_utc": false
}
```

- `admin_emails` — people (assistants, EAs) who schedule on your behalf. They are
  dropped from the attendee count (so they don't flip a 1:1 into a "group"
  meeting) and are omitted from `people:` wikilinks. Default: none. The
  installer (`52-meeting-prepopulate`) prompts for this during setup so you
  don't have to discover the need for it the hard way — any custom producer
  you build should read the same file and drop these emails at the source
  too, not just rely on the consumer's demotion.
- `skip_subject_prefixes` — subject prefixes that mark an invite as
  informational: a calendar placeholder to be aware of, not a meeting to take
  notes in. Matched in the `PREFIX:` / `PREFIX -` shape people actually type,
  so `FYI: Budget Review` is gated and `FYI Roundtable` is not. Default:
  `["fyi"]`. Set to `[]` to disable the gate and let every invite produce a
  note.
- `treat_start_as_utc` — a workaround for a *specific* producer bug where local
  times are emitted without an offset. Leave `false` unless your producer is
  known to do this; enabling it against a correct producer shifts every timed
  meeting by hours.

## The handoff contract

### File conventions

For each export the producer writes a trio into the handoff folder:

- `<name>.json` — the payload (schema below).
- `<name>.ready` — a zero-byte sentinel written **after** the JSON is fully
  committed. The consumer only processes a payload once its `.ready` sibling
  exists (this avoids reading half-synced files).
- `<name>.json.sha256` *(optional)* — first whitespace token on line 1 is the
  hex SHA-256 of the JSON, for integrity checking. If absent, the check is
  skipped.

After a successful run the trio is moved to a timestamped `_processed/`
subfolder. Sync-tool conflict copies (e.g. "conflicted copy" filenames) are
ignored, in case your relay's landing folder is itself synced by something.

### JSON schema (`schema_version: 1`)

```json
{
  "schema_version": 1,
  "source": "my-calendar-producer",
  "source_version": "1.0.0",
  "generated_at": "2026-07-27T21:00:00Z",
  "user": { "timezone": "America/New_York", "email": "you@example.edu" },
  "week": { "start": "2026-07-27", "end": "2026-08-02" },
  "meetings": [
    {
      "uid": "AAMk-stable-id",
      "start": "2026-07-28T14:00:00-04:00",
      "is_all_day": false,
      "is_cancelled": false,
      "is_private_appointment": false,
      "my_response_status": "accepted",
      "subject": "Quarterly Review",
      "is_recurring_instance": false,
      "series_uid": null,
      "recurrence_human": null,
      "rrule_raw": null,
      "attendees": [
        { "email": "jane.doe@example.edu", "display_name": "Jane Doe",
          "is_resource": false, "response_status": "accepted", "is_optional": false }
      ]
    }
  ],
  "contacts": [
    { "email": "jane.doe@example.edu", "given_name": "Jane", "surname": "Doe",
      "display_name": "Jane Doe", "title": "Director", "company": "Example",
      "phone": "+1-555-0100" }
  ],
  "notes": [ { "level": "info", "text": "3 meetings exported" } ]
}
```

Top-level keys — `schema_version`, `source`, `generated_at`, `user`, `week`,
`meetings`, and `contacts` are **required**; `source_version` and `notes` are
optional.

**`user`** — `email` (your address; excluded from attendee counts) and
`timezone` (IANA name; falls back to `America/New_York` if omitted).

**`meetings[]`** — `start` (ISO 8601, the one truly required field) plus
`uid` (stable id used for reschedule/cancel tracking), `subject`, `attendees`,
and the booleans `is_all_day`, `is_cancelled`, `is_private_appointment`,
`is_recurring_instance`. `my_response_status` of `declined` and a
`producer_classification_hint.class` of `solo`/`personal_block` cause the meeting
to be skipped. `series_uid`, `recurrence_human`, and `rrule_raw` populate the
recurring-series root note.

**`meetings[].attendees[]`** — `email`, `display_name`, and the flags
`is_resource`, `is_optional`, and `response_status` (`declined` excludes them
from the count and from wikilinks).

**`contacts[]`** — keyed by `email`; `given_name`, `surname`, `display_name`,
`title`, `company`, and `phone` feed the People stub. Provide a contact entry for
every attendee email you want resolved to a rich People note.

## Behavior details

- **Classification.** 2–9 counted attendees → a group meeting (matched against
  `Groups/` rosters by subject); 1 → individual; 10+ → broadcast. Resources,
  optional attendees, decliners, and `admin_emails` are not counted.
- **Optional attendees on group-mailbox invites.** The rule above has one
  exception, and it exists because ignoring optional attendees outright
  produces a specific wrong answer. When a departmental or shared mailbox
  sends the invite, the convention inverts: the coordinator is booked
  `required` and the whole distribution — you included — is `optional`.
  Counting required-only then leaves exactly **one** participant, so a
  nine-person announcement is filed as a 1:1 with whoever happens to hold the
  required slot, every time it recurs. On these invites (and only these)
  non-declined optional attendees are counted. Because this is a property of
  how the invite was built, no change of producer or connector affects it —
  which is what makes it easy to misdiagnose as an integration bug.
- **Two filters, one decision.** `classify()` and `build_people_wikilinks()`
  must agree on whether optional attendees count; they are passed the same
  value. Letting them drift yields a note whose `type:` says one thing and
  whose `people:` list says another.
- **Solo blocks, and the exception.** An event with no other human attendees
  is treated as a personal block and produces no note — "Hold", "PTO", an
  errand. The exception is a block carrying a hosted-meeting join URL (Zoom,
  Teams, Meet, Webex): someone who blocks their own time and pastes a join
  link into it is convening a meeting without sending invitations, and it is
  often the one meeting of the day they most needed somewhere to type. The
  link is looked for in the structured location fields and then the body —
  note the body is redacted for `private`/`confidential` sensitivity, so a
  link hiding only in a sensitive body is not seen and the event stays solo.
- **Ambiguous blocks get a note, not silence.** An event with no participants
  at all is usually personal time, but not always: a working session booked
  through delegate access to the user's calendar arrives looking identical to
  self-booked time, and no calendar field separates the two. Dropping those
  silently is how someone walks into a meeting with nowhere to type, so the
  benefit of the doubt goes to creating the note. Decisions are made in order
  of certainty: a hosted join link means it is definitely a meeting; then
  learned rules; then a seed vocabulary of blocks (travel legs, time off,
  errands, heads-down work); and anything still unresolved becomes an `Ad-hoc`
  note carrying the subject as its `title:` and tagged `#needs-attendees`.
- **The pipeline learns from corrections instead of guessing.** Participants
  are never inferred from subject text or correlated email — a wrong
  `[[Person]]` link does not sit still, it propagates into the People graph
  and the follow-up scanner. Instead the two edits a user naturally makes to a
  flagged note are read back on the next run:

  | the user does this | the pipeline learns |
  |---|---|
  | deletes the generated note | suppress that subject from now on |
  | fills in `people:` | every later occurrence inherits those people |

  Rules are keyed on a normalized subject (`block_key()`), not the `uid`,
  because a repeating block gets a fresh `uid` every occurrence — and the key
  strips parentheticals, digits and clock times so that "Delta Air Lines
  flight 2712 to Atlanta (G5WFPS)" and the next week's flight number collapse
  to one rule. The store lives at `<scripts>/.state/meeting_block_learning.json`
  and is plain, hand-editable JSON. The point is that identification is paid
  once per recurring subject rather than once per occurrence, which is what
  makes it possible to automate around a scheduling practice you do not
  control.
- **In progress counts as current.** Past meetings are skipped on the *end*
  time, not the start, so a meeting already under way still gets its note.
  Judging on the start meant any run after the hour struck refused to create
  a note for the meeting the user was sitting in.
- **Reschedule / cancel.** Re-emitting the same `uid` at a new time moves the
  note (leaving a redirect stub so old wikilinks resolve) and drops a banner. A
  cancelled or declined meeting removes the generated note.
- **Idempotence.** Every write is fenced with HTML comments and keyed by `uid`,
  so re-running over the same week does not duplicate content or clobber your
  hand-written notes.
- **Heuristics.** Group/distribution-list mailboxes and personal-vs-work email
  routing use built-in heuristics tuned for common conventions; they are
  best-effort, not exhaustive.

## Troubleshooting

- **Agent runs but nothing happens / scan finds zero files** → confirm your
  producer or relay is actually delivering the `.json`/`.json.sha256`/`.ready`
  trio into `MEETING_PREPOP_HANDOFF_DIR`. If you're using the Azure Blob
  relay, see its own troubleshooting section in
  [`Azure-Blob-Handoff-Relay.md`](Azure-Blob-Handoff-Relay.md).
- **Times are off by hours** → your producer's timezone handling; see
  `treat_start_as_utc` above.
- **Logs** → `~/Library/Logs/meeting-prepopulate.log` (launchd stdout) and
  `<scripts>/logs/meeting_prepopulate.log` (the rotating worker log).
