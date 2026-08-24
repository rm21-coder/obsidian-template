#!/usr/bin/env bash
# 20-secrets.sh - prompt for and store all API keys.
#
# Two homes for secrets:
#   ~/dev/secrets/.env   - for the Python scripts that load_dotenv()
#                          (tagger, voice_cleanup, rag-sync)
#   macOS Keychain       - for the endpoint credential and the security HMAC
#
# Skips any key already set. --force overwrites existing values.

set -euo pipefail

source "$REPO_ROOT/installers/lib/secrets.sh"

ensure_secrets_dir

# Where Claude calls go. A profile (or a hand-edited .env) can point every
# Claude-calling script at an institutional AI gateway instead of
# api.anthropic.com; llm_endpoint.py reads these at call time. They are
# PERSISTED to .env rather than left in the installer's environment because
# the LaunchAgents that run the tagger and voice-cleanup on a schedule start
# from a bare environment - launchd never sources your shell, so a gateway
# that only exists in this process would work now and silently fall back to
# stock Anthropic auth at 03:00.
if [[ -n "${LLM_BASE_URL:-}" ]]; then
    GATEWAY_KEY_NAME="${LLM_API_KEY_NAME:-ANTHROPIC_API_KEY}"
    info "AI gateway endpoint: $LLM_BASE_URL"
    env_set LLM_BASE_URL "$LLM_BASE_URL"
    env_set LLM_API_KEY_NAME "$GATEWAY_KEY_NAME"
    # Only if the profile set them; otherwise the scripts' own defaults win.
    # Both LLM-calling agents are listed: a gateway that exposes model aliases
    # rather than dated Anthropic ids needs the alias to reach the SCHEDULED
    # runs, and com.obsidian.classify at 05:00 gets the same bare launchd
    # environment the tagger does.
    for opt in TAGGER_MODEL TAGGER_PROMPT_CACHE \
               CLASSIFIER_MODEL CLASSIFIER_PROMPT_CACHE; do
        opt_val="${!opt:-}"
        [[ -n "$opt_val" ]] && env_set "$opt" "$opt_val"
    done
    info "$GATEWAY_KEY_NAME (used by tagger, voice-cleanup, rag-sync)..."
    prompt_env_secret "$GATEWAY_KEY_NAME" \
        "  Your institution's AI gateway key - NOT an api.anthropic.com key." \
        "${LLM_GATEWAY_HELP_URL:-$LLM_BASE_URL}"
else
    info "Anthropic API key (used by tagger, voice-cleanup, rag-sync)..."
    prompt_env_secret ANTHROPIC_API_KEY \
        "  The tagger and voice-cleanup pipelines call the Claude API." \
        "https://console.anthropic.com/settings/keys"
fi

# The YouTube summarizer needs no key of its own: it calls Claude through
# llm_endpoint, so the endpoint credential collected above already covers it.
# There is deliberately no second-provider prompt here any more.
info "YouTube summarizer: uses the endpoint credential above; no separate key."
opt_val="${YOUTUBE_MODEL:-}"
[[ -n "$opt_val" ]] && env_set YOUTUBE_MODEL "$opt_val"

info "Open WebUI API key (used by obsidian-rag-sync.py)..."
info "  This is generated AFTER component 50-llm-rag has set up Open WebUI"
info "  and you have created the Knowledge collection in its UI."
info "  Press enter to skip if Open WebUI isn't ready yet; re-run with"
info "    ./install.sh --only 20-secrets --force"
info "  once you have the key and the collection UUID."
prompt_env_secret OPEN_WEBUI_API_KEY \
    "  Open WebUI admin > Settings > Account > API Keys > Show" \
    "http://localhost:3000"

info "Obsidian Knowledge collection ID (the UUID for your 'Obsidian' collection)..."
prompt_env_value OBSIDIAN_COLLECTION_ID \
    "  The UUID at the end of http://localhost:3000/workspace/knowledge/<UUID>" \
    "  Example format: 0c2f7e51-9d8b-4b3a-a1e7-2b89df1a0e9c"

ok "secrets component complete"
info "  Keychain (macOS)    : ${GATEWAY_KEY_NAME:-ANTHROPIC_API_KEY}, OPEN_WEBUI_API_KEY"
info "  ~/dev/secrets/.env  : OBSIDIAN_COLLECTION_ID (non-secret config; also the secrets fallback off-macOS)"
if [[ -n "${LLM_BASE_URL:-}" ]]; then
    info "  Endpoint            : $(python3 "$REPO_ROOT/Templates/Scripts/llm_endpoint.py" 2>/dev/null || echo "$LLM_BASE_URL")"
fi
info "  Move any existing plaintext key out of .env with: python3 Templates/Scripts/secret_store.py set <NAME>"
