#!/usr/bin/env bash
# 50-llm-rag.sh - Ollama + 8B model + Open WebUI (Docker) + RAG sync agent.
#
# Reuses Templates/Scripts/setup-ollama.sh for Ollama + 8B model (passes
# --minimal so it skips the 14B and 70B downloads). Then installs Docker
# Desktop if missing, pulls Open WebUI, and configures it to talk to the
# local Ollama on port 11434. Finally wires the obsidian-rag-sync.py
# LaunchAgent to push the vault into a Knowledge collection nightly.
#
# After this component finishes, the user must (one-time, manual):
#   1. Open Open WebUI in a browser at http://localhost:3000
#   2. Create an admin account
#   3. Create a Knowledge collection named "Obsidian"
#   4. Generate an API key in user settings
#   5. Re-run: install.sh --only 20-secrets
#      to populate OPEN_WEBUI_API_KEY and OBSIDIAN_COLLECTION_ID

set -euo pipefail
source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"

# ---- 1. Ollama + 8B model --------------------------------------------------
info "Running setup-ollama.sh --minimal..."
bash "$VAULT/Templates/Scripts/setup-ollama.sh" --minimal

# ---- 2. Docker Desktop -----------------------------------------------------
info "Docker Desktop..."
if has_cmd docker && docker info >/dev/null 2>&1; then
    ok "  docker running"
else
    if ! has_cmd docker; then
        info "  installing Docker Desktop via Homebrew cask..."
        brew_install_if_missing docker cask docker
        warn "  Docker Desktop installed. Launch it once (Applications -> Docker) so it can request privileged-helper permissions, then re-run this component."
        warn "  If brew complained about leftover binaries in /usr/local/bin (docker, hub-tool, kubectl.docker, docker-credential-*), 'sudo rm' them and re-run."
        return 0 2>/dev/null || exit 0
    fi
    warn "  docker daemon not running; start Docker Desktop, then re-run this component"
    return 0 2>/dev/null || exit 0
fi

# ---- 3. Open WebUI ---------------------------------------------------------
info "Open WebUI container..."
if docker ps --format '{{.Names}}' | grep -q '^open-webui$'; then
    ok "  open-webui container already running"
else
    info "  pulling and starting open-webui..."
    # 127.0.0.1 binding is load-bearing: this UI can hold the entire vault
    # as a RAG collection, and a bare -p 3000:8080 publishes it to every
    # device on the LAN (Docker bypasses the macOS application firewall).
    docker run -d --restart=always \
        -p 127.0.0.1:3000:8080 \
        -v open-webui:/app/backend/data \
        --add-host=host.docker.internal:host-gateway \
        -e 'OLLAMA_BASE_URL=http://host.docker.internal:11434' \
        --name open-webui \
        ghcr.io/open-webui/open-webui:main
    ok "  open-webui container started; web UI at http://localhost:3000"
fi

# ---- 4. RAG sync LaunchAgent ----------------------------------------------
info "Installing LaunchAgent com.obsidian-rag-sync..."
install_plist_and_load Templates/Scripts/com.obsidian-rag-sync.plist com.obsidian-rag-sync

ok "llm-rag component complete"
info "  Next (one-time manual): open http://localhost:3000, create admin,"
info "  create 'Obsidian' Knowledge collection, generate API key, then run:"
info "    ./install.sh --only 20-secrets --force"
info "  to populate OPEN_WEBUI_API_KEY and OBSIDIAN_COLLECTION_ID in ~/dev/secrets/.env"
