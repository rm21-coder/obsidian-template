#!/usr/bin/env bash
# secrets.sh - secrets I/O for the installer.
#
# Two flavors:
#   1. Platform keystore   - PREFERRED for secrets (API keys, passwords,
#                            tokens). On macOS that is the Keychain, written
#                            through Templates/Scripts/secret_store.py
#                            (service = key name lowercased, account $USER).
#                            Every script resolves secrets keystore-first,
#                            then .env for a name the keystore lacks (KV-1).
#   2. ~/dev/secrets/.env  - for non-secret config (collection IDs, hosts,
#                            allowlists), and the secrets fallback on
#                            platforms without a keystore path wired up.
# The security HMAC trust anchor (service 'obsidian-allowlist-hmac') is
# managed separately by security_common.get_or_create_hmac_key().

SECRETS_DIR="$HOME/dev/secrets"
SECRETS_ENV="$SECRETS_DIR/.env"

# ensure_secrets_dir: creates ~/dev/secrets/ with mode 0700 if missing.
ensure_secrets_dir() {
    mkdir -p "$SECRETS_DIR"
    chmod 0700 "$SECRETS_DIR"
    if [[ ! -f "$SECRETS_ENV" ]]; then
        : > "$SECRETS_ENV"
        chmod 0600 "$SECRETS_ENV"
        ok "  created: $SECRETS_ENV (mode 0600)"
    fi
}

# env_has <key>: returns 0 if .env already has a non-empty value for key.
env_has() {
    local key="$1"
    [[ -f "$SECRETS_ENV" ]] || return 1
    grep -qE "^${key}=.+" "$SECRETS_ENV"
}

# env_set <key> <value>: idempotently upsert a key in .env. Will not
# overwrite an existing value unless FORCE=1.
env_set() {
    local key="$1" val="$2"
    ensure_secrets_dir
    if env_has "$key" && [[ "${FORCE:-0}" -ne 1 ]]; then
        ok "  $key already set in $SECRETS_ENV (use --force to overwrite)"
        return 0
    fi
    # Remove any existing line for this key, then append.
    local tmp
    tmp="$(mktemp -t obsidian_env)"
    grep -vE "^${key}=" "$SECRETS_ENV" > "$tmp" || true
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
    install -m 0600 "$tmp" "$SECRETS_ENV"
    rm -f "$tmp"
    ok "  set: $key in $SECRETS_ENV"
}

# env_get <key>: the value of key in .env, or nothing. For moving a secret into
# the keystore only; a caller must never print it.
env_get() {
    local key="$1"
    [[ -f "$SECRETS_ENV" ]] || return 0
    grep -E "^${key}=" "$SECRETS_ENV" | head -n 1 | cut -d= -f2-
}

# env_config_value <key>: a configuration value from .env, parsed the way the
# jobs' python-dotenv reads it: an optional `export `, spaces around `=`,
# single or double quotes, a trailing ` # comment` on an unquoted value, CRLF
# line ends, and the LAST assignment winning. For names (LLM_API_KEY_NAME,
# KEYVAULT_KEY_NAME), never for secret values. Review R7: `grep | cut` missed
# every one of those shapes, so uninstall --secrets silently kept the key.
env_config_value() {
    local key="$1"
    # -r, not -f: an unreadable file is treated like a missing one rather
    # than failing the pipeline under pipefail (review S4).
    [[ -r "$SECRETS_ENV" ]] || return 0
    tr -d '\r' < "$SECRETS_ENV" \
        | sed -n -E "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*//p" \
        | tail -n 1 \
        | sed -E -e 's/^"([^"]*)".*$/\1/' -e "s/^'([^']*)'.*\$/\1/" \
                 -e 's/[[:space:]]+#.*$//' -e 's/[[:space:]]+$//'
}

# env_unset <key>: remove key's line from .env, keeping its 0600 mode.
env_unset() {
    local key="$1" tmp
    [[ -f "$SECRETS_ENV" ]] || return 0
    tmp="$(mktemp -t obsidian_env)"
    grep -vE "^${key}=" "$SECRETS_ENV" > "$tmp" || true
    install -m 0600 "$tmp" "$SECRETS_ENV"
    rm -f "$tmp"
}

