# Meeting Handoff — MCP-Connector Producer (Recommended)

This is the recommended way to stand up the producer side of
[Meeting Pre-Population](Meeting-Pre-Population.md): a Claude Code session
with an MCP connector to your calendar/directory system does the privileged
read and the transform, and writes straight into the consumer's local drop
folder. No relay, no custom server, no cloud-sync client.

It presumes you (or your tenant) can enable *some* MCP connector for
scheduling data — Microsoft's official Microsoft 365 connector, a Google
Workspace connector, or an equivalent for another calendar system. The
pattern below is written against the M365 connector, since that's the
reference implementation shipped in this repo, but the shape generalizes:
any connector that can search a calendar and return full per-event detail
(attendees, location, sensitivity) can feed it.

## Why this instead of Tier A/B/C

[`HANDOFF-ARCHITECTURE.md`](HANDOFF-ARCHITECTURE.md) lays out three transport
tiers for the *consumer's* fetch side. This approach doesn't add a fourth
tier — it's a producer for **Tier B** (`MEETING_PREPOP_SOURCE=drop`), the
one already shipped and unchanged. What's different is how the payload
going into that drop folder gets made:

- No cloud-drive sync client (Tier A) and its sync-timing fragility.
- No relay needed at all when the session runs on the same machine as the
  vault — it writes directly into the drop folder, so
  [`Azure-Blob-Handoff-Relay.md`](Azure-Blob-Handoff-Relay.md)'s relay is
  optional, only useful if your MCP session runs somewhere that *can't*
  reach the drop folder directly.
- **Not** the same thing as Tier C / `MEETING_PREPOP_SOURCE=mcp`
  (`MCPSource` in `handoff_source.py`). That's a still-unbuilt concept: a
  purpose-built MCP *server* the consumer calls directly
  (`handoff.list_pending`/`fetch`/`ack`). This approach uses an
  *off-the-shelf* MCP connector (one you or your tenant already enabled) as
  a data source for a producer script — no server to build at all. Don't
  set `MEETING_PREPOP_SOURCE=mcp` for this; leave it at the `drop` default.

Net effect: least code to stand up, since the "server" is just a Claude Code
session and a deterministic transform script, not new infrastructure.

## How it works

1. **Enable an MCP connector** for your calendar/directory system (ask your
   IT/security team if it isn't already available in your Claude Code
   environment — for Microsoft tenants this is the Microsoft 365 connector).
2. **In a Claude Code session with that connector active**, fetch the week's
   events:
   - List events for the date range you want (a calendar-search tool).
   - For each event, fetch full detail (a resource-read tool) — you want
     attendees, location, sensitivity, and body preview, not just the
     summary view a list call returns.
   - Assemble everything into one JSON file matching the input shape
     `Templates/Scripts/mcp_meeting_transform.py` expects:
     ```json
     {
       "user": {"display_name": "...", "email": "...", "tenant": "...", "timezone": "America/New_York"},
       "week": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
       "events": [ /* one full-detail event object per event */ ]
     }
     ```
3. **Run the transform**, which mirrors the reference
   `meeting_handoff_transform.js` field-for-field and writes the
   schema-v1 trio (`.json` / `.json.sha256` / `.ready`) straight into the
   consumer's drop folder:
   ```bash
   python3 Templates/Scripts/mcp_meeting_transform.py \
     --input /path/to/raw_events.json \
     --out-dir "$HOME/MeetingIngest" \
     --tenant-domains yourdomain.edu,another.yourdomain.edu \
     --exclude-attendees assistant@yourdomain.edu
   ```
   `--exclude-attendees` defaults to whatever `admin_emails` is set to in
   `<scripts>/.config/meeting_prepopulate.json` (the installer prompts for
   this — see [`Meeting-Pre-Population.md`](Meeting-Pre-Population.md)), so
   you usually don't need to pass it explicitly.
4. The already-running consumer (`meeting_prepopulate.py`, on its normal
   poll) picks up the trio on its next pass and writes/updates the notes.

