#!/usr/bin/env bash
# 02-classification-audit.sh — defense-in-depth check that the cloned repo
# carries no `.md` content in user-content folders with a classification
# other than `public`.
#
# The PRIMARY gate is the pre-commit hook on the repo maintainer's clone
# (see installers/install-git-hooks.sh). This component is a recipient-
# side belt-and-suspenders that protects you in case the maintainer
# pushed without the hook, or if you cloned a fork that bypassed it.
#
# Failure here is hard: the installer aborts. To override (NOT
# recommended — you are explicitly trusting un-audited content):
#     ./install.sh --skip 02-classification-audit

set -euo pipefail

AUDIT="$REPO_ROOT/installers/lib/check_classification.py"

if [[ ! -x "$AUDIT" ]]; then
    err "classification audit script not found at $AUDIT"
    err "this repo is missing safety scaffolding; refusing to continue."
    exit 1
fi

info "auditing repo content for non-public classifications..."

if python3 "$AUDIT" --repo-root "$REPO_ROOT" --quiet; then
    ok "classification audit passed — repo carries only public content"
else
    err ""
    err "classification audit FAILED. This repo contains .md files in"
    err "user-content folders (Knowledge/, Meetings/, People/, etc.) that"
    err "either lack a classification or are marked non-public."
    err ""
    err "This is a security regression. Refusing to install."
    err ""
    err "To investigate, run:"
    err "    python3 $AUDIT --repo-root $REPO_ROOT"
    err ""
    err "To proceed anyway (NOT recommended), re-run install.sh with:"
    err "    ./install.sh --skip 02-classification-audit"
    exit 1
fi
