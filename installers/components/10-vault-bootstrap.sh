#!/usr/bin/env bash
# 10-vault-bootstrap.sh - confirm the vault is in place.
#
# This template repo IS the vault. If the user followed the README quick-
# start, they cloned the repo into ~/Obsidian/, so $REPO_ROOT == ~/Obsidian/.
# We verify that and bail loudly if not - the rest of the installer assumes
# the vault lives at ~/Obsidian.
#
# We also create the per-vault Python venv at Templates/Scripts/.venv/ that
# the tagger, voice-cleanup, RAG-sync, and clip-article agents all use.

set -euo pipefail

VAULT="$HOME/Obsidian"

info "Verifying vault location..."
if [[ "$REPO_ROOT" != "$VAULT" ]]; then
    warn "  This repo is at $REPO_ROOT, but agents expect the vault at $VAULT."
    warn "  Either re-clone into ~/Obsidian/, or symlink:"
    warn "    ln -s '$REPO_ROOT' '$VAULT'"
    if [[ "${INTERACTIVE:-1}" -eq 1 ]]; then
        if confirm "Create the symlink now?" Y; then
            ln -sfn "$REPO_ROOT" "$VAULT"
            ok "  symlinked $VAULT -> $REPO_ROOT"
        else
            err "  cannot proceed without vault at $VAULT"
            exit 1
        fi
    else
        err "  vault not at $VAULT; pass --interactive or fix manually"
        exit 1
    fi
else
    ok "  vault at $VAULT"
fi

info "Verifying required vault folders..."
REQUIRED=(Actions Categories Clippings Creations Daily Excalidraw Groups Knowledge Meetings Notes People Templates Topics Z_archive Z_attachments)
for d in "${REQUIRED[@]}"; do
    if [[ -d "$VAULT/$d" ]]; then
        ok "  $d/"
    else
        warn "  $d/ missing - creating"
        mkdir -p "$VAULT/$d"
    fi
done

info "Setting up per-vault Python venv at Templates/Scripts/.venv/..."
VENV="$VAULT/Templates/Scripts/.venv"
VENV_PY="$VENV/bin/python3"
if [[ -x "$VENV_PY" ]]; then
    ok "  venv already exists: $VENV"
else
    # Prefer Homebrew python3.13; fall back to system python3.
    PY=""
    for candidate in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3 /usr/local/bin/python3.13 /usr/local/bin/python3 /usr/bin/python3; do
        [[ -x "$candidate" ]] && PY="$candidate" && break
    done
    if [[ -z "$PY" ]]; then
        err "  no usable python3 found for venv creation"
        exit 1
    fi
    info "  using $PY"
    "$PY" -m venv "$VENV"
    ok "  created: $VENV"
fi

info "Installing requirements.txt into the venv..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install -r "$VAULT/Templates/Scripts/requirements.txt"
ok "  requirements installed"
