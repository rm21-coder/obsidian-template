#!/usr/bin/env bash
# 59-email-tasks.sh - OPT-IN email-to-tasks.
#
# Each weekday morning a confined headless Claude session reads the mail
# received since the last run (mail search and item read through the
# Microsoft 365 connector, nothing else) and email_tasks.py appends the
# requests waiting on you as #task lines to Actions/Email Tasks.md.
#
# OFF by default. It depends on what an installer cannot provision: the
# Claude Code CLI, authenticated, with a Microsoft 365 connector your tenant
# has approved. Full design and limits: docs/Email-Tasks.md

set -euo pipefail

source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"
SCRIPTS="$VAULT/Templates/Scripts"
RUNNER="$SCRIPTS/email_tasks.py"
PLIST_SRC="$SCRIPTS/com.obsidian.email-tasks.plist"
LABEL="com.obsidian.email-tasks"
CONFIG_DIR="$SCRIPTS/.config"
CONFIG_FILE="$CONFIG_DIR/email_tasks.json"
PULL_CONFIG="$CONFIG_DIR/meeting_pull.json"

for required in "$RUNNER" "$SCRIPTS/email_task_transform.py" "$SCRIPTS/email_tasks_prompt.txt" "$PLIST_SRC"; do
    if [[ ! -f "$required" ]]; then
        warn "  $required not found; skipping email tasks"
        return 0 2>/dev/null || exit 0
    fi
done

if ! pconfirm EMAIL_TASKS \
        "Set up email-to-tasks? (a Claude session reads your inbox each weekday; needs the Claude CLI + the Microsoft 365 connector)" N; then
    info "  skipped; enable later with: ./install.sh --only 59-email-tasks"
    return 0 2>/dev/null || exit 0
fi

if ! has_cmd claude && [[ ! -x "$HOME/.local/bin/claude" ]] && [[ ! -x "$HOME/.claude/local/claude" ]]; then
    warn "  Claude CLI not found on PATH or in the usual user-level locations"
    info "     install it, then verify the connector with: claude mcp list"
fi

# The meeting pull already asked who you are; reuse its answers as defaults.
pull_get() {
    [[ -f "$PULL_CONFIG" ]] || return 0
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' \
        "$PULL_CONFIG" "$1" 2>/dev/null || true
}
display_name="$(prompt "  Your display name" "$(pdefault DISPLAY_NAME "$(pull_get display_name)")")"
email="$(prompt "  Your work email (the mailbox to read)" "$(pdefault EMAIL "$(pull_get email)")")"
if [[ -z "$email" || -z "$display_name" ]]; then
    err "  a name and an email are required; skipping email tasks"
    return 0 2>/dev/null || exit 0
fi
mcp_prefix="$(pdefault MCP_PREFIX "$(pull_get mcp_prefix)")"
[[ "$mcp_prefix" =~ ^mcp__[A-Za-z0-9_-]*[A-Za-z0-9-]$ ]] || mcp_prefix="mcp__claude_ai_Microsoft_365"

mkdir -p "$CONFIG_DIR"
python3 - "$CONFIG_FILE" "$display_name" "$email" "$mcp_prefix" <<'PYEOF'
import json, sys
from pathlib import Path

path, display_name, email, mcp_prefix = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
config = {}
if path.exists():
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError:
        config = {}
config.update({"display_name": display_name, "email": email, "mcp_prefix": mcp_prefix})
# Preserve the user's own tuning on re-runs.
for key, value in (("other_addresses", []), ("note", "Actions/Email Tasks.md"),
                   ("team_ask_max_recipients", 10), ("max_lookback_hours", 72),
                   ("high_priority_senders", []), ("title_priority", False)):
    config.setdefault(key, value)
path.write_text(json.dumps(config, indent=2) + "\n")
PYEOF
ok "  wrote: $CONFIG_FILE"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
install_plist_and_load "Templates/Scripts/com.obsidian.email-tasks.plist" "$LABEL"
ok "  loaded LaunchAgent: $LABEL (weekdays 06:45)"

echo
warn "  Validate it by hand before trusting the 06:45 run:"
info "     /usr/bin/python3 $RUNNER --dry-run   # renders the prompt, calls nothing"
info "     /usr/bin/python3 $RUNNER             # real run; appends to Actions/Email Tasks.md"

ok "email-tasks component complete"
