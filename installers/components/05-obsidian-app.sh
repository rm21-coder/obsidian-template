#!/usr/bin/env bash
# 05-obsidian-app.sh - install Obsidian.app via Homebrew cask.
#
# The rest of the installer sets up the vault content, scripts, plugins,
# and agents - but Obsidian.app itself (the desktop client) is a separate
# install. Without it the user has a vault but no way to view/edit it.
#
# Runs after 00-preflight (needs brew) and before 10-vault-bootstrap (no
# strict ordering required, but installing Obsidian first means it's ready
# when the install completes and the orchestrator prints "Open Obsidian").
#
# Idempotent: detects existing Obsidian.app in /Applications or
# ~/Applications and skips. Uses brew_install_if_missing's verify_target
# so a brew exit-1 with the .app on disk is treated as success.

set -euo pipefail

info "Checking for Obsidian.app..."
if [[ -e "/Applications/Obsidian.app" ]]; then
    ok "  Obsidian.app already at /Applications/Obsidian.app"
    return 0 2>/dev/null || exit 0
fi
if [[ -e "$HOME/Applications/Obsidian.app" ]]; then
    ok "  Obsidian.app already at $HOME/Applications/Obsidian.app"
    return 0 2>/dev/null || exit 0
fi

info "Installing Obsidian via Homebrew cask..."
brew_install_if_missing obsidian cask Obsidian.app

ok "obsidian-app component complete"
info "  Open from Spotlight (Cmd-Space, type 'Obsidian')"
info "  On first launch, point it at the vault at: $HOME/Obsidian"
info "  Then click 'Trust author and enable plugins' when prompted"
