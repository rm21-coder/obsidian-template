#!/bin/bash
# dashboard_actions.sh — dispatcher for the Morning Dashboard's action buttons.
#
# Invoked by DashboardActions.app (an AppleScript URL-scheme handler; see
# build_dashboard_actions_app.sh and DashboardActions.applescript) via:
#   dashboard_actions.sh <action-name>
#
# The dashboard is a static file:// HTML page opened in a browser, so its
# buttons can't run local commands directly — a browser page can't spawn
# processes regardless of origin. DashboardActions.app closes that gap: its
# Info.plist registers the obsidian-dashboard:// URL scheme, so a button
# link like obsidian-dashboard://run/refresh-dashboard makes macOS launch
# the app, which shells out to this dispatcher.
#
# Long-running actions are backgrounded (nohup + &). This is load-bearing,
# not a nicety: the URL-scheme applet is single-instance, so while it waits
# on a synchronous action every later button click is silently dropped —
# which reads as "the buttons stopped working". Backgrounding frees the
# applet in milliseconds; output lands in $LOG.
#
# GUI-launched processes (AppleScript apps included) get a minimal PATH that
# does not include user-installed tool locations, so every binary below is
# called by its absolute path rather than relying on PATH resolution.
set -uo pipefail
cd "$(dirname "$0")"

PY=/usr/bin/python3
VENV_PY=./.venv/bin/python3
LOG="$HOME/Library/Logs/dashboard-actions.log"

action="${1:-}"
echo "$(date '+%Y-%m-%d %H:%M:%S') dispatch: ${action}" >> "$LOG"

case "$action" in
  pull-meetings)
    # Fetches today's calendar and drops the handoff trio into the drop
    # folder; the meeting-prepopulate LaunchAgent picks it up from there.
    # meeting_pull.py reads everything (identity, drop folder, producer
    # choice, MCP tool names) from .config/meeting_pull.json — the same
    # single code path the scheduled 05:00 LaunchAgent uses.
    nohup "$PY" meeting_pull.py >> "$LOG" 2>&1 &
    ;;

  rebaseline-security)
    # Adopts the current state of Scripts/, LaunchAgents, and the plugin
    # allowlist as the new trusted baseline. Deliberately runs both checks
    # even if the first fails, and fails overall if either did.
    #
    # Order matters: plugin_integrity_check.py --update stamps a fresh
    # vetted_at timestamp into plugin_allowlist.json on every run, changing
    # its hash even when no plugin actually changed. integrity_monitor.py
    # treats that file as a state-dir trust anchor and baselines whatever
    # hash it sees — so if it ran first, its baseline would be stale the
    # instant plugin_integrity_check.py rewrote the file, and the very next
    # check would report bogus drift on plugin_allowlist.json forever after.
    rc=0
    "$PY" plugin_integrity_check.py --update >> "$LOG" 2>&1 || rc=1
    "$PY" integrity_monitor.py --update >> "$LOG" 2>&1 || rc=1

    # The dashboard's pipeline-health section reads each job's
    # LastExitStatus from launchd itself — a value launchd only updates
    # when IT invokes the job, not when the underlying script is run
    # manually like above. Kickstart both so launchd records a fresh,
    # clean status immediately. Labels must match what's actually loaded
    # (verify with: launchctl list | grep obsidian.security).
    uid=$(id -u)
    /bin/launchctl kickstart -k "gui/$uid/com.obsidian.security.integrity" || rc=1
    /bin/launchctl kickstart -k "gui/$uid/com.obsidian.security.plugin-check" || rc=1
    exit $rc
    ;;

  refresh-dashboard)
    # Fast (a render plus a browser open) — stays synchronous so the
    # applet's "Finished" notification means the new page is actually up.
    "$PY" morning_dashboard.py >> "$LOG" 2>&1
    ;;

  refresh-rag)
    # Backgrounded: a full vault re-index takes minutes on a big vault.
    nohup "$VENV_PY" -u obsidian-rag-sync.py >> "$LOG" 2>&1 &
    ;;

  *)
    echo "dashboard_actions.sh: unknown action '$action'" >&2
    exit 2
    ;;
esac
