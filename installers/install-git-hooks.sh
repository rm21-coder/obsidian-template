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
echo "From now on, 'git commit' will refuse:"
echo "  - any .md file in an audited folder (Knowledge/, Meetings/, People/,"
echo "    etc.) that lacks 'classification: public' in its frontmatter"
echo "  - staged content containing a real tenant domain, colleague name, or"
echo "    real-looking email address"
echo "  - a committed credential (gitleaks) or a shell script that will not parse"
echo ""
echo "Override with 'git commit --no-verify' if needed."
echo ""
echo "NEXT STEP -- the identity gate needs a local deny-list of the values you"
echo "are protecting. It is gitignored, because committing the list would"
echo "publish exactly what it guards. Build it from this machine with:"
echo "  installers/lib/check_identity_leak.py --init"
echo "Until you do, only the generic email-address rule runs."
