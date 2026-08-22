#!/usr/bin/env bash
# 40-tagger.sh - install the semantic auto-tagger LaunchAgent.
#
# Source script and plist are already in the repo at Templates/Scripts/.
# This component just renders the plist into ~/Library/LaunchAgents/ and
# loads it. The script uses the per-vault venv (created in 10-vault-
# bootstrap.sh) so it picks up anthropic + pyyaml + python-dotenv.

set -euo pipefail

source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"

info "Verifying tag_clippings.py is in place..."
if [[ ! -x "$VAULT/Templates/Scripts/tag_clippings.py" ]]; then
    err "  $VAULT/Templates/Scripts/tag_clippings.py missing"
    exit 1
fi
ok "  tag_clippings.py at $VAULT/Templates/Scripts/"

info "Installing LaunchAgent com.tag-clippings..."
install_plist_and_load Templates/Scripts/com.tag-clippings.plist com.tag-clippings

ok "tagger component complete"
info "  Log: ~/Library/Logs/tag-clippings.log"
info "  Optional: create ~/Obsidian/Knowledge/Tag Taxonomy.md to lock the tag space"
