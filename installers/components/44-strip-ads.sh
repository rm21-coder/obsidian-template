#!/usr/bin/env bash
# 44-strip-ads.sh - Clippings ad-stripper LaunchAgent.

set -euo pipefail
source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"

if [[ ! -x "$VAULT/Templates/Scripts/strip_ads.py" ]]; then
    err "  $VAULT/Templates/Scripts/strip_ads.py missing"
    exit 1
fi

info "Installing LaunchAgent com.obsidian.strip-ads..."
install_plist_and_load Templates/Scripts/com.obsidian.strip-ads.plist com.obsidian.strip-ads

ok "strip-ads component complete"
info "  Log: ~/Library/Logs/strip-ads.log"
info "  Watches: ~/Obsidian/Clippings"
