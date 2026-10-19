# Email Tasks (optional)

Each weekday at 06:45, `email_tasks.py` finds the requests in your mail that
are waiting on you personally and appends one Tasks line per request to
`Actions/Email Tasks.md`:

```
## 2026-10-19

- [ ] Approve Dee Example's September timesheet, from Payroll System, "Timesheet awaiting approval" ([open in Outlook](https://outlook.office.com/...)) 📅 2026-10-21 #task
- [ ] Review the Q3 budget draft and send comments, from Bob Builder, "Q3 budget draft" ([open in Outlook](https://outlook.office.com/...)) ⏫ #task
- [ ] Send Bob the headcount numbers, from Bob Builder, "Q3 budget draft" ([open in Outlook](https://outlook.office.com/...)) ⏫ #task
- [ ] Pick a date for the offsite (team ask), from Cy Example, "Offsite" ([open in Outlook](https://outlook.office.com/...)) #task
- [ ] Decide whether to join the vendor panel (check), from Fay Example, "Panel invite" ([open in Outlook](https://outlook.office.com/...)) #task
- [ ] Review: an email from Eve Example may contain instructions aimed at the email assistant (treated as data, not acted on) ([open in Outlook](https://outlook.office.com/...)) #task
```

One email can produce several tasks (one per open ask, up to five), and in a
thread only the asks still open in the latest message count. `(check)` marks
a call the session was unsure of. A **Review** line reports an email that
tried to give the session instructions: its wording is fixed by the script,
not taken from the email, and it carries only the sender's name and the
message's own link.

The To-Do page and the Morning Dashboard (07:00) already pick up `#task`
lines, so nothing else needs configuring. Tick a task off in either place.

## How it works

The job is split in two, and only the half that never sees mail can write.

1. **Reading: a confined Claude session.** `email_tasks.py` starts a headless
   `claude -p` session with the same confinement as the meeting pull (see
   [`Meeting-Handoff-MCP-Producer.md`](Meeting-Handoff-MCP-Producer.md)):
   `--restricted` in an empty temporary directory, `--permission-mode
   dontAsk`, `--allowedTools` naming exactly the Microsoft 365 connector's
   mail search and item read, `--disallowedTools` removing every built-in tool
   and the rest of the connector (calendar, Teams, SharePoint), a JSON schema
   for the reply, and `--no-session-persistence` so no transcript of your mail
   is kept. The session searches the Inbox from the last successful run to
   now, reads each candidate, checks Sent Items for a reply you already sent,
   and returns a list of requests. It has no tool that can write, run
   anything or reach the web.
2. **Writing: a fixed transform.** `email_task_transform.py` validates each
   request and writes the line itself. It drops anything malformed, anything
   already written (by message id, remembered 30 days), and anything you sent.
   From the task text, subject (cut to 60 characters) and sender name it
   strips every link and URI scheme,
   tags, wikilinks, HTML, code, table pipes and Tasks emoji. The one link it
   adds is the message's own Outlook link, accepted only as `https` on
   `outlook.office.com`, `outlook.office365.com` or `outlook.live.com`. A due
   date must be a real date within a year of today. The block then goes
   through the same ingest guard as every other job that writes outside text
   (`templater_guard.neutralize`), and the note is written atomically.

What counts as a request is in the prompt, `email_tasks_prompt.txt`:

| Kind | When |
|---|---|
| `approval` | a signature, ERP, timesheet or access approval assigned to you; listed first, worded "Sign ...", "Approve ..." or "Grant or deny access ..." with the document, requester and amount when the email shows them |
| `request` | it asks you by name, or you are the only To recipient, and it asks you to do, decide, review or reply |
| `team_ask` | sent to 10 or fewer individual people (`team_ask_max_recipients`) and asks the group, you included; dropped if another recipient already answered it for the group |

Mail from a priority sender (anyone in `high_priority_senders`, or with
`Email-Priority` checked on their People note) is always read in full, never
skipped as automated. Relative deadlines ("by Friday") are counted from the
date the email was sent.

