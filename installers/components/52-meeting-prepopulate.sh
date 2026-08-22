#!/usr/bin/env bash
# 52-meeting-prepopulate.sh - OPT-IN meeting pre-population (the consumer
# side).
#
# Wires up the consumer LaunchAgent that reads schedule-handoff JSON your
# producer delivers into a local drop folder - directly, or via a relay (e.g.
# the Azure Blob Tier B relay, see docs/Azure-Blob-Handoff-Relay.md) - and
# turns it into Obsidian meeting notes + People stubs.
#
# OFF by default, because it requires something an installer cannot do for
# you: a PRODUCER you build on your side (a Power Automate flow, a Microsoft
# Graph job, or an agent) that writes the documented handoff contract.
#
# Full setup, configuration, and the JSON handoff contract:
#   docs/Meeting-Pre-Population.md

set -euo pipefail

VAULT="$HOME/Obsidian"
SCRIPTS="$VAULT/Templates/Scripts"
CONSUMER="$SCRIPTS/meeting_prepopulate.py"
PLIST_SRC="$SCRIPTS/com.meeting-prepopulate.plist"
LABEL="com.meeting-prepopulate"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
HANDOFF="$HOME/MeetingIngest"

if [[ ! -f "$CONSUMER" ]]; then
    warn "  $CONSUMER not found; skipping meeting pre-population"
    return 0 2>/dev/null || exit 0
fi
if [[ ! -f "$PLIST_SRC" ]]; then
    warn "  $PLIST_SRC not found; skipping meeting pre-population"
    return 0 2>/dev/null || exit 0
fi

# Opt-in gate. Skipped unless the user explicitly asks, because it needs a
# producer of its own - or unless an install profile has already answered
# (PROFILE_MEETING_PREPOPULATE=1), which is the only way an --auto run sets
# this up: the profile is the consent that --auto by itself cannot be.
if ! pconfirm MEETING_PREPOPULATE \
        "Set up meeting pre-population? (needs a producer you build)" N; then
    info "  skipped; enable later with: ./install.sh --only 52-meeting-prepopulate"
    info "  or, if your institution ships one: ./install.sh --profile <name>"
    return 0 2>/dev/null || exit 0
fi

info "  handoff folder: $HANDOFF"
mkdir -p "$HANDOFF"

# Assistant/EA exclusion. Any producer that reads a real calendar will
# eventually hand you a meeting where your assistant is a required attendee
# (they scheduled it on your behalf) — without this, a 1:1 they book looks
# like a "group" meeting because the classifier counts them as a second
# participant. admin_emails tells the consumer to drop them from attendee
# counts and People-stub linking entirely. See docs/Meeting-Pre-Population.md.
CONFIG_DIR="$SCRIPTS/.config"
CONFIG_FILE="$CONFIG_DIR/meeting_prepopulate.json"
admin_emails_csv="$(prompt "  Email(s) of any assistant/EA who schedules meetings on your behalf (comma-separated, blank for none)" "$(pdefault ADMIN_EMAILS)")"
mkdir -p "$CONFIG_DIR"
python3 - "$CONFIG_FILE" "$admin_emails_csv" <<'PYEOF'
import json, sys
from pathlib import Path

config_path = Path(sys.argv[1])
emails = [e.strip() for e in sys.argv[2].split(",") if e.strip()]

config = {}
if config_path.exists():
    try:
        config = json.loads(config_path.read_text())
    except json.JSONDecodeError:
        config = {}
config.setdefault("treat_start_as_utc", False)
if emails:
    existing = set(config.get("admin_emails", []))
    config["admin_emails"] = sorted(existing | set(emails))
else:
    config.setdefault("admin_emails", [])

config_path.write_text(json.dumps(config, indent=2) + "\n")
PYEOF
if [[ -n "$admin_emails_csv" ]]; then
    ok "  admin_emails set: $admin_emails_csv"
else
    info "  no assistant/EA email(s) given — add them later to $CONFIG_FILE if needed"
fi

# Render the plist: substitute the username.
mkdir -p "$HOME/Library/LaunchAgents"
tmp="$(mktemp -t meeting_prepop).plist"
cp "$PLIST_SRC" "$tmp"
sed -i '' "s|YOUR_USERNAME|$USER|g" "$tmp"
if grep -q "YOUR_USERNAME" "$tmp"; then
    err "  plist substitution incomplete; not installing"
    rm -f "$tmp"
    return 0 2>/dev/null || exit 0
fi
install -m 0644 "$tmp" "$DST"
rm -f "$tmp"
ok "  rendered: $DST"
launchctl_reload "$DST"
ok "  loaded LaunchAgent: $LABEL"

echo
warn "  ONE manual step remains before this actually does anything:"
info "  Run a PRODUCER that writes the handoff contract into:"
info "       $HANDOFF"
info "     Recommended: a Claude Code session with an MCP connector to your"
info "     calendar system (no relay needed) - docs/Meeting-Handoff-MCP-Producer.md"
info "     Otherwise, write your own producer and deliver it directly or via a"
info "     relay - see docs/Azure-Blob-Handoff-Relay.md for a worked Azure Blob"
info "     Storage relay. Full JSON contract: docs/Meeting-Pre-Population.md"

ok "meeting-prepopulate component complete"
