#!/usr/bin/env bash
# 51-group-photos.sh - Group Photos nightly refresh LaunchAgent.
#
# Two stdlib-only scripts in Z_attachments/ run in order each night:
#   insert_group_placeholders.py  prepends the placeholder to bare member
#                                 rows, then
#   refresh_groups.py             swaps placeholders for real headshots.
# Plist runs both at 02:00 local via StartCalendarInterval.

set -euo pipefail
source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"

for s in insert_group_placeholders.py refresh_groups.py; do
    if [[ ! -f "$VAULT/Z_attachments/$s" ]]; then
        err "  $VAULT/Z_attachments/$s missing"
        exit 1
    fi
done

info "Installing LaunchAgent com.obsidian.group-photos..."
install_plist_and_load Templates/Scripts/com.obsidian.group-photos.plist com.obsidian.group-photos

ok "group-photos component complete"
info "  Log: ~/Library/Logs/group-photos.log"
info "  Runs nightly at 02:00; inserts placeholders then swaps in real headshots"