Steps 2–3 are exactly the kind of thing you'd ask a Claude Code session to
do directly — "pull my meetings for next week via the calendar connector,
assemble them into the transform's input shape, and run the transform". Run
it that way whenever you want an ad-hoc pre-population, or schedule it and
stop thinking about it — see the next section.

## Scheduling it (the automated producer)

The interactive flow above is also shipped as a scheduled job, so meeting
notes are simply *there* each morning instead of being something you remember
to ask for. Three pieces, all opt-in:

| Piece | What it is |
| --- | --- |
| `Templates/Scripts/meeting_pull.py` | The runner. Renders the prompt template, invokes `claude -p` headlessly with the connector's tools allowlisted, and lets the transform write the trio. Stdlib-only and cross-platform. |
| `Templates/Scripts/meeting_pull_prompt.txt` | The prompt, with `{{TOKEN}}` placeholders — the repo carries no identity or paths. |
| `Templates/Scripts/com.obsidian.meeting-pull.plist` | macOS scheduler: weekdays 05:00 (an hour ahead of the morning dashboard) plus 06:30/08:00 catch-up firings. On Windows the equivalent is the `meeting-pull` task in `windows/schedules.psd1`. |

Install it with `./install.sh --only 54-meeting-pull`, which prompts for the
identity block (name, email, tenant, timezone, internal domains) and the
connector's tool names, writes them to `Templates/Scripts/.config/meeting_pull.json`
(gitignored), and loads the agent. It needs the consumer
(`52-meeting-prepopulate`) too — this half only produces the handoff.

Validate by hand before trusting the 05:00 run:

```bash
python3 Templates/Scripts/meeting_pull.py --dry-run   # render the prompt, call nothing
python3 Templates/Scripts/meeting_pull.py             # real run
```

Producer and consumer stay decoupled through the drop folder: the runner never
calls `meeting_prepopulate.py`. Either half can be rescheduled, replaced, or
run by hand without touching the other.

### On a laptop, one firing a morning is not enough

The agent fires at 05:00 and again at 06:30 and 08:00, passing `--skip-if-fresh`
so the later firings cost a directory glob on any morning the first one worked.
That is not belt-and-braces for its own sake; it covers a failure this pipeline
actually hit. launchd does run a missed calendar job when the machine wakes, but
waking to *start* the job is not the same as staying awake to *finish* it: a
sleep mid-session kills the CLI with the calendar already read and no handoff
written (`API Error: Your computer went to sleep mid-response`, exit 1). The
consumer then has nothing to process, and because it exits clean, nothing
anywhere reports a problem.

Three things absorb that:

- **Catch-up firings.** A later attempt runs while the machine is actually in
  use. `--skip-if-fresh` looks for `schedule-handoff-<today>.v1*` in the drop
  folder *and* its `_processed` archive, so a handoff the consumer already
  ingested still counts as done.
- **In-process retries.** `--retries` (default 2, `--retry-delay` 60s) re-runs
  the CLI after a failure, which is usually enough on its own — the machine that
  slept mid-run is typically awake moments later. A non-zero exit with a handoff
  present on disk is treated as success, since the transform runs before the
  session's final message.
- **A desktop notification on real failure** (macOS), because the whole failure
  mode here is silence.

To have the machine awake for the 05:00 run rather than relying on catch-up,
schedule a wake:

```bash
sudo pmset repeat wakeorpoweron MTWRF 04:55:00
```

A scheduled wake gets the machine up but does not keep it up: nobody is at the
keyboard at 05:00, so the idle timer is free to put it straight back to sleep
mid-run. On macOS the runner therefore wraps the CLI call in `caffeinate -i -s`,
which holds an idle-sleep assertion for exactly as long as the run takes. That
covers idle sleep only -- a closed lid or an explicit sleep still ends the run,
which is what the retry and catch-up layers are for. Note also that a Mac on
battery with the lid shut may not honour a scheduled wake at all, and that these
are per-user LaunchAgents: after a reboot they do not run until someone logs in.

### Two failure modes that look like each other

Both surface as "the headless run did nothing", and both are worth knowing
before you debug the wrong one. A headless session has no way to answer a
permission prompt, so anything permission-shaped fails quietly.

