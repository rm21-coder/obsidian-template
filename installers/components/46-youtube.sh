#!/usr/bin/env bash
# 46-youtube.sh - YouTube summarizer (CLI tool + Share Sheet ready).
#
# Not a LaunchAgent: invoked on-demand from a macOS Shortcut. We just
# ensure yt-dlp is installed (via brew). It needs no credential of its own:
# summarization goes through llm_endpoint, the same endpoint and key the tagger
# uses.

set -euo pipefail

VAULT="$HOME/Obsidian"
SCRIPT="$VAULT/Templates/Scripts/youtube_summarize.py"

if [[ ! -x "$SCRIPT" ]]; then
    err "  $SCRIPT missing"
    exit 1
fi

info "Ensuring yt-dlp is installed..."
brew_install_if_missing yt-dlp "" yt-dlp

# No credential check of its own: the summarizer shares the endpoint the tagger
# already uses, and 90-verify reports whether that resolves.
ENDPOINT_PY="$VAULT/Templates/Scripts/llm_endpoint.py"
VENV_PY="$VAULT/Templates/Scripts/.venv/bin/python3"
[[ -x "$VENV_PY" ]] || VENV_PY="$(command -v python3 || true)"
if [[ -f "$ENDPOINT_PY" && -n "$VENV_PY" ]]; then
    endpoint="$("$VENV_PY" "$ENDPOINT_PY" 2>/dev/null || true)"
    [[ -n "$endpoint" ]] && ok "  summarizes via: $endpoint"
fi

# Make YouTube/ Clippings subfolder for output
mkdir -p "$VAULT/Clippings/YouTube"
ok "  output dir: $VAULT/Clippings/YouTube"

ok "youtube component complete"
info "  CLI: $SCRIPT 'https://www.youtube.com/watch?v=...'"
info "  Shortcut: see docs/LaunchAgents — Setup & Migration.md (Share Sheet section)"
