#!/usr/bin/env bash
# secrets.sh - secrets I/O for the installer.
#
# Two flavors:
#   1. Platform keystore   - PREFERRED for secrets (API keys, passwords,
#                            tokens). On macOS that is the Keychain, written
#                            through Templates/Scripts/secret_store.py
#                            (service = key name lowercased, account $USER).
#                            Every script resolves secrets env-first then
#                            keystore, so this is a drop-in upgrade.
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

# keystore_has <key>: true if the key is resolvable — non-empty in .env, or
# present in the platform keystore under the lowercased service name.
keystore_has() {
    local key="$1"
    env_has "$key" && return 0
    if is_macos; then
        local service
        service="$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')"
        _security 10 find-generic-password -a "$USER" -s "$service" -w >/dev/null 2>&1 && return 0
    fi
    return 1
}

# keystore_set <key> <value>: store a secret. Keychain via secret_store.py on
# macOS (value travels over stdin, never argv); .env everywhere else, and as
# the fallback if the Keychain write fails.
keystore_set() {
    local key="$1" val="$2"
    if is_macos; then
        if _secret_store_set "$key" "$val"; then
            ok "  stored in Keychain: $key"
            return 0
        fi
        warn "  keystore unavailable for $key; falling back to $SECRETS_ENV"
    fi
    env_set "$key" "$val"
}

# prompt_env_secret <key> <human-description> <url-to-get-key>
# Silent input (key doesn't echo). Honors --force: when FORCE=1, prompts
# even if the key is already set, so the user can paste a new value.
# Secrets land in the platform keystore (macOS Keychain), not .env.
prompt_env_secret() {
    local key="$1" desc="$2" url="$3"
    if keystore_has "$key" && [[ "${FORCE:-0}" -ne 1 ]]; then
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
#     must stay that way: `x="$(keychain_has ...)"` would silently un-latch.
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
# THREE: keychain_has reads 142 as "absent", so prompt_keychain_secret prompts,
# so keychain_set writes - firing more calls at a Keychain that already proved
# it is blocked on a consent dialog nobody can answer. Each one queues another
# dialog, and that stack blocks every later Keychain operation on the machine,
# git-over-HTTPS through the osxkeychain helper included. A retry after a hang
# is the specific thing that must never happen, so the first timeout takes the
# Keychain out of play for the rest of the run and secrets fall back to .env -
# which is exactly why that fallback exists.
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
    err "  Keychain call timed out: $1"
    err "  It is blocked on a consent dialog that cannot be answered here."
    err "  NOT retrying, and skipping the Keychain for the rest of this run."
    info "     secrets fall back to $SECRETS_ENV for now"
    info "     once any pending dialog is dismissed, store them with:"
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

    local errout rc
    errout="$(printf '%s\n' "$value" | python3 "$helper" set "$name" 2>&1 >/dev/null)"
    rc=$?
    [[ "$rc" -eq 0 ]] && return 0
    case "$errout" in
        *"timed out"*) _keychain_latch "secret_store.py set $name" ;;
    esac
    return 1
}

keychain_has() {
    local service="$1" account="${2:-$USER}"
    _security "$KEYCHAIN_TIMEOUT_SECONDS" find-generic-password \
        -a "$account" -s "$service" -w >/dev/null 2>&1
}

keychain_set() {
    local service="$1" account="${2:-$USER}" value="$3"
    # secret_store.py binds to the invoking user (getpass.getuser()), so it
    # cannot honour a different account. Refuse rather than write the secret
    # somewhere its reader will never look.
    if [[ "$account" != "$USER" ]]; then
        err "  keychain_set: account '$account' is not the current user; refusing"
        return 1
    fi
    if _secret_store_set "$service" "$value"; then
        return 0
    fi
    err "  Keychain write failed for $service"
    info "     do NOT re-run it if it timed out - a queued consent dialog"
    info "     blocks later Keychain reads. Store it by hand once, with:"
    info "       python3 Templates/Scripts/secret_store.py set $service"
    return 1
}

prompt_keychain_secret() {
    local service="$1" desc="$2" url="$3" account="${4:-$USER}"
    if keychain_has "$service" "$account" && [[ "${FORCE:-0}" -ne 1 ]]; then
        ok "  Keychain entry already set: service=$service, account=$account (use --force to overwrite)"
        return 0
    fi
    if [[ "${INTERACTIVE:-1}" -eq 0 ]]; then
        warn "  Keychain entry missing for $service; --auto mode skipping prompt"
        return 0
    fi
    echo
    info "$desc"
    info "  Get a key at: $url"
    local val
    read -r -s -p "  Paste secret for $service (or press enter to skip): " val
    echo
    if [[ -z "$val" ]]; then
        warn "  skipped $service; set later with: python3 Templates/Scripts/secret_store.py set $service"
        return 0
    fi
    keychain_set "$service" "$account" "$val" || return 1
    ok "  Keychain entry stored: service=$service"
}
