---
classification: public
---

# Dashboard Action Buttons (Optional)

The Morning Dashboard is a static `file://` HTML page — a browser will never
let a page spawn local processes, so its action buttons can't simply run
scripts. Instead each button links to a custom URL scheme:

    obsidian-dashboard://run/<action>

which something on the machine has to answer. On macOS a tiny helper app,
**DashboardActions.app**, registers that scheme and dispatches to
`Templates/Scripts/dashboard_actions.sh`. On Windows a per-user registry key
points it at `Templates/Scripts/windows/dashboard_action.py` (see
[Windows](#windows) below).

## Install (macOS)

Opt-in installer component (it registers a URL-scheme handler system-wide,
so it asks first):

```bash
./install.sh --only 57-dashboard-actions
```

or by hand:

```bash
bash ~/Obsidian/Templates/Scripts/build_dashboard_actions_app.sh
```

The builder compiles `DashboardActions.applescript` into
`~/Applications/DashboardActions.app` (osacompile + ad-hoc codesign) and
registers it with Launch Services. Re-run it any time the `.applescript`
changes. If your vault is not at `~/Obsidian`, edit `scriptsDir` in the
`.applescript` first.

The dashboard only renders the buttons when a handler exists — without one
the dashboard is identical minus the button bar, so nothing else depends on
this component.

## Windows

Also opt-in: `install.ps1` asks, or follows `PROFILE_DASHBOARD_ACTIONS` in an
install profile, and an unattended run without a profile declines. To opt in
later, or to undo it:

```powershell
cd $env:USERPROFILE\Obsidian; powershell -ExecutionPolicy Bypass -File .\Templates\Scripts\windows\Install-DashboardActions.ps1
```

(add `-Remove` to undo). It writes
`HKCU\Software\Classes\obsidian-dashboard` — the current user only, no
elevation — whose command runs `dashboard_action.py` with the venv's
`pythonw.exe`. `update.ps1` refreshes the key if it exists and never adds it;
`uninstall.ps1` removes it.

The handler accepts exactly the three actions below and reads nothing else
from the URL. Each runs the job the way its scheduled task does — through
`run_logged.py`, appending to `%LOCALAPPDATA%\obsidian-logs\<task>.log`, with
no console window — so a click and a scheduled run leave the same log.
Differences from macOS:

- **Each action follows its scheduled task.** If the task is disabled (the
  meeting pull ships disabled until a profile enables it) or missing, the
  click is reported as not set up rather than run. If the task is running
  right now, it is reported as already running.
- **Pull meetings pulls now:** it drops the scheduled task's
  `--skip-if-fresh`.
- **Refresh RAG index appears only where RAG is set up** (`rag_status.py`:
  `OBSIDIAN_COLLECTION_ID` filled in), on both platforms; the dashboard's RAG
  sync card likewise. Most installs never run the local LLM layer. A
  `refresh-rag` link on a machine without it is refused as not set up.
- **Every action waits for its job** and then toasts *Finished* or *Failed
  (exit N)* with the log to read, rather than toasting at start only. A second
  click while the first is still running is reported, not run twice.
- **Repetition is bounded**, because any web page can fire the scheme, not
  just the dashboard. A job runs once at a time: the click takes the same
  per-job lock `run_logged.py` takes for a scheduled run, so a click and a
  scheduled run never overlap. Each action then waits a cooldown before it
  will run again from the dashboard (10 minutes for Pull meetings, 2 for the
  others). Each run has a time limit, so a hung job cannot block later
  clicks. Pull meetings also honours today's recorded sign-in refusal, the
  same guard the scheduled pull applies.
- Every click, refused ones included, is logged to
  `%LOCALAPPDATA%\obsidian-logs\dashboard-actions.log`, along with any failure of
  the handler itself.

The browser asks before each launch whether the page may open the handler
(it names Python, the program it starts, not the dashboard). On a `file://`
page it may not offer "always allow", so expect the prompt on every click.
Only accept it on the dashboard: the same prompt on any other page is that
page asking to start a dashboard job.

## Actions

| Button | What it runs | Sync or background |
|---|---|---|
| Pull meetings | `meeting_pull.py` | background |
| Refresh dashboard | `morning_dashboard.py` | synchronous |
| Refresh RAG index (only where RAG is set up) | `obsidian-rag-sync.py` | background |

Every dispatch appends to `~/Library/Logs/dashboard-actions.log`, and the
app posts a notification when an action starts and finishes.

### Why there is no "rebaseline security" button

There used to be one, and it was removed on 2026-09-25. `obsidian-dashboard://`
is a registered URL scheme, so any web page can open it — not just this
dashboard. Rebaselining adopts the current scripts, LaunchAgents and plugins as
trusted, so a page that fired it could finish a tamper for an attacker: change
a plugin or script, which the integrity controls detect, then trigger a
rebaseline and the detection disappears. The browser's "open this app?" prompt
was the only obstacle, and "always allow" removes it.

Adopting a baseline is a deliberate act. Run it in a terminal, in this order
(the plugin check first, because it restamps the file the integrity monitor
hashes):

```bash
/usr/bin/python3 ~/Obsidian/Templates/Scripts/plugin_integrity_check.py --update
/usr/bin/python3 ~/Obsidian/Templates/Scripts/integrity_monitor.py --update
```

The dispatcher now refuses the action, so an old bookmark or a hostile page
gets `unknown action` and exit 2.

**Why long actions run in the background:** the URL-scheme applet is
single-instance. If it sat inside a multi-minute RAG re-index, every later
button click would be silently dropped until it finished — which reads as
"the buttons stopped working". Backgrounding frees the applet in
milliseconds; for a backgrounded action, the "Finished" notification means
*started successfully* — the action's own output is in the log.

## Troubleshooting

- **Buttons missing from the dashboard** — the handler isn't installed (see
  above). On Windows: `Test-Path HKCU:\Software\Classes\obsidian-dashboard`.
  The buttons appear only on a dashboard rendered after it was installed.
- **A click does nothing, no notification** — check that exactly one app
  owns the scheme: `open 'obsidian-dashboard://run/refresh-dashboard'` from
  Terminal should launch it. Re-run the builder to re-register.
- **Notification says Failed** — the exit code and output are in
  `~/Library/Logs/dashboard-actions.log`.
