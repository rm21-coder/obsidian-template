#!/usr/bin/env bash
# 31-quickadd-patch.sh - suppress QuickAdd's "current folder" suggestion
# in the default fall-through of getFolderPath().
#
# Why
# ---
# When QuickAdd creates a new note via a Template choice with a single
# configured folder (e.g. New Meeting -> Meetings/), it should write
# straight to that folder. But if the active file is in a subfolder of
# the configured root (e.g. you're focused on Meetings/History when you
# hit New Meeting), QuickAdd's getCurrentFolderSuggestion() injects the
# active file's parent as a "topItem" suggestion. That bumps items.length
# from 1 to 2, and shouldPromptForFolder fires - producing an unexpected
# picker that offers Meetings vs Meetings/History. There is no toggle to
# disable this suggestion.
#
# The patch removes the topItems argument from the deterministic
# fall-through branch only. All the user-facing "choose" toggles
# (chooseWhenCreatingNote / chooseFromSubfolders /
# createInSameFolderAsActiveFile) still work and still pass topItems
# through their own branches.
#
# The fall-through call is matched structurally, not by a hardcoded
# literal: QuickAdd's minifier renames locals on every release (the I/G
# names below were already stale as of 2.21.0, replaced by t/i plus a
# new executor: arg), so a fixed-string match silently stops matching
# the moment upstream re-minifies. See installers/lib/quickadd_patch.py
# for the structural match (a backreference pins the fall-through call
# regardless of what the minifier names its variables this release).
#
# Idempotent: runs sequentially after 30-plugins.sh re-fetches the
# plugin from GitHub. If the unpatched shape isn't present (already
# patched, or upstream changed the code), the component logs and exits.

set -euo pipefail

VAULT="$HOME/Obsidian"
TARGET="$VAULT/.obsidian/plugins/quickadd/main.js"

if [[ ! -f "$TARGET" ]]; then
    warn "  $TARGET not found; skipping quickadd patch (plugin not installed?)"
    exit 0
fi

RESULT="$(python3 "$REPO_ROOT/installers/lib/quickadd_patch.py" "$TARGET")"

case "$RESULT" in
    PATCHED)
        ok "  quickadd patched: dropped topItems in default fall-through of getFolderPath"
        ;;
    ALREADY_PATCHED)
        ok "  quickadd already patched (idempotent re-run)"
        ;;
    NOT_FOUND)
        warn "  quickadd main.js does not contain the expected getOrCreateFolder pattern; upstream may have changed structurally. Skipping patch - review getFolderPath() in main.js manually."
        ;;
    AMBIGUOUS:*)
        warn "  quickadd main.js has ${RESULT#AMBIGUOUS:} candidate fall-through calls (expected 1); aborting - review getFolderPath() in main.js manually."
        ;;
    *)
        warn "  quickadd patch check produced unexpected output: $RESULT"
        ;;
esac
