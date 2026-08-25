#!/usr/bin/env bash
# 90-verify.sh - post-install smoke tests.
#
# Prints a status table of every pipeline:
#   script present?  agent loaded?  log file present?
#
# Non-fatal: a failing row prints in red but does not exit non-zero. The
# user reads the table and decides what (if anything) to fix.

set -euo pipefail

VAULT="$HOME/Obsidian"
LOG_DIR="$HOME/Library/Logs"

# row <name> <script-path-or-empty> <plist-label-or-empty> <log-name-or-empty>
row() {
    local name="$1" script="$2" label="$3" logname="$4"
    local script_ok="-" agent_ok="-" log_ok="-"

    if [[ -n "$script" ]]; then
        # -e covers both regular files AND .app bundle directories
        if [[ -e "$script" ]]; then script_ok="✓"; else script_ok="✗"; fi
    fi
    if [[ -n "$label" ]]; then
        # Loosened match: just look for the label anywhere on its own line
        # in launchctl list. Different macOS versions vary the surrounding
        # whitespace; the strict `[[:space:]]LABEL$` pattern can miss valid
        # entries.
        if launchctl list 2>/dev/null | awk -v l="$label" '$3==l {found=1} END{exit !found}'; then
            agent_ok="✓"
        else
            agent_ok="✗"
        fi
    fi
    if [[ -n "$logname" ]]; then
        if [[ -f "$LOG_DIR/$logname" ]]; then log_ok="✓"; else log_ok="-"; fi
    fi
    printf "  %-22s  script %s   agent %s   log %s\n" "$name" "$script_ok" "$agent_ok" "$log_ok"
}

info "Post-install status table:"
echo
row "tagger"            "$VAULT/Templates/Scripts/tag_clippings.py"               "com.tag-clippings"                  "tag-clippings.log"
row "source-mail"      "$VAULT/Templates/Scripts/source_mail_pull.py"            "com.obsidian.source-mail-pull"      "source-mail-pull.log"
row "voice-cleanup"     "$VAULT/Templates/Scripts/voice_cleanup.py"               "com.voice-cleanup"                  "voice-cleanup.log"
row "markitdown-cleanup" "$VAULT/Templates/Scripts/markitdown_cleanup.py"          ""                                   ""
row "markitdown-dropper" "$HOME/Applications/Markitdown Dropper.app"               ""                                   ""
row "strip-ads"         "$VAULT/Templates/Scripts/strip_ads.py"                   "com.obsidian.strip-ads"             "strip-ads.log"
row "youtube"           "$VAULT/Templates/Scripts/youtube_summarize.py"           ""                                   ""
row "podcast"           "$VAULT/Templates/Scripts/podcast_transcribe.py"          ""                                   ""
row "podcast-watch"     "$VAULT/Templates/Scripts/podcast_watch.py"               "com.obsidian.podcast-watch"         "podcast-watch.log"
row "meeting-prep"      "$VAULT/Templates/Scripts/meeting_prep.py"                "com.obsidian.meeting-prep"          "meeting-prep.log"
row "security/plugin"   "$VAULT/Templates/Scripts/plugin_integrity_check.py" "com.obsidian.security.plugin-check" "obsidian-security.log"
row "security/integrity" "$VAULT/Templates/Scripts/integrity_monitor.py" "com.obsidian.security.integrity"    "obsidian-security.log"
row "rag-sync"          "$VAULT/Templates/Scripts/obsidian-rag-sync.py"           "com.obsidian-rag-sync"              "obsidian-rag-sync.log"
row "group-photos"      "$VAULT/Z_attachments/refresh_groups.py"                  "com.obsidian.group-photos"          "group-photos.log"
row "meeting-prepopulate*" "$VAULT/Templates/Scripts/meeting_prepopulate.py"        "com.meeting-prepopulate"            "meeting-prepopulate.log"
row "meeting-pull*"     "$VAULT/Templates/Scripts/meeting_pull.py"                "com.obsidian.meeting-pull"          "meeting-pull.log"
row "morning-dashboard"  "$VAULT/Templates/Scripts/morning_dashboard.py"           "com.morning-dashboard"             "morning-dashboard.log"
row "vault-lint"        "$VAULT/Templates/Scripts/vault_lint.py"                  "com.obsidian.vault-lint"            "vault-lint.log"
row "classify"          "$VAULT/Templates/Scripts/classify_notes.py"              "com.obsidian.classify"              "obsidian-classify.log"
row "dash-actions*"     "$HOME/Applications/DashboardActions.app"                 ""                                   "dashboard-actions.log"