# migrate_env_secret <key>: on macOS, move a secret an older install left in
# .env into the Keychain, then remove it from .env. Since KV-1 (2026-11-05)
# scripts read the Keychain first and .env only for a name the Keychain lacks,
# so when both hold a value the Keychain one is in use. When they DIFFER, both
# are left and the operator is told which one the jobs now use.
#
# Review S3: only exit 44 means the Keychain does not hold it. Any other
# failure says nothing about what is stored, so nothing is moved: moving the
# .env value in would replace a stored value with it, which is what KV-1
# exists to stop.
migrate_env_secret() {
    local key="$1" service envval kcval rc
    is_macos || return 0
    env_has "$key" || return 0
    service="$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')"
    envval="$(env_get "$key")"
    [[ "${KEYCHAIN_WEDGED:-0}" -eq 1 ]] && {
        warn "  $key: the Keychain is not answering this run; left it in $SECRETS_ENV"
        return 0
    }
    kcval="$(_security 10 find-generic-password -a "$USER" -s "$service" -w 2>/dev/null)" \
        && rc=0 || rc=$?
    # The latch dies in the $(...) above, so set it here on a timeout.
    [[ "$rc" -eq 142 ]] && _keychain_latch "the Keychain read of $service (10s)"
    if [[ "$rc" -ne 0 && "$rc" -ne 44 ]]; then
        warn "  $key: the Keychain could not be read (exit $rc); left it in"
        warn "  $SECRETS_ENV and moved nothing"
        return 0
    fi
    if [[ "$rc" -eq 0 ]]; then
        if [[ "$kcval" == "$envval" ]]; then
            env_unset "$key"
            ok "  $key: removed the plaintext copy from $SECRETS_ENV; the Keychain holds it"
        else
            warn "  $key is in $SECRETS_ENV and in the Keychain with different values;"
            warn "  the Keychain one is in use. Left both: keep the right one, delete the other."
        fi
        return 0
    fi
    rc=0
    _secret_store_set "$key" "$envval" || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        env_unset "$key"
        ok "  $key: moved from $SECRETS_ENV into the Keychain"
    elif [[ "$rc" -eq 3 ]]; then
        # F-20261125: Key Vault is on and holds this name; no job reads
        # either copy. Moved nowhere, and the operator is told why.
        warn "  $key: Key Vault is on, so this .env copy is not used; load it"
        warn "  into Key Vault if it is newer, then delete its line from $SECRETS_ENV"
    else
        warn "  $key: Keychain unavailable; it stays in $SECRETS_ENV (mode 0600)"
    fi
}

# installed_keystore_names: every keystore item (lowercased service name) an
# installer component stores, one per line, for uninstall.sh --secrets (F10).
# The gateway key's name and the Key Vault certificate key's name are
# configurable, so they are read from .env. test_uninstall_secrets.py checks
# that every name a component stores appears here.
installed_keystore_names() {
    local gw kv
    gw="$(env_config_value LLM_API_KEY_NAME)"
    kv="$(env_config_value KEYVAULT_KEY_NAME)"
    printf '%s\n' anthropic_api_key open_webui_api_key open_webui_admin_password \
        source_mail_app_password source_mail_token azure_kv_client_key \
        gemini_api_key obsidian-allowlist-hmac
    [[ -n "$gw" ]] && printf '%s\n' "$gw" | tr '[:upper:]' '[:lower:]'
    [[ -n "$kv" ]] && printf '%s\n' "$kv" | tr '[:upper:]' '[:lower:]'
    return 0
}

# env_project_keys [gateway-key-name]: the names (never values) of this
# project's keys present in .env, one per line, for uninstall.sh --secrets.
env_project_keys() {
    local gw="${1:-}"
    [[ -r "$SECRETS_ENV" ]] || return 0
    # The gateway key's own line is matched as written, in any case (review
    # S5). YOUTUBE_MODEL and OBSIDIAN_COLLECTION_ID are installer-written
    # configuration, not secrets, but still this project's.
    tr -d '\r' < "$SECRETS_ENV" \
        | sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=.*/\2/p' \
        | awk -v gw="$gw" '
            (gw != "" && tolower($0) == tolower(gw)) ||
            $0 == "ANTHROPIC_API_KEY" || $0 == "GEMINI_API_KEY" ||
            $0 == "OPEN_WEBUI_API_KEY" || $0 == "OPEN_WEBUI_ADMIN_PASSWORD" ||
            $0 == "YOUTUBE_MODEL" || $0 == "OBSIDIAN_COLLECTION_ID" ||
            /^(LLM|SOURCE_MAIL|TAGGER|CLASSIFIER|KEYVAULT)_/ { if (!seen[$0]++) print }'
}