Skipped: mail where you are only Cc'd and the ask is someone else's, FYIs,
thank-yous, newsletters, notifications, and mailing lists that do not name
you. An approval request whose sender domain does not match the service it
claims is written as `Verify (possible phishing): ...`.

## Priority

Priority comes from your own data, never from the email:

1. a sender in `high_priority_senders` (config) gets ⏫;
2. otherwise the sender's People note, matched on `Email-Work`,
   `Email-Work-2` or `Email-Personal`: a checked `Email-Priority` checkbox
   gets ⏫. Unchecked or empty is no opinion. (Notes written before it was a
   checkbox may hold `high`, `medium` or `none`; those are still honoured,
   `none` meaning no marker even when rule 3 would add one.)
3. otherwise, with `title_priority: true`, a sender whose signature gives a
   Director-or-above title gets 🔼. The title is the model's reading of the
   signature, so this one an email can influence; it is off by default.

## Setup

Needs the Claude Code CLI, signed in, with the Microsoft 365 connector
connected (`claude mcp list`). The same setup the meeting pull uses.

**macOS:**

```bash
./install.sh --only 59-email-tasks
```

It asks for your name and work address (defaulting to the meeting pull's),
writes `Templates/Scripts/.config/email_tasks.json` and loads
`com.obsidian.email-tasks`.

**Windows:** copy `Templates\Scripts\email_tasks.example.json` to
`Templates\Scripts\.config\email_tasks.json`, fill in `display_name` and
`email`, validate by hand, then enable the task, which is registered disabled:

```powershell
Enable-ScheduledTask -TaskPath '\Obsidian\' -TaskName 'email-tasks'
```

**Validate before trusting the schedule:**

```bash
/usr/bin/python3 ~/Obsidian/Templates/Scripts/email_tasks.py --dry-run
/usr/bin/python3 ~/Obsidian/Templates/Scripts/email_tasks.py
```

`--dry-run` prints the command and the rendered prompt and calls nothing.
`--from-json FILE` writes tasks from a saved reply, for testing the transform
without a session.

### Config keys

| Key | Default | Meaning |
|---|---|---|
| `display_name`, `email` | required | who you are; your own mail is never a task |
| `other_addresses` | `[]` | further addresses that are you |
| `note` | `Actions/Email Tasks.md` | where tasks are appended |
| `team_ask_max_recipients` | `10` | the largest recipient list a team ask can have |
| `max_lookback_hours` | `72` (at most 96) | how far back a run reads after a gap |
| `high_priority_senders` | `[]` | addresses whose requests are always ⏫ |
| `title_priority` | `false` | 🔼 for senders with a senior title |
| `mcp_prefix`, `search_tool`, `read_tool` | Microsoft 365 names | only for a differently-named connector; a name the job is denied is refused |

State (the last successful run and the handled message ids) is in
`~/.local/share/obsidian-email-tasks/state.json` (macOS) or
`%LOCALAPPDATA%\obsidian-email-tasks\state.json` (Windows). The log,
`~/Library/Logs/email-tasks.log`, carries counts and errors only, never mail
content.

## Failure behaviour

A session that did not load the connector, or never completed a mail search,
is a failure, not an empty inbox: nothing is written and the window is not
advanced, so the next run reads the same mail. Each run tries twice. Exit
codes match the meeting pull: 3 for a sign-in or organisation-policy refusal,
5 when the API host is unreachable.

## What it does not stop

- **An email can steer what the session reads and says.** The item-read tool
  takes a resource URI, so an injected instruction could have it read another
  item the connector exposes and carry some of it, as plain text, into task
  lines in your own vault: at most five tasks per message, each up to 200
  characters plus a 60-character subject. Nothing leaves the machine through
  this job, since the session has no tool that sends or writes.
- **A task can be wrong or planted.** A crafted email can produce a task that
  looks legitimate, and the sender name on the line is the model's reading of
  the message. Open the Outlook link before acting on anything that asks for
  money, credentials or access.
- **A Review line depends on the model noticing.** An email that tries to
  instruct the session is flagged only if the session recognises it; one it
  misses is not reported. The confinement, not the flag, is what stops it.
- **Classification.** The note is written `classification: confidential`.
  Task text summarises mail, so set it higher if your mail warrants it.