1. **Your tenant hasn't approved the connector for unattended use.** Every
   MCP call comes back `Your organization requires approval for this tool` —
   connector-wide, not specific to the calendar tool. No local flag fixes
   this: `--dangerously-skip-permissions` and
   `--permission-mode bypassPermissions` govern *local* permission state, and
   this is a server-side policy. It needs an admin change on the tenant side,
   after which you sign out and back in to pick it up. Until then, run the
   producer interactively, where you can answer the prompt.
2. **The tool names in the allowlist don't match what the CLI exposes.** The
   error is different and easy to misread as the same problem:
   `Claude requested permissions to use ... but you haven't granted it yet`.
   The CLI and desktop clients can name the same connector differently — the
   CLI uses a stable prefix like `mcp__claude_ai_Microsoft_365__*`, while a
   desktop client may show an installation-specific id. `--allowedTools`
   matches literally, so a mismatched name is never granted. Check the real
   names with `claude mcp list` and put the prefix in `meeting_pull.json`.

A useful property of the split design is worth stating plainly, because it
inverts the usual debugging instinct: **a dead producer looks like a healthy
pipeline.** The consumer keeps polling, finds nothing, and exits clean, so
its logs and launchd status are green while no notes get written. The morning
dashboard lists both halves in its pipeline-health panel for exactly this
reason — check the producer's log (`~/Library/Logs/meeting-pull.log`) first
when notes are missing.

## Skipping the LLM entirely: the direct Graph producer

The producer's work is deterministic — the Claude session in the recommended
path exists only to reach the M365 MCP connector. If you are comfortable with
a one-time Microsoft sign-in, `graph_calendar_fetch.py` calls Microsoft Graph
directly and feeds the identical transform:

- **Zero LLM tokens** per run, versus a full headless session daily.
- **A ~2-second HTTP call** — fits inside any laptop wake window, so the
  sleep-mid-session failure class disappears.
- **No Claude CLI dependency** for the 05:00 job (auth expiry, MCP tool-name
  drift, org tool-approval policy all stop mattering here).

Setup:

```bash
# one-time, interactive: device-code sign-in, delegated Calendars.Read only
python3 ~/Obsidian/Templates/Scripts/graph_calendar_fetch.py --auth
```

Then set `"producer": "graph"` in `.config/meeting_pull.json`. The scheduled
`meeting_pull.py` (LaunchAgent / Windows task) picks the new path up on its
next firing — retries, `--skip-if-fresh`, and failure notifications all apply
unchanged. The refresh token lives in the platform keystore (macOS Keychain /
Windows DPAPI) via `secret_store.py`, never in a file.

By default this signs in through Microsoft's pre-registered public
"Graph Command Line Tools" client. If your tenant blocks that client, register
your own public-client app (delegated `Calendars.Read` + `offline_access`),
and set `"graph_client_id"` (and optionally `"graph_auth_tenant"`) in the
config. Bonus over the MCP path: this producer works cross-platform with
nothing but python3 — Windows needs no Claude CLI at all.

## Known gaps vs. a Graph/Cowork-based producer

- Most MCP connectors expose no directory/contacts lookup tool, so contact
  enrichment (title, department, office, manager) isn't available — every
  contact's `directory_source` comes through as `invite-fallback`. This is
  an already-documented, first-class degraded case in the schema-v1
  contract, not a new failure mode; you just get plainer People stubs.
- A connector's full-event-detail response may be missing fields a
  Graph-native producer would have (for example, the M365 connector's
  `read_resource` output has no top-level `isOrganizer` — the reference
  transform derives it from `organizer.address` matching your own email
  instead). If you adapt this for a different connector, check its actual
  output shape rather than assuming it matches Graph's.

## Adapting to a different MCP connector

`mcp_meeting_transform.py` is written specifically against the M365
connector's `outlook_calendar_search` + `read_resource` output shape. A
different connector (Google Workspace, a bespoke on-prem calendar MCP
server) will return differently-shaped JSON, so it needs its own small
transform script — same structure (normalize times to UTC, classify
group/individual/broadcast, drop `admin_emails`, emit schema-v1), different
field mapping. Use `mcp_meeting_transform.py` as the template, not as a
drop-in.
