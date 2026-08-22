#!/usr/bin/env bash
# 30-plugins.sh - install the 13 community plugins from PINNED releases.
#
# Every plugin comes from installers/plugin-pins.json: exact release tag,
# exact URL, SHA256 verified before install (see installers/lib/plugins.sh).
# Idempotent: re-running reinstalls the same pinned bytes. Upgrading is a
# deliberate maintainer act: python3 installers/lib/pin_plugins.py, review
# the diff, commit.
#
# User still needs to open Obsidian once and click "Trust author and
# enable plugins" on first launch.

set -euo pipefail

source "$REPO_ROOT/installers/lib/plugins.sh"

VAULT="$HOME/Obsidian"
info "Fetching community plugins into $VAULT/.obsidian/plugins/..."
fetch_all_plugins "$VAULT"

ok "plugins component complete"
info "  Next: open Obsidian, click 'Trust author and enable plugins' when prompted"
