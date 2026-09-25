---
classification: public
---

# Dashboard Action Buttons (Optional, macOS)

The Morning Dashboard is a static `file://` HTML page — a browser will never
let a page spawn local processes, so its action buttons can't simply run
scripts. Instead each button links to a custom URL scheme:

    obsidian-dashboard://run/<action>

and a tiny helper app, **DashboardActions.app**, registers that scheme and
dispatches to `Templates/Scripts/dashboard_actions.sh`.

## Install

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

The dashboard only renders the buttons when the handler app exists —
without it (and on Windows, where no handler exists yet) the dashboard is
identical minus the button bar, so nothing else depends on this component.

## Actions

| Button | What it runs | Sync or background |
|---|---|---|
| Pull meetings | `meeting_pull.py` | background |
| Refresh dashboard | `morning_dashboard.py` | synchronous |
| Refresh RAG index | `obsidian-rag-sync.py` | background |

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

- **Buttons missing from the dashboard** — the handler app isn't installed
  (see above), or you're on Windows.
- **A click does nothing, no notification** — check that exactly one app
  owns the scheme: `open 'obsidian-dashboard://run/refresh-dashboard'` from
  Terminal should launch it. Re-run the builder to re-register.
- **Notification says Failed** — the exit code and output are in
  `~/Library/Logs/dashboard-actions.log`.
