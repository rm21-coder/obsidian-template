#!/usr/bin/env bash
#
# setup-ollama.sh
# One-shot Ollama setup for an M5 Max MacBook Pro (128 GB unified memory).
#
# What it does:
#   1. Installs Ollama via Homebrew cask if not already installed
#   2. Adds OLLAMA_FLASH_ATTENTION and OLLAMA_KV_CACHE_TYPE to your shell profile
#   3. Optionally raises the Metal wired-memory cap (runtime + persistent via LaunchDaemon)
#   4. Pulls a sensible starter set of models
#
# Safe to re-run — each step checks before acting.
#
# Usage:
#   chmod +x setup-ollama.sh
#   ./setup-ollama.sh
#
# Options:
#   --skip-wired-limit   Don't touch iogpu.wired_limit_mb (keep macOS default ~75%)
#   --skip-models        Don't pull any models
#   --minimal            Only pull the 8B model (skip 70B and 14B)

set -euo pipefail

# ---------- pretty output ----------
BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
info()  { printf "%s==>%s %s\n" "$BOLD" "$RESET" "$*"; }
ok()    { printf "%s✓%s %s\n" "$GREEN" "$RESET" "$*"; }
warn()  { printf "%s!%s %s\n" "$YELLOW" "$RESET" "$*"; }
err()   { printf "%s✗%s %s\n" "$RED" "$RESET" "$*" >&2; }

# ---------- args ----------
SKIP_WIRED=0
SKIP_MODELS=0
MINIMAL=0
for arg in "$@"; do
    case "$arg" in
        --skip-wired-limit) SKIP_WIRED=1 ;;
        --skip-models)      SKIP_MODELS=1 ;;
        --minimal)          MINIMAL=1 ;;
        -h|--help)
            sed -n '2,22p' "$0"; exit 0 ;;
        *) err "Unknown arg: $arg"; exit 1 ;;
    esac
done

# ---------- sanity checks ----------
if [[ "$(uname)" != "Darwin" ]]; then
    err "This script targets macOS only."
    exit 1
fi

ARCH="$(uname -m)"
if [[ "$ARCH" != "arm64" ]]; then
    warn "Detected arch: $ARCH — script is tuned for Apple Silicon. Continuing anyway."
fi

# ---------- 1. install Ollama ----------
info "Checking for Ollama..."
if command -v ollama >/dev/null 2>&1; then
    ok "Ollama already installed: $(ollama --version 2>/dev/null | head -1)"
else
    if ! command -v brew >/dev/null 2>&1; then
        err "Homebrew not found. Install it from https://brew.sh or download Ollama from https://ollama.com/download"
        exit 1
    fi
    info "Installing Ollama via Homebrew cask (includes menu-bar app)..."
    brew install --cask ollama-app
    ok "Ollama installed."
fi

# Make sure the server is running (cask launches it, but be explicit)
if ! pgrep -x ollama >/dev/null 2>&1; then
    info "Starting Ollama in the background..."
    open -a Ollama || nohup ollama serve >/dev/null 2>&1 &
    sleep 2
fi

# ---------- 2. shell profile env vars ----------
SHELL_NAME="$(basename "${SHELL:-/bin/zsh}")"
case "$SHELL_NAME" in
    zsh)  PROFILE="$HOME/.zshrc" ;;
    bash) PROFILE="$HOME/.bash_profile" ;;
    *)    PROFILE="$HOME/.profile" ;;
esac

info "Ensuring Ollama env vars in $PROFILE ..."
touch "$PROFILE"

add_line_if_missing() {
    local line="$1" file="$2"
    if ! grep -Fqx "$line" "$file"; then
        printf '%s\n' "$line" >> "$file"
        ok "  added: $line"
    else
        printf "  %s(already present)%s %s\n" "$DIM" "$RESET" "$line"
    fi
}

{
    grep -Fq "# --- Ollama tuning (setup-ollama.sh) ---" "$PROFILE" || \
        printf '\n# --- Ollama tuning (setup-ollama.sh) ---\n' >> "$PROFILE"
}
add_line_if_missing 'export OLLAMA_FLASH_ATTENTION=1' "$PROFILE"
add_line_if_missing 'export OLLAMA_KV_CACHE_TYPE=q8_0' "$PROFILE"

# Export for this shell too, so subsequent pulls see them
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE=q8_0

# ---------- 3. wired memory limit ----------
if [[ $SKIP_WIRED -eq 0 ]]; then
    TARGET_MB=122880   # 120 GB — leaves 8 GB for macOS on a 128 GB machine
    CURRENT_MB="$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)"

    info "Metal wired-memory cap: current=${CURRENT_MB} MB, target=${TARGET_MB} MB"
    if [[ "$CURRENT_MB" -ge "$TARGET_MB" ]]; then
        ok "Already at or above target."
    else
        warn "Raising the cap requires sudo. You'll be prompted."
        sudo sysctl -w iogpu.wired_limit_mb=$TARGET_MB
        ok "Runtime cap raised to ${TARGET_MB} MB."

        # Persist across reboots via LaunchDaemon
        PLIST="/Library/LaunchDaemons/com.local.iogpu.wired-limit.plist"
        if [[ ! -f "$PLIST" ]]; then
            info "Installing LaunchDaemon to persist the cap across reboots..."
            sudo tee "$PLIST" >/dev/null <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.local.iogpu.wired-limit</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/sbin/sysctl</string>
        <string>-w</string>
        <string>iogpu.wired_limit_mb=${TARGET_MB}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
PLIST_EOF
            sudo chown root:wheel "$PLIST"
            sudo chmod 644 "$PLIST"
            sudo launchctl load -w "$PLIST" 2>/dev/null || true
            ok "LaunchDaemon installed: $PLIST"
        else
            ok "LaunchDaemon already present."
        fi
    fi
else
    info "Skipping wired-memory cap (--skip-wired-limit)."
fi

# ---------- 4. pull models ----------
if [[ $SKIP_MODELS -eq 1 ]]; then
    info "Skipping model pulls (--skip-models)."
else
    # Fast everyday model
    MODELS=("llama3.1:8b-instruct-q8_0")
    if [[ $MINIMAL -eq 0 ]]; then
        # Mid-tier for stronger reasoning at interactive speed
        MODELS+=("qwen2.5:14b-instruct-q6_K")
        # Flagship for this hardware
        MODELS+=("llama3.3:70b-instruct-q5_K_M")
    fi

    info "Pulling ${#MODELS[@]} model(s). Total size is substantial (~65 GB for the full set)."
    for m in "${MODELS[@]}"; do
        info "  pulling $m ..."
        if ollama pull "$m"; then
            ok "    $m ready"
        else
            err "    failed to pull $m — continuing"
        fi
    done
fi

# ---------- summary ----------
echo
info "Done. Quick checks:"
echo "  ollama list                 # see installed models"
echo "  ollama run llama3.1:8b-instruct-q8_0   # quick smoke test"
if [[ $SKIP_WIRED -eq 0 ]]; then
    echo "  sysctl iogpu.wired_limit_mb # confirm wired cap"
fi
echo
echo "${DIM}Open a new terminal (or 'source $PROFILE') to pick up the env vars.${RESET}"
