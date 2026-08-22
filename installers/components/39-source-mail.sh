#!/usr/bin/env bash
# 39-source-mail.sh - source mail transport (phone -> intake mailbox -> ~/SourceMedia).
#
# Runs before the watchers it feeds (41-voice-cleanup,
# 47-podcast) because it is what fills their drop folders. Those folders used
# to live in iCloud Drive; this replaces that with a mailbox relay, so no
# cloud-drive client is needed on macOS or Windows. See
# docs/Source-Mail-Transport.md.
#
# The agent is installed unconditionally but is inert without credentials: with
# no mailbox configured it logs one "missing config" line per tick and exits
# non-zero, touching neither the network nor the vault. That is deliberate —
# installing it now means a user who sets up the mailbox later only has to fill
# in .env, not remember there was an install step too. The cost is a noisy
# source-mail-pull.err on a machine that never wires up a phone.

set -euo pipefail

source "$REPO_ROOT/installers/lib/plist.sh"
source "$REPO_ROOT/installers/lib/secrets.sh"

VAULT="$HOME/Obsidian"
SCRIPTS="$VAULT/Templates/Scripts"

if [[ ! -x "$SCRIPTS/source_mail_pull.py" ]]; then
    err "  $SCRIPTS/source_mail_pull.py missing"
    exit 1
fi

# ---- drop folders ----------------------------------------------------------
# source_media.py owns the layout; ask it rather than hardcoding paths here, so
# this component and the watchers cannot disagree about where drops land. It
# also reports (and, with --apply, moves) anything still sitting in a legacy
# iCloud Drive folder.
info "Creating the drop-folder layout under ~/SourceMedia..."
"$SCRIPTS/.venv/bin/python3" "$SCRIPTS/source_media.py" --apply || {
    err "  source_media.py failed; not installing the agent"
    exit 1
}

# ---- credentials -----------------------------------------------------------
ensure_secrets_dir

info "Source mail transport credentials (used by source_mail_pull.py)..."
info "  Use a DEDICATED mailbox, not a +alias on your main account: the"
info "  credential on this machine should unlock exactly one mailbox holding"
info "  exactly these payloads. Make the address unguessable."
info "  Press enter at any prompt to skip; the agent stays inert until set."

prompt_env_value SOURCE_MAIL_USER \
    "  The intake mailbox address the phone sends drops to." \
    "  Example format: intake-xxxxxxxx@gmail.com (use your own random suffix)"

prompt_env_secret SOURCE_MAIL_APP_PASSWORD \
    "  App Password for that mailbox. Generate one PER MACHINE so a lost
  machine can be revoked on its own. Gmail hides the option until 2-Step
  Verification is on, and keeps it hidden if 2SV is passkey-only." \
    "https://myaccount.google.com/apppasswords"

if env_has SOURCE_MAIL_TOKEN && [[ "${FORCE:-0}" -ne 1 ]]; then
    ok "  SOURCE_MAIL_TOKEN already set; leaving unchanged"
else
    # Generated rather than prompted: this one is shared with the phone, and a
    # hand-picked value is both weaker and likelier to contain a character that
    # dies silently in transit. Alphanumeric only — it has to survive
    # quoted-printable encoding, mail line-wrapping, iOS smart punctuation and
    # dotenv parsing (`#` starts a comment in some parsers, `$` interpolates).
    TOKEN="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40)"
    if [[ "${#TOKEN}" -ne 40 ]]; then
        err "  token generation produced ${#TOKEN} chars, expected 40"
        exit 1
    fi
    env_set SOURCE_MAIL_TOKEN "$TOKEN"
    ok "  generated a 40-character SOURCE_MAIL_TOKEN"
    info "  Enter this same value in the signing script on each device:"
    info "    $TOKEN"
fi

prompt_env_value SOURCE_MAIL_ALLOWED_SENDERS \
    "  Comma-separated From addresses allowed to post drops. Fails CLOSED:
  empty rejects everything rather than allowing everything. Point every
  device at one sending account (iOS Settings > Apps > Mail > Default
  Account) rather than widening this list." \
    "  Example format: you@gmail.com"

# ---- agent -----------------------------------------------------------------
info "Installing LaunchAgent com.obsidian.source-mail-pull..."
install_plist_and_load Templates/Scripts/com.obsidian.source-mail-pull.plist com.obsidian.source-mail-pull

ok "source-mail component complete"
info "  Log:    ~/Library/Logs/source-mail-pull.log (rejections: .err)"
info "  Drops:  ~/SourceMedia/{VoiceInput,PodcastInput}/"
info "  Verify before wiring up a phone:"
info "    '$SCRIPTS/.venv/bin/python3' '$SCRIPTS/source_mail_pull.py' --emit news 'https://example.com/a'"
info "    '$SCRIPTS/.venv/bin/python3' '$SCRIPTS/source_mail_pull.py' --once --dry-run"
info "  Producer-side Shortcut setup: see docs/Source-Mail-Transport.md"
