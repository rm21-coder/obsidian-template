#!/usr/bin/env bash
# 53-morning-dashboard.sh - weekday "morning dashboard" LaunchAgent.
#
# Installs com.morning-dashboard, which runs morning_dashboard.py Mon-Fri at
# 07:00 to build an HTML page of the day's open to-dos, meetings, new
# clippings/creations, and pipeline health under ~/Obsidian/Z_dashboards/, and
# opens it in Google Chrome. Stdlib-only; runs via /usr/bin/python3.

set -euo pipefail
source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"
SCRIPT="$VAULT/Templates/Scripts/morning_dashboard.py"

if [[ ! -f "$SCRIPT" ]]; then
    err "  $SCRIPT missing"
    exit 1
fi

# The script creates this at runtime too; make it now so the first run is clean.
mkdir -p "$VAULT/Z_dashboards"

info "Installing LaunchAgent com.morning-dashboard..."
install_plist_and_load Templates/Scripts/com.morning-dashboard.plist com.morning-dashboard

# Seed a first dashboard so the log exists and you can eyeball the output.
# --no-open avoids launching Chrome during the install.
info "Generating an initial dashboard (no browser)..."
/usr/bin/python3 "$SCRIPT" --no-open \
    || warn "  initial run returned non-zero; see ~/Library/Logs/morning-dashboard.log"

ok "morning-dashboard component complete"
info "  Runs Mon-Fri 07:00; output: $VAULT/Z_dashboards/morning.html (opens in Chrome)"
info "  Log: ~/Library/Logs/morning-dashboard.log"
info "  Run by hand: /usr/bin/python3 \"$SCRIPT\"  (add --no-open to skip Chrome)"
