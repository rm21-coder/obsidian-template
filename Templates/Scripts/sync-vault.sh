#!/usr/bin/env bash
# sync-vault.sh — wrapper for obsidian-rag-sync.py
#
# Loads OPEN_WEBUI_API_KEY and OBSIDIAN_COLLECTION_ID from ~/dev/secrets/.env
# (matching the project-wide secrets convention) and execs the Python indexer.
set -euo pipefail

SECRETS="$HOME/dev/secrets/.env"
if [[ ! -r "$SECRETS" ]]; then
    echo "sync-vault.sh: cannot read $SECRETS" >&2
    exit 1
fi

# auto-export every var defined while sourcing
set -a
# shellcheck disable=SC1090
source "$SECRETS"
set +a

export OBSIDIAN_VAULT="${OBSIDIAN_VAULT:-$HOME/Obsidian}"
export OPEN_WEBUI_URL="${OPEN_WEBUI_URL:-http://localhost:3000}"

exec "$HOME/Obsidian/Templates/Scripts/obsidian-rag-sync.py" "$@"
