#!/usr/bin/env bash
# install-git-hooks.sh — install the pre-commit classification audit hook
# into the current clone of the obsidian-template repo.
#
# Run this once after cloning, if you intend to commit to the repo:
#     ./installers/install-git-hooks.sh
#
# Re-running is safe (it overwrites the existing hook).
#
# To uninstall:
#     rm "$(git rev-parse --show-toplevel)/.git/hooks/pre-commit"

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_SRC="$REPO_ROOT/installers/lib/hooks/pre-commit"
HOOK_DEST="$REPO_ROOT/.git/hooks/pre-commit"

if [[ ! -f "$HOOK_SRC" ]]; then
    echo "error: hook source not found at $HOOK_SRC" >&2
    exit 1
fi

cp "$HOOK_SRC" "$HOOK_DEST"
chmod +x "$HOOK_DEST"

echo "Installed pre-commit hook at:"
echo "  $HOOK_DEST"
echo ""
echo "From now on, 'git commit' will refuse any .md file in an audited folder"
echo "(Knowledge/, Meetings/, People/, etc.) that lacks 'classification: public'"
echo "in its frontmatter. Override with 'git commit --no-verify' if needed."
