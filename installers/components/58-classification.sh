#!/usr/bin/env bash
# 58-classification.sh - nightly data-classification assistant.
#
# Installs com.obsidian.classify, which runs classify_notes.py at 02:15 and
# proposes a data-classification tier for notes whose body has changed. Also
# checks that disclosure_check.py - the export gate that consumes those tiers -
# is present, because a classification scheme nothing enforces is decoration.
#
# This component deliberately does NOT seed an initial run, unlike most others.
# The first pass over an existing vault is the expensive one (every note, one
# model call each) and it must be run with Obsidian QUIT: Obsidian rewrites the
# frontmatter of any note an external process touches, bumping every modified
# stamp and reformatting what the script wrote. Kicking that off from an
# installer, while the user is almost certainly looking at Obsidian, would be
# the wrong default in both directions. The instructions are printed instead.
#
# Full reference: docs/Data-Classification.md

set -euo pipefail
source "$REPO_ROOT/installers/lib/plist.sh"

VAULT="$HOME/Obsidian"
SCRIPT="$VAULT/Templates/Scripts/classify_notes.py"
GATE="$VAULT/Templates/Scripts/disclosure_check.py"
POLICY="$VAULT/Knowledge/Data Classification.md"
VENV="$VAULT/Templates/Scripts/.venv/bin/python3"

if [[ ! -f "$SCRIPT" ]]; then
    err "  $SCRIPT missing"
    exit 1
fi

if [[ ! -x "$VENV" ]]; then
    err "  $VENV missing - the classifier needs the shared venv (anthropic,"
    err "  pyyaml, python-dotenv). Run the tagger component first, or create it."
    exit 1
fi

if [[ ! -f "$GATE" ]]; then
    warn "  disclosure_check.py not found alongside the classifier."
    warn "  The tiers will be assigned but nothing will enforce them on export."
fi

# The tiers only mean what your own policy note says they mean. Without it the
# classifier still runs against the four canonical values, but nobody reviewing
# the queue has a definition to check against.
if [[ ! -f "$POLICY" ]]; then
    warn "  No Knowledge/Data Classification.md found."
    warn "  Write one before working the review queue - see docs/Data-Classification.md."
fi

info "Installing LaunchAgent com.obsidian.classify..."
install_plist_and_load Templates/Scripts/com.obsidian.classify.plist com.obsidian.classify

ok "classification component complete"
info "  Runs nightly 02:15; log: ~/Library/Logs/obsidian-classify.log"
info ""
info "  NEXT: run the first full pass by hand, with Obsidian QUIT."
info "    1. Quit Obsidian."
info "    2. Preview:  \"$VENV\" \"$SCRIPT\" --dry-run --workers 8"
info "    3. Apply:    \"$VENV\" \"$SCRIPT\" --workers 8"
info "  Then work the review queue in Topics/Classification."
info ""
info "  Detectors only, no model calls and no API key needed:"
info "    \"$VENV\" \"$SCRIPT\" --detectors-only"
info "  Export gate:"
info "    \"$VENV\" \"$GATE\" check <note> --audience public"
