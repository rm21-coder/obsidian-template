# ONBOARDING.md — installing this, with Claude's help

This file is for someone installing this template for the first time, and for
the Claude Code session helping them. `CLAUDE.md` is the other agent-facing
file in this repo; it is for *changing* the template. This one is for getting
it running and keeping it running.

## How to use Claude here

**Claude's job is to explain, check, and diagnose. The human runs the
installer.** That split is deliberate. The install is a single command — there
is nothing to automate — and the steps that actually block people are ones no
agent can do for them: getting a gateway key issued, getting a calendar
connector approved by the tenant, being on the VPN. Handing the keyboard over
buys nothing and makes it harder to see what happened.

So: read this file, answer questions from it, help interpret errors, and run
read-only checks freely. Before running anything that installs software,
registers a scheduled job, or writes a credential, show the person the command
and let them run it.

Two things to refuse outright, no matter who asks:

- Never put an API key in a profile, a note, a commit, or a chat message. The
  installer stores it in the platform keystore. If someone pastes a key into
  the conversation, tell them to rotate it.
- Never disable, unload, or re-baseline the security controls to make an alert
  go away. A drift alert means a watched file changed; the answer is to find
  out why. `docs/Security-Harness.md` explains the two controls.

If this file does not cover something, say so rather than guessing. The
failure modes here are specific and a plausible-sounding wrong answer costs
more than "I don't know — check `docs/`."

## What you are installing

An Obsidian vault plus the automation that runs on top of it. **The repo is
the vault**: you clone it to `~/Obsidian` and its folders become your vault.

Once running, and without you doing anything:

| When | What happens |
|---|---|
| every :00 and :30 | new notes get semantically tagged against a fixed taxonomy |
| weekdays 05:00 | your calendar is pulled through the M365 connector |
| weekdays 06:00 | that becomes tomorrow's meeting notes + People stubs |
| 05:00 | changed notes get a proposed data-classification tier for you to review |
| weekdays 07:00 | a morning dashboard renders: today's meetings, open to-dos, new notes |
| Mondays 07:00 | a vault lint reports duplicate tags, broken links, schema gaps |

The meeting pipeline is the part most people install this for, and it is also
the part with a real prerequisite — see below.

## Before you start (a human has to do these)

1. **If your profile points at an institutional AI gateway, be on the network
   that can reach it.** These are typically internal-only. Off VPN, every
   model call fails with a bare `Connection error` that points at nothing —
   the single most common "it's broken" report. `./install.sh --only 90-verify`
   prints the endpoint you are actually pointed at.
2. **Have the API key for that endpoint.** The installer asks for it and puts
   it in the platform keystore; it is not in the profile file and never
   should be.
3. **Have the Claude CLI installed and signed in**, with the Microsoft 365 MCP
   connector approved for your account. Check with `claude mcp list` — you
   should see a Microsoft 365 entry. Without this, the calendar half installs
   cleanly and then produces nothing.
4. **Have the profile file** your colleague sent you, if there is one. A
   profile carries the organization-specific answers — gateway endpoint,
   internal domains, connector tool names — so you are not asked to invent
   them. It contains no credentials, which is why it can be emailed. See
   `installers/profiles/README.md`; without one, the installer simply asks.

## Install

macOS, from a Terminal, with the profile in `~/Downloads`:

```bash
git clone https://github.com/rm21-coder/obsidian-template ~/Obsidian && cd ~/Obsidian && ./install.sh --profile ~/Downloads/ours.env
```

Windows, from PowerShell:

```powershell
git clone https://github.com/rm21-coder/obsidian-template $env:USERPROFILE\Obsidian; cd $env:USERPROFILE\Obsidian; powershell -ExecutionPolicy Bypass -File .\Templates\Scripts\windows\install.ps1 -Profile $env:USERPROFILE\Downloads\ours.env
```

It will ask for a few per-person answers the profile cannot carry — your
display name, your work email, and your assistant's email if you have one. It
will also offer to install a local LLM stack (Ollama + Open WebUI, multi-GB,
needs Docker). **Answer no** unless you specifically want to run models on your
own machine; nothing else depends on it.

Preview without changing anything: add `--dry-run` (macOS).

### One extra step on Windows

The Windows installer does **not** prompt for the gateway key. It prints a
`secret_store.py set <KEY_NAME>` command at the end — run that once. The key
goes into a DPAPI-encrypted file readable only by your Windows account.

Windows also ships three scheduled jobs disabled on purpose, because each needs
something per-user first. Enable one deliberately after you have validated it:

```powershell
Enable-ScheduledTask -TaskName meeting-pull -TaskPath '\Obsidian\'
```

## First run — check it actually works

Do these in order. The first two are safe to re-run anytime.

```bash
./install.sh --only 90-verify
```

Prints a status table: which scripts are present, which jobs are registered,
and which endpoint your model calls resolve to. The endpoint line should name
the gateway, not `api.anthropic.com`.

```bash
python3 ~/Obsidian/Templates/Scripts/meeting_pull.py --dry-run
```

Renders the calendar prompt and calls nothing. This is the fastest way to tell
whether the connector is really wired up before trusting the 05:00 run.

```bash
python3 ~/Obsidian/Templates/Scripts/tag_clippings.py
```

Tags anything untagged, and prints the endpoint it used.

**Want something to look at?** The content folders ship empty on purpose. To
see the system working end to end before your own notes arrive:

```bash
python3 ~/Obsidian/Templates/Scripts/seed_demo_content.py
```

That writes ~74 synthetic notes (an invented company, `.example` addresses) so
the dashboard, tagger, and views have something to act on. `--remove` takes it
all back out cleanly.

## When something looks broken

Logs live in `~/Library/Logs/` on macOS (`meeting-pull.log`, `tag-clippings.log`,
`obsidian-classify.log`, and so on). Read the log before theorising.

**Every model call fails with `Connection error`.** You are off the VPN. The
gateway is internal-only. Reconnect and re-run; the scheduled jobs will catch
up on their own.

**The 05:00 calendar pull produced nothing, no error.** Almost always the MCP
connector. Run `claude mcp list` and confirm a Microsoft 365 entry exists, then
`meeting_pull.py --dry-run`. The tool names in the profile must match what
`claude mcp list` reports, not what the desktop app labels them.

**"Claude CLI sign-in expired."** The pull detects this and says so rather than
retrying. Run `claude`, then `/login`, and let the catch-up firing handle it.

**The pull says the API host is not reachable.** It is gating on having a
network route — usually Wi-Fi hasn't come up yet after a sleep. It is not an
error; later catch-up runs handle it.

**A meeting that your assistant booked is classified as a group meeting.**
Their address needs to be in `admin_emails`. Re-run
`./install.sh --only 52-meeting-prepopulate` and enter it.

**A security drift alert.** A watched file changed. That is the control
working. Find out what changed and why before doing anything else; do not
re-baseline to silence it.

## Where to look next

`docs/` has a file per subsystem, and they are written to be read:

- `docs/Meeting-Handoff-MCP-Producer.md` — the calendar pipeline, and the two
  failure modes that are easy to misdiagnose
- `docs/Semantic Auto-Tagger Setup.md` — how tagging decides what it decides
- `docs/Data-Classification.md` — the classification tiers and the review queue
- `docs/Security-Harness.md` — the two security controls and how to respond
- `docs/Windows Setup.md` — the full Windows guide
- `README.md` — every component, every flag

The fastest way to get an answer is to open this folder in Claude Code and ask.
It can read all of the above, check your actual configuration, and tell you
which of the failure modes above matches what you are seeing.