# keystore_has <key>: whether the jobs can resolve key, by the same rule as
# secret_store.get_secret (review S3). Returns 0 when it resolves (the
# Keychain holds it, or the Keychain reports no such item and .env holds it),
# 1 when nothing holds it, and 2 when the Keychain could not be read, which
# says nothing about what is stored: a caller must then neither prompt nor
# write. Call it as `keystore_has K && rc=0 || rc=$?`, never inside $(...).
keystore_has() {
    local key="$1" service rc
    if is_macos; then
        service="$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')"
        _keychain_state "$service" && rc=0 || rc=$?
        [[ "$rc" -eq 0 ]] && return 0
        [[ "$rc" -eq 2 ]] && return 2
    fi
    env_has "$key" && return 0
    return 1
}

# keystore_set <key> <value>: store a secret. Keychain via secret_store.py on
# macOS (value travels over stdin, never argv); .env everywhere else, and as
# the fallback if the Keychain write fails.
#
# Review R3: since KV-1 a stored value wins over .env. So when the Keychain
# write fails and the new value lands in .env, an OLDER Keychain item for the
# same name would stay in use: a rotation that silently did not happen, and
# for a leaked key, the leaked one kept in service. The fallback therefore
# removes that older item, or says plainly that the old value is still live.
keystore_set() {
    local key="$1" val="$2" rc=0
    if is_macos; then
        _secret_store_set "$key" "$val" || rc=$?
        if [[ "$rc" -eq 0 ]]; then
            ok "  stored in Keychain: $key"
            return 0
        fi
        if [[ "$rc" -eq 3 ]]; then
            # F-20261125: refused because Key Vault holds this name. Never
            # fall back to .env: that would put the value on disk in plain
            # text, where no job reads it while the vault is on.
            warn "  $key not stored: Key Vault is on; load it into the vault"
            warn "  (docs/Key-Vault.md). Nothing was written to $SECRETS_ENV."
            return 0
        fi
        warn "  keystore unavailable for $key; falling back to $SECRETS_ENV"
        _retire_stale_keychain_item "$key"
    fi
    env_set "$key" "$val"
}

# _retire_stale_keychain_item <key>: after a failed Keychain write, remove an
# older item for key so the value written to .env is the one the jobs use.
_retire_stale_keychain_item() {
    local key="$1" service rc
    service="$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')"
    if [[ "${KEYCHAIN_WEDGED:-0}" -ne 1 ]]; then
        # `&& || ` so a non-zero exit is data, not a set -e abort.
        _security "$KEYCHAIN_TIMEOUT_SECONDS" find-generic-password \
            -a "$USER" -s "$service" >/dev/null 2>&1 && rc=0 || rc=$?
        [[ "$rc" -eq 44 ]] && return 0           # no older item: nothing in the way
        if [[ "$rc" -eq 0 ]] &&
                _security "$KEYCHAIN_TIMEOUT_SECONDS" delete-generic-password -a "$USER" -s "$service" >/dev/null 2>&1; then
            warn "  removed the older $key from the Keychain, so the value in"
            warn "  $SECRETS_ENV is the one the jobs now use"
            return 0
        fi
    fi
    err "  the Keychain may still hold an older $key, and if it does the jobs"
    err "  keep using THAT value, not the new one in $SECRETS_ENV."
    info "     once the Keychain answers, remove it with:"
    info "       python3 Templates/Scripts/secret_store.py rm $key"
}

