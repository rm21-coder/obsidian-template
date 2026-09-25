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
cd "$(dirname "$0")" || { echo "dashboard_actions: cannot cd to $(dirname "$0")" >&2; exit 1; }

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

  # rebaseline-security was removed 2026-09-25 and must not come back.
  #
  # obsidian-dashboard:// is a registered URL scheme, so ANY web page can fire
  # it, not just this dashboard. Rebaselining adopts the current state of the
  # scripts, LaunchAgents and plugins as trusted -- so a page that fired it
  # would complete a tamper for the attacker: change a plugin or a script,
  # which the integrity controls detect, then trigger a rebaseline and the
  # detection is erased. The browser's "open this app?" prompt was the only
  # thing in the way, and "always allow" removes it.
  #
  # Adopting a baseline is a deliberate act; do it in a terminal, in order:
  #   /usr/bin/python3 plugin_integrity_check.py --update
  #   /usr/bin/python3 integrity_monitor.py --update
  # A request for it now falls through to *) below and is refused.

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
