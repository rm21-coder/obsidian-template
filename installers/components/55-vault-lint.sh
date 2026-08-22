#!/usr/bin/env bash
# 55-vault-lint.sh - weekly vault content lint.
#
# Installs com.obsidian.vault-lint, which runs vault_lint.py Mondays at 07:00
# and writes findings to ~/Library/Logs/vault-lint.log: duplicate and malformed
# tags, drift against your tag taxonomy, near-duplicate notes, frontmatter
# schema gaps, broken wikilinks, and divergence between the vault's
# Templates/Scripts and your clone of this repo.
#
# The scheduled run is READ-ONLY. It never passes a fixing flag; you read the
# report and run the fixers by hand. That is deliberate - the fixable checks
# rewrite frontmatter, and an unattended weekly job is the wrong place to do
# that silently.
#
# Stdlib-only; runs via /usr/bin/python3.
#
# Full reference: docs/Vault-Lint.md

set -euo pipefail
source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"
SCRIPT="$VAULT/Templates/Scripts/vault_lint.py"
TAXONOMY="$VAULT/Knowledge/Tag Taxonomy.md"

if [[ ! -f "$SCRIPT" ]]; then
    err "  $SCRIPT missing"
    exit 1
fi

# The tag checks read Knowledge/Tag Taxonomy.md as a hard allowlist. Without it
# they no-op rather than guessing a standard, so the lint still runs and the
# other five checks still report - but say so, since a silent no-op on the two
# checks most people install this for looks like a broken tool.
if [[ ! -f "$TAXONOMY" ]]; then
    warn "  No Knowledge/Tag Taxonomy.md found."
    warn "  The tag checks (dup-tags, bad-tags, taxonomy) will no-op until you create one."
    warn "  The other checks work regardless. See docs/Semantic Auto-Tagger Setup.md."
fi

info "Installing LaunchAgent com.obsidian.vault-lint..."
install_plist_and_load Templates/Scripts/com.obsidian.vault-lint.plist com.obsidian.vault-lint

# Seed one run so the log exists and you can see what the weekly report will
# look like against your own vault. Read-only, and --exit-zero keeps a vault
# with existing findings from failing the install.
info "Running an initial lint (read-only)..."
/usr/bin/python3 "$SCRIPT" --exit-zero \
    || warn "  initial run returned non-zero; see ~/Library/Logs/vault-lint.log"

ok "vault-lint component complete"
info "  Runs Mondays 07:00; log: ~/Library/Logs/vault-lint.log"
info "  Run by hand:      /usr/bin/python3 \"$SCRIPT\""
info "  Fix safe classes: /usr/bin/python3 \"$SCRIPT\" --apply"
info "  Undo any fix:     /usr/bin/python3 \"$SCRIPT\" --rollback <manifest>"