# report_key_resolution <key> <scripts-dir>: say (for 90-verify) whether the
# jobs can resolve key, by the same rule as secret_store.get_secret.
report_key_resolution() {
    local key="$1" scripts="$2" rc
    if [[ -n "$(env_config_value KEYVAULT_URL)" ]]; then
        # With the vault on, neither the Keychain nor .env is the source
        # (review S7), so the Keychain is not read at all (T3): only
        # secret_store can say where it resolves.
        info "  Key Vault is on: check $key with"
        info "     python3 $scripts/secret_store.py status"
        return 0
    fi
    keystore_has "$key" && rc=0 || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        ok "  $key resolves (Keychain, or .env where the Keychain lacks it)"
    elif [[ "$rc" -eq 2 ]]; then
        warn "  $key: the Keychain could not be read, so the scheduled"
        warn "  runs get no key until it answers. Do not store it again."
    else
        warn "  $key not resolvable - the scheduled tagger/voice runs will fail"
        info "     store it with: python3 $scripts/secret_store.py set $key"
    fi
    return 0
}

# vault_holds <key>: true on macOS when Key Vault is on (KEYVAULT_URL in
# .env) and key is one of the secrets the jobs read from it. The same list
# as secret_store._vault_names: the gateway key (LLM_API_KEY_NAME, default
# ANTHROPIC_API_KEY) and the three in _STATUS_NAMES; test_secrets_sh.py
# holds the two in step.
vault_holds() {
    local key gw
    key="$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
    is_macos || return 1
    [[ -n "$(env_config_value KEYVAULT_URL)" ]] || return 1
    gw="$(env_config_value LLM_API_KEY_NAME)"
    gw="$(printf '%s' "${gw:-ANTHROPIC_API_KEY}" | tr '[:lower:]' '[:upper:]')"
    case "$key" in
        "$gw"|SOURCE_MAIL_APP_PASSWORD|SOURCE_MAIL_TOKEN|OPEN_WEBUI_API_KEY) return 0 ;;
    esac
    return 1
}

# keystore_plan <key>: what a step that would create or replace key should
# do, in KEYSTORE_PLAN (a variable, not output: the Keychain latch must not
# die in a $(...) subshell). "keep" when it is held and --force is not set;
# "create" when nothing holds it, or with --force; "skip" when the Keychain
# could not be read, which says nothing about what it holds, so nothing may
# be prompted for, generated or written (S3). Says why when it skips.
keystore_plan() {
    local key="$1" rc
    if vault_holds "$key"; then
        # F-20261125: the jobs read this one from Key Vault, and a value
        # stored here would never be used, so nothing is asked or generated.
        KEYSTORE_PLAN=skip
        ok "  $key: Key Vault is on and holds it; not prompting or generating"
        info "     check it with: python3 Templates/Scripts/secret_store.py status $key"
        return 0
    fi
    keystore_has "$key" && rc=0 || rc=$?
    if [[ "$rc" -eq 2 ]]; then
        KEYSTORE_PLAN=skip
        if [[ "${KEYCHAIN_WEDGED:-0}" -eq 1 ]]; then
            warn "  $key: skipped; the Keychain is not answering this run (see above)"
        else
            warn "  $key: the Keychain could not be read; not prompting,"
            warn "  generating or writing. Re-run once it answers."
        fi
    elif [[ "$rc" -eq 0 && "${FORCE:-0}" -ne 1 ]]; then
        KEYSTORE_PLAN=keep
    else
        KEYSTORE_PLAN=create
    fi
    return 0
}

# prompt_env_secret <key> <human-description> <url-to-get-key>
# Silent input (key doesn't echo). Honors --force: when FORCE=1, prompts
# even if the key is already set, so the user can paste a new value.
# Secrets land in the platform keystore (macOS Keychain), not .env.
prompt_env_secret() {
    local key="$1" desc="$2" url="$3"
    keystore_plan "$key"
    [[ "$KEYSTORE_PLAN" == skip ]] && return 0
    if [[ "$KEYSTORE_PLAN" == keep ]]; then
        ok "  $key already set; leaving unchanged (use --force to overwrite)"
        return 0
    fi
    if [[ "${INTERACTIVE:-1}" -eq 0 ]]; then
        warn "  $key not set; --auto mode skipping prompt (store later with: python3 Templates/Scripts/secret_store.py set $key)"
        return 0
    fi
    echo
    info "$desc"
    info "  Get a key at: $url"
    local val
    # -s for silent input so the key doesn't echo to the terminal/log
    read -r -s -p "  Paste $key (or press enter to skip): " val
    echo
    if [[ -z "$val" ]]; then
        warn "  skipped $key; store later with: python3 Templates/Scripts/secret_store.py set $key"
        return 0
    fi
    keystore_set "$key" "$val"
}

