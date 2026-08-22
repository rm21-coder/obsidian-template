#!/usr/bin/env bash
# 00-preflight.sh - environment sanity checks + Homebrew bootstrap.
#
# Fatal if any required prereq cannot be satisfied. Sourced by install.sh
# so $REPO_ROOT and the lib helpers are already loaded.

set -euo pipefail

info "macOS check..."
if ! is_macos; then
    err "This installer targets macOS only. Detected: $(uname)."
    exit 1
fi
ok "  $(sw_vers -productName) $(sw_vers -productVersion)"

info "Architecture check..."
ARCH="$(uname -m)"
if is_arm64; then
    ok "  $ARCH (Apple Silicon)"
else
    warn "  $ARCH (Intel) - podcast component will be skipped automatically"
fi

info "Disk space check..."
AVAIL_GB="$(df -g "$HOME" | awk 'NR==2 {print $4}')"
if [[ "$AVAIL_GB" -lt 20 ]]; then
    warn "  only ${AVAIL_GB}G free at \$HOME - the LLM RAG component pulls ~5G of models; consider freeing space first"
else
    ok "  ${AVAIL_GB}G free at \$HOME"
fi

info "Homebrew..."
ensure_brew

info "python3..."
if has_cmd python3; then
    ok "  $(python3 --version) at $(command -v python3)"
else
    err "  python3 not found - macOS bundles one; check your PATH"
    exit 1
fi

info "Homebrew python3.13 (for the per-vault venv)..."
if brew list python@3.13 >/dev/null 2>&1 || has_cmd /opt/homebrew/bin/python3.13 || has_cmd /usr/local/bin/python3.13; then
    ok "  Homebrew python3.13 present"
else
    brew_install_if_missing python@3.13
fi

info "git (already required to clone this repo, just confirming)..."
has_cmd git && ok "  $(git --version)" || { err "  git missing"; exit 1; }

info "curl..."
has_cmd curl && ok "  $(curl --version | head -1)" || { err "  curl missing"; exit 1; }

info "Caching sudo (the security component's newsyslog step needs it)..."
sudo -v || warn "  sudo not cached; you may be prompted during component 49-security-controls"

ok "preflight passed"
