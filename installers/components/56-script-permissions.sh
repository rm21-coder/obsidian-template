#!/usr/bin/env bash
# 56-script-permissions.sh - normalize modes on the deployed Python scripts.
#
# Runs late, after every component that writes into Templates/Scripts, so it is
# the single authority on script modes. Two rules:
#
#   entry point (has an `if __name__ == "__main__"` block)  ->  0700
#   library     (imported by a sibling, no __main__ block)  ->  0600
#
# Owner-only throughout. Nothing here needs group or world access, and it lines
# up with the rest of the vault's posture (lib/secrets.sh writes the secrets dir
# 0700, the security state files are 0600).
#
# Why this exists as an install step rather than committed file modes: git only
# records the executable bit, not the full mode. A clone materializes 0755/0644
# per the cloner's umask, so the owner-only half of the convention cannot be
# expressed in the repo at all. Without this component the modes drift on every
# fresh install.
#
# The execute bit is not cosmetic. Templates/Scripts ships `#!/usr/bin/env
# python3` shebangs and sync-vault.sh execs obsidian-rag-sync.py directly, so an
# entry point that loses +x fails at the call site. Conversely a library with +x
# invites being run as a program, which for the venv-bootstrapping modules would
# re-exec the wrong file.
#
# Components 35, 42 and 49 used to chmod their own scripts; those lines are gone
# now that this is the single authority. One consequence: `--only` takes a single
# component, so `./install.sh --only 42-markitdown-cleanup` on a fresh clone sets
# no modes at all. Follow it with `./install.sh --only 56-script-permissions` if
# you need the executable bit before the next full run.

set -euo pipefail

VAULT="$HOME/Obsidian"
SCRIPTS="$VAULT/Templates/Scripts"

if [[ ! -d "$SCRIPTS" ]]; then
    err "  $SCRIPTS missing"
    exit 1
fi

info "Normalizing permissions on $SCRIPTS/*.py..."

entry=0
lib=0

# -maxdepth 1 keeps this out of .venv/, which holds thousands of installed
# files whose modes are pip's business, not ours.
while IFS= read -r script; do
    if grep -q '__main__' "$script"; then
        chmod 0700 "$script"
        entry=$((entry + 1))
    else
        chmod 0600 "$script"
        lib=$((lib + 1))
    fi
done < <(find "$SCRIPTS" -maxdepth 1 -type f -name '*.py' -print)

if (( entry + lib == 0 )); then
    warn "  no Python scripts found in $SCRIPTS - nothing to normalize"
    exit 0
fi

ok "script-permissions component complete"
info "  $entry entry point(s) set 0700, $lib librar(ies) set 0600"
