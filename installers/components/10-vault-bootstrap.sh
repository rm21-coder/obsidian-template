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
# Resolved, not string-compared: ~/Obsidian is often a symlink to the clone
# (the same test update.sh makes; see vault_is_repo in installers/lib/update.sh).
if [[ "$REPO_ROOT" == "$VAULT" || ( -d "$VAULT" && "$REPO_ROOT" -ef "$VAULT" ) ]]; then
    ok "  vault at $VAULT"
elif [[ -e "$VAULT" && ! -L "$VAULT" ]]; then
    # A real folder (or file) that is not this repo. `ln -sfn` would not
    # replace it: it would put a link to the repo INSIDE it, as
    # ~/Obsidian/<repo-name>, and every agent would then run against the
    # wrong tree. Refuse, and leave the user's folder exactly as it is.
    err "  $VAULT already exists and is not this repo ($REPO_ROOT)."
    err "  Not linking over it: move it aside, or clone the repo into $VAULT."
    exit 1
else
    warn "  This repo is at $REPO_ROOT, but agents expect the vault at $VAULT."
    warn "  Either re-clone into ~/Obsidian/, or symlink:"
    warn "    ln -s '$REPO_ROOT' '$VAULT'"
    if [[ "${INTERACTIVE:-1}" -eq 1 ]]; then
        if confirm "Create the symlink now?" Y; then
            # -n: an existing symlink at $VAULT is replaced, not descended.
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
fi

# Owner-only (5278631). The vault holds every note, the venv, the lock files
# and the jobs' working state; under the default umask a fresh clone is 0755,
# readable by every other account on the Mac. The trailing slash makes chmod
# act on the directory a symlinked ~/Obsidian points to, not on the link.
# Only the root: 0700 there closes the whole tree without rewriting the mode
# of every tracked file.
chmod 700 "$VAULT/"
ok "  vault restricted to its owner (0700)"

info "Verifying required vault folders..."
REQUIRED=(Actions Categories Clippings Creations Daily Groups Knowledge Meetings Notes People Templates Topics Z_archive Z_attachments)
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
install_requirements "$VENV_PY" "$VAULT/Templates/Scripts/requirements.txt"   # shared with update.sh
ok "  requirements installed"
