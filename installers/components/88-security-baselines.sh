#!/usr/bin/env bash
# 88-security-baselines.sh - offer to record the two security baselines, last.
#
# A baseline adopts the machine's current state as trusted, so it has to be
# taken after everything that changes a watched file: the plugins (30/31), the
# scripts, and every LaunchAgent. It used to live in 49-security-controls,
# ahead of components 50-58, which install further agents; the integrity
# monitor's first scheduled run on a fresh install then reported the
# installer's own later steps as drift, and the first thing a new user learned
# was to adopt an alert unread.
#
# Asked, never assumed (--auto skips it), and only when a baseline is missing
# or --rebaseline was given.

set -euo pipefail

SECDIR="$HOME/Obsidian/Templates/Scripts"
STATE_DIR="${OBSIDIAN_SECURITY_STATE_DIR:-$HOME/.local/share/obsidian-security}"

NEEDS_BASELINE=0
if [[ ! -f "$STATE_DIR/plugin_allowlist.json" || "${REBASELINE:-0}" -eq 1 ]]; then NEEDS_BASELINE=1; fi
if [[ ! -f "$STATE_DIR/integrity_state.json" || "${REBASELINE:-0}" -eq 1 ]]; then NEEDS_BASELINE=1; fi

if [[ "$NEEDS_BASELINE" -eq 0 ]]; then
    ok "  baselines already present at $STATE_DIR"
elif [[ "${INTERACTIVE:-1}" -eq 1 ]]; then
    info "  The two security controls compare this machine against a recorded baseline;"
    info "  without one they protect nothing. Record it only if nothing but this installer"
    info "  has changed the vault, its plugins or its LaunchAgents since you started."
    if confirm "Record the security baselines now?" Y; then
        # Plugin allowlist first: it is one of the integrity monitor's trust
        # anchors, so the integrity baseline must record its final form.
        /usr/bin/python3 "$SECDIR/plugin_integrity_check.py" --update || warn "  plugin baseline returned non-zero"
        /usr/bin/python3 "$SECDIR/integrity_monitor.py"      --update || warn "  integrity baseline returned non-zero"
        ok "  baselines recorded"
    else
        warn "  no baselines recorded; both controls stay inactive until you run, in this order:"
        warn "    /usr/bin/python3 $SECDIR/plugin_integrity_check.py --update"
        warn "    /usr/bin/python3 $SECDIR/integrity_monitor.py --update"
    fi
else
    info "  --auto: not recording baselines. When ready, run, in this order:"
    info "    /usr/bin/python3 $SECDIR/plugin_integrity_check.py --update"
    info "    /usr/bin/python3 $SECDIR/integrity_monitor.py --update"
fi
