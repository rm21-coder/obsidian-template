#!/usr/bin/env bash
# 50-llm-rag.sh - Ollama + 8B model + Open WebUI (Docker) + RAG sync agent.
#
# Reuses Templates/Scripts/setup-ollama.sh for Ollama + 8B model (passes
# --minimal so it skips the 14B and 70B downloads). Then installs Docker
# Desktop if missing, pulls Open WebUI, and configures it to talk to the
# local Ollama on port 11434. Finally wires the obsidian-rag-sync.py
# LaunchAgent to push the vault into a Knowledge collection nightly.
#
# On a fresh volume the admin account is created headlessly (a random
# password, stored in the Keychain as open_webui_admin_password), so there is
# no first-signup race for a web page to win. After this component finishes,
# the user must (one-time, manual):
#   1. Sign in to Open WebUI at http://localhost:3000 as that admin
#   2. Create a Knowledge collection named "Obsidian"
#   3. Generate an API key in user settings
#   4. Re-run: install.sh --only 20-secrets
#      to populate OPEN_WEBUI_API_KEY and OBSIDIAN_COLLECTION_ID

set -euo pipefail
source "$REPO_ROOT/installers/lib/plist.sh"
source "$REPO_ROOT/installers/lib/secrets.sh"

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
# 127.0.0.1 binding is load-bearing: this UI can hold the entire vault as a
# RAG collection, and a bare -p 3000:8080 publishes it to every device on the
# LAN (Docker bypasses the macOS application firewall). An existing container
# keeps whatever binding it was created with, so it is checked, not trusted,
# and recreated on the same volume if it is published anywhere else.
# The image is pinned by digest (installers/rag-pins.json, rag_pins.py): Docker
# resolves the digest, so a re-pushed tag or a swapped image is refused.
OPEN_WEBUI_IMAGE="$(python3 "$REPO_ROOT/installers/lib/rag_pins.py" image)"
OPEN_WEBUI_DIGEST="$(python3 "$REPO_ROOT/installers/lib/rag_pins.py" image-digest)"
RAG_PINS=(python3 "$REPO_ROOT/installers/lib/rag_pins.py")
# Loopback is not private from the browser (finding 5268557 / N1): any page
# the operator opens can send requests to 127.0.0.1:3000, and DNS rebinding
# gets past a same-origin check. So the container runs with login required
# (WEBUI_AUTH), CORS held to the two loopback origins instead of the `*`
# default, and signup closed. Auth is the control that matters; CORS narrows
# what a cross-origin page can read. ENABLE_SIGNUP is a persistent setting:
# it seeds the database on first start, and the Admin Panel value wins after.
OPEN_WEBUI_ADMIN_EMAIL="${OPEN_WEBUI_ADMIN_EMAIL:-admin@example.com}"
OPEN_WEBUI_SIGNUP="${OPEN_WEBUI_SIGNUP:-False}"
# start_open_webui [env-file]: the env-file carries the headless admin's
# credentials on a fresh volume only, so the password is never in argv.
start_open_webui() {
    local env_file_args=()
    [[ -n "${1:-}" ]] && env_file_args=(--env-file "$1")
    docker run -d --restart=always \
        -p 127.0.0.1:3000:8080 \
        -v open-webui:/app/backend/data \
        --add-host=host.docker.internal:host-gateway \
        -e 'OLLAMA_BASE_URL=http://host.docker.internal:11434' \
        -e 'WEBUI_AUTH=True' \
        -e 'CORS_ALLOW_ORIGIN=http://localhost:3000;http://127.0.0.1:3000' \
        -e "ENABLE_SIGNUP=$OPEN_WEBUI_SIGNUP" \
        ${env_file_args[@]+"${env_file_args[@]}"} \
        --name open-webui \
        "$OPEN_WEBUI_IMAGE"
}
# First start on a fresh volume: create the admin from WEBUI_ADMIN_EMAIL /
# WEBUI_ADMIN_PASSWORD / WEBUI_ADMIN_NAME (Open WebUI's headless first-admin
# variables), with signup closed from the first second. The password is
# random, goes to the Keychain through secret_store.py (stdin, never argv),
# and reaches Docker in a 0600 env-file deleted straight after `docker run`.
# A sign-in then proves the account exists, and the container is recreated
# without the env-file so the password does not stay in `docker inspect`.
# If the release ignores those variables, signup is reopened for a hand-made
# admin, and the operator is told how to close it again.
start_open_webui_fresh() {
    local admin_pw env_file
    admin_pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    if ! _secret_store_set OPEN_WEBUI_ADMIN_PASSWORD "$admin_pw"; then
        admin_pw=""
        warn "  the Keychain is unavailable, so the admin cannot be created headlessly"
    fi
    if [[ -n "$admin_pw" ]]; then
        env_file="$(umask 077 && mktemp "${TMPDIR:-/tmp}/open-webui-admin.XXXXXX")"
        chmod 600 "$env_file"
        # shellcheck disable=SC2064  # expand now: the path is local
        trap "rm -f '$env_file'" EXIT
        printf 'WEBUI_ADMIN_EMAIL=%s\nWEBUI_ADMIN_PASSWORD=%s\nWEBUI_ADMIN_NAME=Admin\n' \
            "$OPEN_WEBUI_ADMIN_EMAIL" "$admin_pw" > "$env_file"
        start_open_webui "$env_file"
        rm -f "$env_file"
        info "  waiting for Open WebUI's first start (database migration, about a minute)..."
        if printf '%s\n%s\n' "$OPEN_WEBUI_ADMIN_EMAIL" "$admin_pw" \
                | "${RAG_PINS[@]}" webui-signin --wait 240; then
            admin_pw=""
            docker rm -f open-webui >/dev/null
            start_open_webui
            ok "  admin created: $OPEN_WEBUI_ADMIN_EMAIL; password in the Keychain (open_webui_admin_password)"
            info "    read it with: python3 Templates/Scripts/secret_store.py get OPEN_WEBUI_ADMIN_PASSWORD | pbcopy"
            return 0
        fi
        admin_pw=""
        docker rm -f open-webui >/dev/null
    fi
    # Fallback: open signup for the one hand-made admin. Loud, because until
    # that account exists the first page to reach port 3000 can take it.
    OPEN_WEBUI_SIGNUP=True
    start_open_webui
    OPEN_WEBUI_SIGNUP=False
    err "  open-webui could not create its admin headlessly, so SIGNUP IS OPEN."
    err "  Create the admin at http://localhost:3000 NOW, before opening other pages,"
    err "  then close signup (the volume keeps the account):"
    err "    docker rm -f open-webui && ./install.sh --only 50-llm-rag"
    err "  and in Admin Panel -> Settings -> General, turn 'Enable New Sign Ups' off."
    info "  (open_webui_admin_password in the Keychain is kept, in case the account was"
    info "   created but answered late; it does not apply to an admin you make by hand.)"
}
# Report, do not replace: an existing container keeps the environment it was
# created with, and the volume holds the account. 5268557 / N1.
report_open_webui_hardening() {
    local findings state
    findings="$(docker inspect open-webui 2>/dev/null | "${RAG_PINS[@]}" check-container || true)"
    if [[ -n "$findings" ]]; then
        while IFS= read -r line; do warn "  open-webui: $line"; done <<< "$findings"
        warn "  recreate it with the hardened settings, on the same volume (account and knowledge kept):"
        warn "    docker rm -f open-webui && ./install.sh --only 50-llm-rag"
        if [[ "$findings" == *WEBUI_AUTH=* ]]; then
            warn "  With auth off, Open WebUI signs everyone in as one built-in admin; once auth is"
            warn "  back on, give that account a strong password at once, in"
            warn "  Settings -> Account -> Change Password -> Update password (never the Admin Panel user editor)."
        fi
    else
        ok "  open-webui requires login and limits CORS to the loopback origins"
    fi
    state="$("${RAG_PINS[@]}" webui-state --wait 120 2>/dev/null || true)"
    case "$state" in
        OK) ok "  open-webui signup is closed" ;;
        NO-ADMIN) err "  open-webui has NO admin yet and signup decides who gets it; create it now at http://localhost:3000" ;;
        SIGNUP-OPEN) warn "  open-webui signup is OPEN: Admin Panel -> Settings -> General -> 'Enable New Sign Ups' off" ;;
        AUTH-OFF) warn "  open-webui reports login disabled (WEBUI_AUTH off)" ;;
        *) warn "  could not read open-webui's signup state (not reachable yet?); re-run this component to check" ;;
    esac
    report_open_webui_default_password
}
# N10: an account still on the password "admin" (created while sign-in was
# off, or reset by the admin user editor's Save). Checked in the container.
report_open_webui_default_password() {
    local weak rc=0
    weak="$("${RAG_PINS[@]}" default-password 2>/dev/null)" || rc=$?
    case "$rc" in
        0) ok "  open-webui: no account uses the default password" ;;
        1) while IFS= read -r line; do err "  open-webui: $line"; done <<< "$weak"
           err "  change it now in Settings -> Account -> Change Password -> Update password;"
           err "  the Admin Panel user editor's Save writes its password field back, and the profile Save changes nothing." ;;
        *) warn "  could not check open-webui's account passwords (container not running?)" ;;
    esac
}
# An existing container keeps the image it was created from. Report, do not
# replace: moving Open WebUI to another release migrates its database, which
# is the operator's call, and the volume keeps the account and knowledge.
report_open_webui_pin() {
    local image_id repo_digests
    image_id="$(docker inspect open-webui --format '{{.Image}}' 2>/dev/null || true)"
    repo_digests="$(docker image inspect "$image_id" --format '{{join .RepoDigests " "}}' 2>/dev/null || true)"
    if [[ "$repo_digests" == *"@$OPEN_WEBUI_DIGEST"* ]]; then
        ok "  open-webui runs the pinned image"
    else
        warn "  open-webui runs an image other than the pinned one ($OPEN_WEBUI_IMAGE)."
        warn "  To move to the pin, on the same volume (account and knowledge kept):"
        warn "    docker pull '$OPEN_WEBUI_IMAGE' && docker rm -f open-webui && ./install.sh --only 50-llm-rag"
    fi
}
if docker ps -a --format '{{.Names}}' | grep -q '^open-webui$'; then
    if ! docker ps --format '{{.Names}}' | grep -q '^open-webui$'; then
        docker start open-webui >/dev/null && sleep 2
    fi
    exposed="$(docker port open-webui 8080 2>/dev/null | grep -v '^127\.0\.0\.1:' || true)"
    if [[ -n "$exposed" ]]; then
        warn "  open-webui is published beyond this Mac ($(echo "$exposed" | tr '\n' ' ')); recreating it on the same volume, bound to 127.0.0.1"
        docker rm -f open-webui >/dev/null
        start_open_webui
        ok "  open-webui recreated; your account and knowledge are kept"
    else
        ok "  open-webui container running, bound to 127.0.0.1"
    fi
    report_open_webui_pin
    report_open_webui_hardening
elif docker volume inspect open-webui >/dev/null 2>&1; then
    # A volume without a container (removed to re-pin or harden): the account
    # is on the volume, so start it plainly, with no new admin password.
    info "  starting open-webui on the existing volume..."
    start_open_webui
    ok "  open-webui container started; web UI at http://localhost:3000"
    report_open_webui_hardening
else
    info "  pulling and starting open-webui on a fresh volume..."
    start_open_webui_fresh
    ok "  open-webui container started; web UI at http://localhost:3000"
    report_open_webui_hardening
fi

# ---- 4. RAG sync LaunchAgent ----------------------------------------------
info "Installing LaunchAgent com.obsidian-rag-sync..."
install_plist_and_load Templates/Scripts/com.obsidian-rag-sync.plist com.obsidian-rag-sync

ok "llm-rag component complete"
info "  Next (one-time manual): sign in at http://localhost:3000 as $OPEN_WEBUI_ADMIN_EMAIL,"
info "  create 'Obsidian' Knowledge collection, generate API key, then run:"
info "    ./install.sh --only 20-secrets --force"
info "  to populate OPEN_WEBUI_API_KEY and OBSIDIAN_COLLECTION_ID in ~/dev/secrets/.env"
