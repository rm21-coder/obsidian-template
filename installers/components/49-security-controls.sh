#!/usr/bin/env bash
# 49-security-controls.sh - two security agents + state dir + log rotation.
#
#   plugin-check  daily 06:30 + watch ~/Obsidian/.obsidian/plugins
#   integrity     daily 06:35 + watch ~/Library/LaunchAgents, vault Scripts,
#                 ~/.local/share/obsidian-security
#
# All three are stdlib-only and run via /usr/bin/python3, so they never need an
# executable bit here; component 56-script-permissions sets their modes.

set -euo pipefail
source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"
SECDIR="$VAULT/Templates/Scripts"
STATE_DIR="$HOME/.local/share/obsidian-security"
LOG_DIR="$HOME/Library/Logs"

info "Verifying security scripts are in place..."
for f in plugin_integrity_check.py integrity_monitor.py; do
    if [[ ! -f "$SECDIR/$f" ]]; then
        err "  $SECDIR/$f missing"
        exit 1
    fi
done
ok "  all 3 scripts present"

info "Creating state directory..."
mkdir -p "$STATE_DIR" "$LOG_DIR"
chmod 0700 "$STATE_DIR"
ok "  $STATE_DIR (mode 0700)"

info "Installing 3 LaunchAgents..."
install_plist_and_load Templates/Scripts/com.obsidian.security.plugin-check.plist com.obsidian.security.plugin-check
install_plist_and_load Templates/Scripts/com.obsidian.security.integrity.plist     com.obsidian.security.integrity

info "Establishing baselines..."
NEEDS_BASELINE=0
if [[ ! -f "$STATE_DIR/plugin_allowlist.json" || "${REBASELINE:-0}" -eq 1 ]]; then NEEDS_BASELINE=1; fi
if [[ ! -f "$STATE_DIR/integrity_state.json" || "${REBASELINE:-0}" -eq 1 ]]; then NEEDS_BASELINE=1; fi

if [[ "$NEEDS_BASELINE" -eq 1 ]]; then
    if [[ "${INTERACTIVE:-1}" -eq 1 ]]; then
        if confirm "Establish security baselines now (one-time)?" Y; then
            /usr/bin/python3 "$SECDIR/plugin_integrity_check.py" --update || warn "  plugin baseline returned non-zero"
            /usr/bin/python3 "$SECDIR/integrity_monitor.py"      --update || warn "  integrity baseline returned non-zero"
            ok "  baselines established"
        else
            warn "  baselines not established; controls will alert on first run"
        fi
    else
        info "  --auto: skipping baseline; run 'plugin_integrity_check.py --update' + 'integrity_monitor.py --update' manually"
    fi
else
    ok "  baselines already present at $STATE_DIR"
fi

# Log rotation (newsyslog)
NEWSYSLOG="/etc/newsyslog.d/obsidian-security.conf"
NEWSYSLOG_LINE="$LOG_DIR/obsidian-security.log $USER:staff   644  7  *   \$D0 J"
if [[ -f "$NEWSYSLOG" ]] && grep -q "obsidian-security.log" "$NEWSYSLOG"; then
    ok "  newsyslog rotation already configured"
else
    info "Configuring newsyslog rotation (sudo required)..."
    echo "$NEWSYSLOG_LINE" | sudo tee "$NEWSYSLOG" >/dev/null
    sudo chmod 0644 "$NEWSYSLOG"
    ok "  $NEWSYSLOG"
fi

ok "security-controls component complete"
info "  Log: $LOG_DIR/obsidian-security.log"
info "  Alerts: $STATE_DIR/alerts.log"
