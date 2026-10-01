#!/usr/bin/env bash
# 47-podcast.sh - podcast transcription CLI + drop-folder watcher.
#
# Two halves:
#   podcast_transcribe.py  on-demand CLI, any input kind (URL/RSS/Apple/file)
#   podcast_watch.py       com.obsidian.podcast-watch, drains
#                          ~/SourceMedia/PodcastInput/ into Clippings/
#
# Backends are tried in order by podcast_transcribe.py: MLX Whisper (Apple
# Silicon GPU), faster-whisper (CPU), ONNX Runtime. The installer runs on
# Apple Silicon only (00-preflight), so MLX is always the first choice.

set -euo pipefail
source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"
SCRIPTS="$VAULT/Templates/Scripts"
PY="$SCRIPTS/.venv/bin/python3"

for s in podcast_transcribe.py podcast_watch.py; do
    if [[ ! -x "$SCRIPTS/$s" ]]; then
        err "  $SCRIPTS/$s missing"
        exit 1
    fi
done

info "Ensuring ffmpeg is installed..."
brew_install_if_missing ffmpeg "" ffmpeg

# ---- transcription backend -------------------------------------------------
# mlx-whisper is in requirements.lock (Apple Silicon), installed hash-checked
# by 10-vault-bootstrap. Check rather than install: a pip install here would
# fetch it unhashed, and a missing backend means the venv itself is broken.
info "Verifying mlx-whisper is in the per-vault venv..."
if "$PY" -c "import mlx_whisper" 2>/dev/null; then
    ok "  mlx-whisper present"
else
    warn "  mlx-whisper does not import - re-run the installer (10-vault-bootstrap"
    warn "  reinstalls the venv from requirements.lock); installing the watcher anyway,"
    warn "  it will fail until a backend imports"
fi

# ---- drop folder -----------------------------------------------------------
# Ask source_media.py rather than hardcoding: it owns the layout and honours
# legacy locations, so the watcher and the mail transport agree by construction.
info "Ensuring the podcast drop folder exists..."
"$PY" "$SCRIPTS/source_media.py" --apply >/dev/null || {
    err "  source_media.py failed; not installing the watcher"
    exit 1
}
DROP="$("$PY" -c "import sys; sys.path.insert(0, '$SCRIPTS'); import source_media; print(source_media.drop_dir('podcast'))")"

# ---- watcher ---------------------------------------------------------------
info "Installing LaunchAgent com.obsidian.podcast-watch..."
install_plist_and_load Templates/Scripts/com.obsidian.podcast-watch.plist com.obsidian.podcast-watch

ok "podcast component complete"
info "  CLI:   $SCRIPTS/podcast_transcribe.py 'https://example.com/episode.mp3'"
info "  Drop:  $DROP  (audio files, or a .txt/.url holding an episode/RSS/Apple link)"
info "  Log:   ~/Library/Logs/podcast-watch.log (problems: .err)"
info "  First run downloads the transcription model (~1.5GB)"
info "  One episode per 900-sec tick, single-instance locked; processed drops"
info "  move to done/ or failed/ rather than being deleted."
