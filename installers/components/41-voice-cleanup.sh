#!/usr/bin/env bash
# 41-voice-cleanup.sh - voice-note dictation cleanup LaunchAgent.
#
# Optional pipeline. Polls ~/SourceMedia/VoiceInput/ for raw iPhone dictation
# .txt files, polishes them via Claude, writes the result into Creations/.
# The folder is filled by 39-source-mail; this component only drains it.

set -euo pipefail

source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"

if [[ ! -x "$VAULT/Templates/Scripts/voice_cleanup.py" ]]; then
    err "  $VAULT/Templates/Scripts/voice_cleanup.py missing"
    exit 1
fi

# Voice cleanup needs its own YAML config. If the example exists but the
# real config doesn't, copy it over and remind the user to edit.
EXAMPLE="$VAULT/Templates/Scripts/voice_cleanup_config.yaml.example"
CONFIG="$VAULT/Templates/Scripts/voice_cleanup_config.yaml"
if [[ -f "$EXAMPLE" && ! -f "$CONFIG" ]]; then
    cp "$EXAMPLE" "$CONFIG"
    chmod 0600 "$CONFIG"
    warn "  copied $EXAMPLE -> $CONFIG"
    warn "  edit $CONFIG before next launchd run if you want voice cleanup"
fi

info "Installing LaunchAgent com.voice-cleanup..."
install_plist_and_load Templates/Scripts/com.voice-cleanup.plist com.voice-cleanup

ok "voice-cleanup component complete"
info "  Log: ~/Library/Logs/voice-cleanup.log"
