#!/usr/bin/env bash
# 48-meeting-prep.sh - Meeting Prep auto-insert LaunchAgent.
#
# meeting_prep.py is stdlib-only (audited). Plist runs /usr/bin/python3
# every 5 min; the script self-gates to weekdays 05:30-19:00.

set -euo pipefail
source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"

if [[ ! -x "$VAULT/Templates/Scripts/meeting_prep.py" ]]; then
    err "  $VAULT/Templates/Scripts/meeting_prep.py missing"
    exit 1
fi

info "Installing LaunchAgent com.obsidian.meeting-prep..."
install_plist_and_load Templates/Scripts/com.obsidian.meeting-prep.plist com.obsidian.meeting-prep

ok "meeting-prep component complete"
info "  Log: ~/Library/Logs/meeting-prep.log"
info "  Runs every 5 min; only acts on weekdays 05:30-19:00"