# prompt_env_value <key> <human-description> <hint>
# Visible input. For non-secret identifiers (UUIDs, hostnames) where the
# user will want to see what they're pasting. Same --force behavior.
prompt_env_value() {
    local key="$1" desc="$2" hint="$3"
    if env_has "$key" && [[ "${FORCE:-0}" -ne 1 ]]; then
        ok "  $key already set; leaving unchanged (use --force to overwrite)"
        return 0
    fi
    if [[ "${INTERACTIVE:-1}" -eq 0 ]]; then
        warn "  $key not set; --auto mode skipping prompt (set manually in $SECRETS_ENV)"
        return 0
    fi
    echo
    info "$desc"
    [[ -n "$hint" ]] && info "  $hint"
    local val
    read -r -p "  Paste $key (or press enter to skip): " val
    if [[ -z "$val" ]]; then
        warn "  skipped $key; set later by editing $SECRETS_ENV"
        return 0
    fi
    env_set "$key" "$val"
}

# ---- macOS Keychain helpers -------------------------------------------------
# ---- Keychain access: bounded, and latched on a hang -----------------------
#
# Mirrors secret_store.py's own ceiling so the two layers agree on what "too
# long" means. The helper bounds itself from the inside; this bounds the
# reads the shell makes directly.
KEYCHAIN_TIMEOUT_SECONDS=15

# Set once a Keychain call has hung. Deliberately a shell variable and not a
# marker file, which bounds it in two ways worth stating plainly:
#
#   - It lives in the current shell, so it does NOT carry from one component
#     into the next (install.sh sources each in a subshell). 20-secrets is
#     where essentially every Keychain call happens, and the python layer
#     bounds itself independently, so the cost of rediscovery is one dialog.
#   - It therefore also dies inside $(...). Every caller below is invoked as a
#     statement or an `if` condition, never in a command substitution, and it
#     must stay that way: `x="$(keystore_has ...)"` would silently un-latch.
#     There is a static test pinning this (test_static.py).
#
# A file-based latch would fix both, and was rejected: a marker keyed to a PID
# outlives the run, PIDs get reused, and a stale latch silently routes every
# secret to plaintext .env instead of the Keychain. Failing toward one extra
# dialog beats failing toward plaintext.
KEYCHAIN_WEDGED=0

# _security: run /usr/bin/security under a hard timeout, and LATCH on a hang.
#
# The latch is the point, not the timeout. perl exits 142 (128 + SIGALRM) when
# the alarm fires, and a caller that only checks "non-zero" cannot tell that
# from a legitimate "no such item". That ambiguity is how ONE hang becomes
# THREE: a reader takes 142 as "absent", so the caller prompts,
# so the write fires - more calls at a Keychain that already proved
# it is blocked on a consent dialog nobody can answer. Each one queues another
# dialog, and that stack blocks every later Keychain operation on the machine,
# git-over-HTTPS through the osxkeychain helper included. A retry after a hang
# is the specific thing that must never happen, so the first timeout takes the
# Keychain out of play for the rest of the run. Since review S3 (2026-11-10)
# a secret is then skipped, not written to .env: a Keychain that cannot be
# read says nothing about what it holds (T2).
#
# The path is explicit because /usr/bin/security is Apple-signed and stable
# across `brew upgrade python`, so ACL grants keyed to it survive; macOS ships
# no timeout(1), and perl's alarm() is the smallest thing always present.
_security() {
    local secs="$1"; shift
    if [[ "${KEYCHAIN_WEDGED:-0}" -eq 1 ]]; then
        return 142
    fi
    if [[ ! -x /usr/bin/perl ]]; then
        # Every macOS ships /usr/bin/perl, so this should be unreachable. The
        # call still has to happen; say plainly that it is now unbounded, so a
        # stall has a printed cause instead of looking like a mystery hang.
        warn "  /usr/bin/perl missing; running security without a timeout"
        /usr/bin/security "$@"
        return $?
    fi
    # alarm() survives the exec, so the timer applies to `security` itself.
    /usr/bin/perl -e 'alarm shift; exec @ARGV' "$secs" /usr/bin/security "$@"
    local rc=$?
    [[ "$rc" -eq 142 ]] && _keychain_latch "$1 (${secs}s)"
    return "$rc"
}

