#!/usr/bin/env bash
# install-git-hooks.sh — install the pre-commit and pre-push hooks into the
# current clone of the obsidian-template repo.
#
# Run this once after cloning, if you intend to commit to the repo:
#     ./installers/install-git-hooks.sh
#
# Re-running is safe (it overwrites the existing hooks).
#
# To uninstall:
#     rm "$(git rev-parse --show-toplevel)/.git/hooks/"{pre-commit,pre-push}

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOKS_DIR="$(git rev-parse --git-path hooks)"

for hook in pre-commit pre-push; do
    src="$REPO_ROOT/installers/lib/hooks/$hook"
    if [[ ! -f "$src" ]]; then
        echo "error: hook source not found at $src" >&2
        exit 1
    fi
    cp "$src" "$HOOKS_DIR/$hook"
    chmod +x "$HOOKS_DIR/$hook"
    echo "Installed $hook hook at: $HOOKS_DIR/$hook"
done
echo ""
echo "From now on, 'git commit' will refuse:"
echo "  - any .md file in an audited folder (Knowledge/, Meetings/, People/,"
echo "    etc.) that lacks 'classification: public' in its frontmatter"
echo "  - staged content containing a real tenant domain, colleague name, or"
echo "    real-looking email address"
echo "  - a committed credential (gitleaks) or a shell script that will not parse"
echo ""
echo "And 'git push' will run the full security suite (~40s: pip-audit, semgrep,"
echo "bandit, shellcheck, gitleaks, dynamic checks) and refuse on any FAIL."
echo ""
echo "Override with --no-verify on either command if needed."
echo ""
echo "NEXT STEP -- the identity gate needs a local deny-list of the values you"
echo "are protecting. It is gitignored, because committing the list would"
echo "publish exactly what it guards. Build it from this machine with:"
echo "  installers/lib/check_identity_leak.py --init"
echo "Until you do, only the generic email-address rule runs."
