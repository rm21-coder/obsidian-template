#!/usr/bin/env bash
# 57-dashboard-actions.sh - make the Morning Dashboard's buttons work.
#
# Builds ~/Applications/DashboardActions.app (AppleScript applet, ad-hoc
# signed) and registers it for the obsidian-dashboard:// URL scheme, so the
# dashboard's action buttons (pull meetings, rebaseline security, refresh
# dashboard, refresh RAG) dispatch to Templates/Scripts/dashboard_actions.sh.
# Without this the dashboard simply doesn't render the buttons — nothing
# else depends on it.
#
# Opt-in like 52/54: it registers a URL-scheme handler system-wide, which is
# the kind of thing a user should say yes to explicitly.

set -euo pipefail

VAULT="$HOME/Obsidian"

# An install profile can answer this (PROFILE_DASHBOARD_ACTIONS=1); otherwise
# it is asked, and an --auto run without a profile declines.
if ! pconfirm DASHBOARD_ACTIONS \
        "Build DashboardActions.app so the Morning Dashboard's buttons work? (registers the obsidian-dashboard:// URL scheme)" N; then
    info "  skipped dashboard action buttons; run later with: ./install.sh --only 57-dashboard-actions"
    return 0 2>/dev/null || exit 0
fi

BUILDER="$VAULT/Templates/Scripts/build_dashboard_actions_app.sh"
if [[ ! -f "$BUILDER" ]]; then
    err "  builder not found: $BUILDER (did 10-vault-bootstrap run?)"
    return 1 2>/dev/null || exit 1
fi

bash "$BUILDER"
ok "dashboard action buttons installed (~/Applications/DashboardActions.app)"
info "  Buttons appear on the next dashboard render (Refresh, or tomorrow 07:00)"
info "  Dispatch log: ~/Library/Logs/dashboard-actions.log"