# _keychain_latch: record that the Keychain is wedged and say so once.
_keychain_latch() {
    [[ "${KEYCHAIN_WEDGED:-0}" -eq 1 ]] && return 0
    KEYCHAIN_WEDGED=1
    _keychain_explain "$1"
}

# _keychain_explain <what hung>: what the latch means, said once (T2). The
# caller may have sent _security's output to /dev/null (_keychain_state
# does), so it can call this again afterwards.
_keychain_explain() {
    err "  Keychain call timed out: $1"
    err "  It is blocked on a consent dialog that cannot be answered here."
    err "  NOT retrying: no Keychain read or write for the rest of this run, so"
    err "  every later secret is skipped (none is prompted for, none goes to .env)."
    info "     dismiss any pending dialog and re-run. If the same item hangs"
    info "     again, remove that item (named above) and store it fresh:"
    info "       python3 Templates/Scripts/secret_store.py rm <NAME>"
    info "       python3 Templates/Scripts/secret_store.py set <NAME>"
}

# _secret_store_set <name> <value>: the ONE Keychain write path.
#
# Everything routes through Templates/Scripts/secret_store.py rather than
# calling `security` again here. The helper already does the only safe form -
# delete, then a fresh add with -T /usr/bin/security, and never `-U`, because
# updating an existing item raises a consent prompt whenever its ACL doesn't
# already cover the caller. Two further reasons not to duplicate it: the value
# travels on stdin, so it never appears in the process table the way an argv
# `-w "$value"` does; and one implementation cannot drift from the other.
#
# Service names match across layers: the helper lowercases the name, and every
# name the installer writes is already lowercase.
#
# On failure, the helper's stderr is inspected for its timeout marker so a hang
# on the python side latches the shell side too - the two layers share the
# wedged state without sharing code.
_secret_store_set() {
    local name="$1" value="$2"
    local helper="$REPO_ROOT/Templates/Scripts/secret_store.py"

    if [[ "${KEYCHAIN_WEDGED:-0}" -eq 1 ]]; then
        return 1
    fi
    if [[ ! -f "$helper" ]]; then
        warn "  $helper missing; cannot use the keystore"
        return 1
    fi

    local errout rc line
    errout="$(printf '%s\n' "$value" | python3 "$helper" set "$name" 2>&1 >/dev/null)"
    rc=$?
    [[ "$rc" -eq 0 ]] && return 0
    # Say why (review R3). Only the helper's own lines, which name a secret
    # but never print its value.
    while IFS= read -r line; do
        case "$line" in secret_store:*) warn "  $line" ;; esac
    done <<< "$errout"
    case "$errout" in
        *"timed out"*) _keychain_latch "secret_store.py set $name" ;;
    esac
    # 3: refused because Key Vault holds the name (F-20261125); the callers
    # must not treat it as a Keychain failure.
    [[ "$rc" -eq 3 ]] && return 3
    return 1
}

# _keychain_state <service> [account]: 0 the item exists and reads, 1 no such
# item (exit 44), 2 it could not be read (a timeout, a locked Keychain, a
# denied ACL). The same line secret_store.py draws (review S3).
_keychain_state() {
    local service="$1" account="${2:-$USER}" rc was="${KEYCHAIN_WEDGED:-0}"
    _security "$KEYCHAIN_TIMEOUT_SECONDS" find-generic-password \
        -a "$account" -s "$service" -w >/dev/null 2>&1 && rc=0 || rc=$?
    # The latch's explanation went to /dev/null with the rest (T2): say it.
    if [[ "$was" -ne 1 && "${KEYCHAIN_WEDGED:-0}" -eq 1 ]]; then
        _keychain_explain "the Keychain read of $service"
    fi
    case "$rc" in
        0) return 0 ;;
        44) return 1 ;;
        *) return 2 ;;
    esac
}


