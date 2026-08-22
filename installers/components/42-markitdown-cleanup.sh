#!/usr/bin/env bash
# 42-markitdown-cleanup.sh - markitdown_cleanup.py library install.
#
# No LaunchAgent here. The cleanup module is imported by the Markitdown
# Dropper (component 43) and can also be run standalone for retroactive
# cleanup. We just verify the file is in place; its mode is set centrally by
# component 56-script-permissions.

set -euo pipefail

VAULT="$HOME/Obsidian"
SCRIPT="$VAULT/Templates/Scripts/markitdown_cleanup.py"

info "Verifying markitdown_cleanup.py is in place..."
if [[ ! -f "$SCRIPT" ]]; then
    err "  $SCRIPT missing"
    exit 1
fi
ok "  $SCRIPT"

ok "markitdown-cleanup component complete (no LaunchAgent; library only)"
