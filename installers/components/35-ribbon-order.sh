#!/usr/bin/env bash
# 35-ribbon-order.sh - apply the tracked ribbon order to workspace.json.
#
# The ribbon order is persisted by Obsidian in workspace.json under
# 'left-ribbon.hiddenItems' (an ordered dict). workspace.json itself
# is per-machine state (open files, tabs, scroll positions) and stays
# gitignored, but the ribbon order is shareable workflow config.
#
# We track the ribbon order in .obsidian/ribbon-config.json and merge
# it into workspace.json at install time.
#
# Runs after 30-plugins so the plugin IDs referenced by ribbon-config
# correspond to plugins that are actually present.
#
# To capture a new order after drag-reordering in Obsidian:
#   /usr/bin/python3 ~/Obsidian/Templates/Scripts/sync-ribbon-order.py --export
# Then commit the updated .obsidian/ribbon-config.json.

set -euo pipefail

VAULT="$HOME/Obsidian"
CONFIG="$VAULT/.obsidian/ribbon-config.json"
SCRIPT="$VAULT/Templates/Scripts/sync-ribbon-order.py"

if [[ ! -f "$CONFIG" ]]; then
    warn "  $CONFIG not present; skipping ribbon-order sync"
    return 0 2>/dev/null || exit 0
fi
if [[ ! -f "$SCRIPT" ]]; then
    err "  $SCRIPT missing"
    exit 1
fi
# Warn if Obsidian is running - the merge could be clobbered when Obsidian
# next writes workspace.json from its in-memory state.
if pgrep -x Obsidian >/dev/null 2>&1; then
    warn "  Obsidian is currently running; quit it (Cmd-Q) before this step"
    warn "  to prevent the merged ribbon order from being overwritten."
    if [[ "${INTERACTIVE:-1}" -eq 1 ]]; then
        if ! confirm "Continue anyway?" N; then
            warn "  skipped ribbon-order sync; quit Obsidian and re-run:"
            warn "    ./install.sh --only 35-ribbon-order"
            return 0 2>/dev/null || exit 0
        fi
    fi
fi

info "Applying tracked ribbon order to workspace.json..."
/usr/bin/python3 "$SCRIPT" --apply --vault "$VAULT"
ok "ribbon-order component complete"
info "  On next Obsidian launch, the ribbon will be in your preferred order"
info "  To capture a new order after drag-reordering:"
info "    /usr/bin/python3 \"$SCRIPT\" --export"
