#!/usr/bin/env bash
# 54-meeting-pull.sh - OPT-IN meeting pull (the PRODUCER side).
#
# Wires up the producer LaunchAgent that fetches your calendar each weekday
# morning through an MCP connector and drops a schema-v1 handoff into the
# folder component 52 (the consumer) watches. Together they turn "my calendar"
# into "today's meeting notes, already written" with no manual step.
#
# OFF by default, and pointless without 52-meeting-prepopulate: this half only
# produces the handoff, it never writes notes. It also depends on something an
# installer cannot provision for you - the Claude Code CLI, authenticated,
# with an MCP calendar connector your tenant has approved.
#
# Full setup, the two failure modes that are easy to misdiagnose, and how to
# drive a non-Microsoft connector:
#   docs/Meeting-Handoff-MCP-Producer.md

set -euo pipefail

VAULT="$HOME/Obsidian"
SCRIPTS="$VAULT/Templates/Scripts"
RUNNER="$SCRIPTS/meeting_pull.py"
PROMPT="$SCRIPTS/meeting_pull_prompt.txt"
PLIST_SRC="$SCRIPTS/com.obsidian.meeting-pull.plist"
LABEL="com.obsidian.meeting-pull"
CONFIG_DIR="$SCRIPTS/.config"
CONFIG_FILE="$CONFIG_DIR/meeting_pull.json"
HANDOFF="$HOME/MeetingIngest"

for required in "$RUNNER" "$PROMPT" "$PLIST_SRC"; do
    if [[ ! -f "$required" ]]; then
        warn "  $required not found; skipping meeting pull"
        return 0 2>/dev/null || exit 0
    fi
done

# Opt-in gate, same as its consumer half: an install profile can answer it
# (PROFILE_MEETING_PULL=1), otherwise it is asked and defaults to no.
if ! pconfirm MEETING_PULL \
        "Set up the meeting-pull producer? (needs the Claude CLI + an MCP calendar connector)" N; then
    info "  skipped; enable later with: ./install.sh --only 54-meeting-pull"
    info "  or, if your institution ships one: ./install.sh --profile <name>"
    return 0 2>/dev/null || exit 0
fi

# The CLI is a hard dependency but a user-level install, so a missing binary
# here is normal on a fresh machine and worth a warning rather than a failure -
# the config and agent are still worth installing now, ready for it to arrive.
if ! has_cmd claude && [[ ! -x "$HOME/.local/bin/claude" ]] && [[ ! -x "$HOME/.claude/local/claude" ]]; then
    warn "  Claude CLI not found on PATH or in the usual user-level locations"
    info "     install it, then verify the connector with: claude mcp list"
fi

# Identity for the handoff's "user" block. The producer prompt is a template in
# the repo precisely so none of this is ever committed - it lands in .config/,
# which is gitignored.
default_tz="$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||')"
[[ -n "$default_tz" ]] || default_tz="UTC"

display_name="$(prompt "  Your display name, as it should appear in meeting notes" "$(pdefault DISPLAY_NAME "$(id -F 2>/dev/null || echo "$USER")")")"
email="$(prompt "  Your work email (the calendar's owner)" "$(pdefault EMAIL)")"
if [[ -z "$email" ]]; then
    err "  an email is required; skipping meeting pull"
    if [[ "${INTERACTIVE:-1}" -eq 0 ]]; then
        # --auto has no one to ask, and the calendar owner is the one answer a
        # shared profile cannot carry. Name the two ways out rather than
        # leaving a half-installed pipeline (consumer on, producer missing).
        info "     an --auto run needs PROFILE_EMAIL in the profile, or run"
        info "     interactively: ./install.sh --only 54-meeting-pull --profile <name>"
    fi
    return 0 2>/dev/null || exit 0
fi
tenant="$(prompt "  Your tenant/primary domain" "$(pdefault TENANT "${email##*@}")")"
timezone="$(prompt "  Your calendar's timezone (IANA)" "$(pdefault TIMEZONE "$default_tz")")"
# Attendees in these domains are treated as internal colleagues; everyone else
# is external. Multi-domain tenants (a university and its hospital, say) need
# every domain listed or the classification quietly mislabels people.
tenant_domains="$(prompt "  Internal domain(s), comma-separated" "$(pdefault TENANT_DOMAINS "$tenant")")"
# Only worth changing for a non-Microsoft connector; the tool names must match
# what `claude mcp list` exposes, NOT what a desktop client shows.
mcp_prefix="$(prompt "  MCP tool-name prefix" "$(pdefault MCP_PREFIX "mcp__claude_ai_Microsoft_365")")"
search_tool="$(prompt "  Calendar-search tool name" "$(pdefault SEARCH_TOOL "outlook_calendar_search")")"
read_tool="$(prompt "  Resource-read tool name" "$(pdefault READ_TOOL "read_resource")")"
out_dir="$(prompt "  Drop folder the consumer watches" "$(pdefault OUT_DIR "$HANDOFF")")"

mkdir -p "$CONFIG_DIR" "$out_dir"
python3 - "$CONFIG_FILE" "$display_name" "$email" "$tenant" "$timezone" \
    "$tenant_domains" "$mcp_prefix" "$search_tool" "$read_tool" "$out_dir" <<'PYEOF'
import json, sys
from pathlib import Path

(config_path, display_name, email, tenant, timezone,
 tenant_domains, mcp_prefix, search_tool, read_tool, out_dir) = sys.argv[1:11]

path = Path(config_path)
config = {}
if path.exists():
    try:
        config = json.loads(path.read_text())
    except json.JSONDecodeError:
        config = {}

config.update({
    "display_name": display_name,
    "email": email,
    "tenant": tenant,
    "timezone": timezone,
    "tenant_domains": [d.strip() for d in tenant_domains.split(",") if d.strip()],
    "mcp_prefix": mcp_prefix,
    "search_tool": search_tool,
    "read_tool": read_tool,
    "out_dir": out_dir,
})
# Producer selection: "claude" (default, zero API setup) or "graph"
# (direct Microsoft Graph — no LLM in the loop; see
# docs/Meeting-Handoff-MCP-Producer.md, "Skipping the LLM entirely").
# Preserve an existing explicit choice on re-runs.
config.setdefault("producer", "claude")
path.write_text(json.dumps(config, indent=2) + "\n")
PYEOF
ok "  wrote: $CONFIG_FILE"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
install_plist_and_load "Templates/Scripts/com.obsidian.meeting-pull.plist" "$LABEL"
ok "  loaded LaunchAgent: $LABEL (weekdays 05:00)"

echo
warn "  Validate it by hand before trusting the 05:00 run:"
info "     python3 $RUNNER --dry-run   # renders the prompt, calls nothing"
info "     python3 $RUNNER             # real run; writes into $out_dir"
info "  A headless run cannot answer a permission prompt, so a connector your"
info "  tenant has not approved fails silently-looking rather than loudly."
info "  See docs/Meeting-Handoff-MCP-Producer.md if the first real run stalls."

ok "meeting-pull component complete"