echo
info "  * opt-in component: 'agent ✗' on a starred row just means you declined it"
echo
info "Secrets status:"
# Existence and content are separate questions. Chaining them with && ... ||
# meant grep's exit status decided the message: a file that existed but held
# none of the matched keys reported as "missing". That is worse than unhelpful
# here -- this path is a machine-wide secrets file shared with other tools, so
# telling the operator it is absent invites them to recreate or overwrite it.
if [[ -f "$HOME/dev/secrets/.env" ]]; then
    if grep -qE '^(ANTHROPIC|OPEN_WEBUI|OBSIDIAN_COLLECTION|LLM_|TAGGER_)' "$HOME/dev/secrets/.env"; then
        grep -E '^(ANTHROPIC|OPEN_WEBUI|OBSIDIAN_COLLECTION|LLM_|TAGGER_)' "$HOME/dev/secrets/.env" \
            | sed -E 's/=.*/=<redacted>/' | sed 's/^/  /'
    else
        info "  ~/dev/secrets/.env present, but holds none of this project's keys"
    fi
else
    warn "  ~/dev/secrets/.env missing"
fi

# No third-party key row: every model call in the vault, the YouTube
# summarizer included, resolves through the endpoint reported below.

echo
info "Claude endpoint:"
# Which endpoint the SCHEDULED runs will use - resolved the same way they
# resolve it (from ~/dev/secrets/.env), not from this shell's environment,
# because that difference is exactly the bug worth catching here: a gateway
# configured only in the installer's process works now and silently reverts
# to stock Anthropic auth at 03:00.
source "$REPO_ROOT/installers/lib/secrets.sh"
ENDPOINT_PY="$VAULT/Templates/Scripts/llm_endpoint.py"
VENV_PY="$VAULT/Templates/Scripts/.venv/bin/python3"
[[ -x "$VENV_PY" ]] || VENV_PY="$(command -v python3 || true)"
if [[ -f "$ENDPOINT_PY" && -n "$VENV_PY" ]]; then
    # Run with a clean LLM_* environment so we report what .env says.
    endpoint="$(env -u LLM_BASE_URL -u LLM_API_KEY_NAME "$VENV_PY" "$ENDPOINT_PY" 2>/dev/null || true)"
    if [[ -n "$endpoint" ]]; then
        ok "  $endpoint"
        endpoint_key="${endpoint%% *}"
        if keystore_has "$endpoint_key"; then
            ok "  $endpoint_key resolves (.env or Keychain)"
        else
            warn "  $endpoint_key not resolvable - the scheduled tagger/voice runs will fail"
            info "     store it with: python3 $VAULT/Templates/Scripts/secret_store.py set $endpoint_key"
        fi
    else
        warn "  could not resolve the endpoint (python-dotenv missing from the venv?)"
    fi
else
    warn "  llm_endpoint.py not found at $ENDPOINT_PY"
fi

echo
info "Ollama / Open WebUI:"
if has_cmd ollama; then
    ok "  ollama installed: $(ollama --version 2>/dev/null | head -1 || echo unknown)"
    info "  models: $(ollama list 2>/dev/null | tail -n +2 | wc -l | tr -d ' ') installed"
else
    warn "  ollama not installed (run install.sh --only 50-llm-rag)"
fi
if has_cmd docker && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^open-webui$'; then
    ok "  open-webui container running at http://localhost:3000"
else
    warn "  open-webui not running"
fi

echo
ok "verify complete"
info "Open Obsidian, click 'Trust author and enable plugins' on first launch."
