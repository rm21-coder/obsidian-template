#!/usr/bin/env bash
# common.sh - shared logging, prompt, and idempotent helpers.
#
# Sourced by install.sh and every installers/components/*.sh.

# ---- output formatting ------------------------------------------------------
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'
    RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[34m'; CYAN=$'\033[36m'
    RESET=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; CYAN=""; RESET=""
fi

ts()      { date '+%Y-%m-%d %H:%M:%S'; }
# All log helpers write to stderr so they don't pollute $(func) captures
# inside subshells. The orchestrator's `exec > >(tee -a ...)` + `2>&1`
# wrapper still routes everything into the install log.
info()    { printf "%s[%s] %s==>%s %s\n" "$DIM" "$(ts)" "$BOLD$BLUE" "$RESET" "$*" >&2; }
ok()      { printf "%s[%s] %s✓%s %s\n"  "$DIM" "$(ts)" "$GREEN"    "$RESET" "$*" >&2; }
warn()    { printf "%s[%s] %s!%s %s\n"       "$DIM" "$(ts)" "$YELLOW"   "$RESET" "$*" >&2; }
err()     { printf "%s[%s] %sx%s %s\n"       "$DIM" "$(ts)" "$RED"      "$RESET" "$*" >&2; }

banner() {
    printf "\n%s%s===================================================%s\n" "$BOLD" "$CYAN" "$RESET"
    printf "%s%s Obsidian Second-Brain Template - install.sh        %s\n" "$BOLD" "$CYAN" "$RESET"
    printf "%s%s===================================================%s\n\n" "$BOLD" "$CYAN" "$RESET"
}

section() {
    printf "\n%s%s--- %s ---%s\n" "$BOLD" "$CYAN" "$*" "$RESET"
}

# ---- confirm: y/n prompt with default ---------------------------------------
# usage: confirm "Do thing?" Y       # default yes
#        confirm "Do thing?" N       # default no
# Returns 0 for yes, 1 for no. Honors INTERACTIVE=0 (always returns 0 unless
# the default is N, in which case it returns 1).
confirm() {
    local prompt="$1"
    local default="${2:-Y}"
    local hint
    if [[ "$default" == "Y" ]]; then hint="[Y/n]"; else hint="[y/N]"; fi

    if [[ "${INTERACTIVE:-1}" -eq 0 ]]; then
        [[ "$default" == "Y" ]] && return 0 || return 1
    fi

    local reply
    read -r -p "$prompt $hint " reply
    reply="${reply:-$default}"
    case "$reply" in
        [Yy]|[Yy][Ee][Ss]) return 0 ;;
        *) return 1 ;;
    esac
}

# ---- prompt: free-text input with default -----------------------------------
# Honors INTERACTIVE=0 by taking the default instead of blocking on a read
# nobody is there to answer. A component that reaches a prompt with no default
# in --auto therefore gets an empty string and has to handle it - which is
# exactly what an install profile is for (see pdefault below).
prompt() {
    local question="$1"
    local default="${2:-}"
    local var
    if [[ "${INTERACTIVE:-1}" -eq 0 ]]; then
        printf "%s" "$default"
        return 0
    fi
    if [[ -n "$default" ]]; then
        read -r -p "$question [$default]: " var
        var="${var:-$default}"
    else
        read -r -p "$question: " var
    fi
    printf "%s" "$var"
}

# ---- install profiles -------------------------------------------------------
# A profile is an env file (installers/profiles/<name>.env) that pre-answers
# the installer: which opt-in components to run, and what goes in the prompts
# they ask. install.sh sources it before the first component and exports
# INSTALL_PROFILE; the three helpers below are how components read it.
#
# The point is distribution. "Clone this and run ./install.sh --profile ours"
# reproduces a working setup - gateway endpoint, calendar producer, the
# components that ship off because they need something an installer cannot
# provision - instead of handing someone a list of choices they have no basis
# to make yet. See installers/profiles/README.md.

# profile_active: true when a profile was loaded.
profile_active() { [[ -n "${INSTALL_PROFILE:-}" ]]; }

# pdefault KEY [FALLBACK]: the profile's value for KEY, else FALLBACK.
# Pass it as prompt()'s default so a profile pre-fills the answer and the
# user can still overtype it.
pdefault() {
    local var="PROFILE_$1"
    printf "%s" "${!var:-${2:-}}"
}

# pconfirm KEY "question" [DEFAULT]: confirm(), except that a profile's
# explicit answer for KEY wins outright and is announced rather than asked.
# That is what lets `--auto --profile X` install an opt-in component: the
# profile is the consent, so the component must gate on this and not on
# INTERACTIVE alone.
pconfirm() {
    local var="PROFILE_$1"
    local answer="${!var:-}"
    case "$answer" in
        1|[Yy]|[Yy][Ee][Ss]|true)  info "  $1: yes (profile ${INSTALL_PROFILE:-?})"; return 0 ;;
        0|[Nn]|[Nn][Oo]|false)     info "  $1: no (profile ${INSTALL_PROFILE:-?})";  return 1 ;;
        "")                        ;;
        *) warn "  ignoring PROFILE_$1='$answer' (expected 1 or 0)" ;;
    esac
    confirm "$2" "${3:-N}"
}

# ---- macOS / arch / brew bootstrap ------------------------------------------
is_macos()       { [[ "$(uname)" == "Darwin" ]]; }
is_arm64()       { [[ "$(uname -m)" == "arm64" ]]; }
has_brew()       { command -v brew >/dev/null 2>&1; }
has_cmd()        { command -v "$1" >/dev/null 2>&1; }

ensure_brew() {
    if has_brew; then
        ok "Homebrew already installed: $(brew --version | head -1)"
        return 0
    fi
    if [[ "${INTERACTIVE:-1}" -eq 1 ]]; then
        if ! confirm "Homebrew not found. Install it now from brew.sh?" Y; then
            err "Homebrew is required. Install from https://brew.sh and re-run."
            return 1
        fi
    fi
    info "Installing Homebrew (will prompt for your sudo password)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Append to PATH for the rest of this session
    if [[ -d /opt/homebrew/bin ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -d /usr/local/bin ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
    ok "Homebrew installed."
}

# _verify_target_exists <target>: returns 0 if `target` is present.
# Smart resolution:
#   - empty target  -> always true (no check requested)
#   - ends in .app  -> check /Applications and ~/Applications
#   - contains /    -> check that exact path
#   - else          -> check has_cmd (on PATH)
_verify_target_exists() {
    local target="$1"
    [[ -z "$target" ]] && return 0
    case "$target" in
        *.app)
            [[ -e "/Applications/$target" || -e "$HOME/Applications/$target" || -e "$target" ]]
            ;;
        */*)
            [[ -e "$target" ]]
            ;;
        *)
            has_cmd "$target"
            ;;
    esac
}

# brew_install_if_missing <pkg> [cask] [verify_target]
# Idempotent brew install with tolerance for brew exits non-zero from a
# post-install hook (link conflicts, leftover binaries, etc.) when the
# target actually landed on disk. Pass `verify_target` to opt into that
# tolerance: typically the CLI name (e.g., "yt-dlp") or the .app bundle
# (e.g., "Obsidian.app"). Without verify_target, brew's exit code is
# trusted as-is.
brew_install_if_missing() {
    local pkg="$1"
    local cask="${2:-}"
    local verify_target="${3:-}"

    # Fast path: if the target is already in place, skip even checking brew.
    # Useful when the binary lives in a path brew didn't manage (custom PATH,
    # prior non-brew install, etc.).
    if [[ -n "$verify_target" ]] && _verify_target_exists "$verify_target"; then
        ok "$verify_target already present; skipping brew install of $pkg"
        return 0
    fi

    if [[ "$cask" == "cask" ]]; then
        if brew list --cask "$pkg" >/dev/null 2>&1; then
            # brew's registration is not evidence the app is on disk. A
            # Caskroom entry can outlive the application -- observed on a Mac
            # Studio 2026-08-25, where the Caskroom held a dangling symlink to
            # an /Applications bundle that no longer existed, and the installer
            # still reported the cask installed and the component complete.
            # Net effect: a green install and no Obsidian on the machine.
            if [[ -n "$verify_target" ]] && ! _verify_target_exists "$verify_target"; then
                warn "brew reports $pkg installed, but $verify_target is not on disk"
                warn "  (stale Caskroom registration). Reinstalling."
                if brew reinstall --cask "$pkg" && _verify_target_exists "$verify_target"; then
                    ok "brew cask $pkg reinstalled; $verify_target present"
                    return 0
                fi
                err "  $pkg is registered with brew but $verify_target is still missing."
                err "  Fix by hand: brew uninstall --cask --force $pkg && brew install --cask $pkg"
                return 1
            fi
            ok "brew cask $pkg already installed"
            return 0
        fi
        info "brew install --cask $pkg"
        if brew install --cask "$pkg"; then
            return 0
        fi
        if [[ -n "$verify_target" ]] && _verify_target_exists "$verify_target"; then
            warn "  brew install --cask $pkg exited non-zero, but $verify_target is on disk; continuing"
            return 0
        fi
        err "brew install --cask $pkg failed"
        return 1
    else
        if brew list "$pkg" >/dev/null 2>&1; then
            ok "brew $pkg already installed"
            return 0
        fi
        info "brew install $pkg"
        if brew install "$pkg"; then
            return 0
        fi
        if [[ -n "$verify_target" ]] && _verify_target_exists "$verify_target"; then
            warn "  brew install $pkg exited non-zero, but $verify_target is on PATH; continuing"
            warn "  (likely a brew link conflict; run 'brew link --overwrite <dep>' to clean up)"
            return 0
        fi
        err "brew install $pkg failed"
        return 1
    fi
}

# ---- launchd helpers --------------------------------------------------------
launchctl_reload() {
    local plist="$1"   # absolute path
    local label
    label="$(basename "$plist" .plist)"
    if launchctl list | grep -q "^[0-9-]\+[[:space:]]\+[0-9-]\+[[:space:]]\+${label}$"; then
        info "  unloading previous agent: $label"
        launchctl unload "$plist" 2>/dev/null || true
    fi
    info "  loading agent: $label"
    launchctl load "$plist"
}

# ---- dry-run wrapper --------------------------------------------------------
run() {
    if [[ "${DRY_RUN:-0}" -eq 1 ]]; then
        printf "%s[dry-run]%s %s\n" "$YELLOW" "$RESET" "$*"
    else
        "$@"
    fi
}

# ---- safer overwrite: refuse to clobber newer files at dst ------------------
safe_install_file() {
    local src="$1" dst="$2"
    if [[ -f "$dst" && "$dst" -nt "$src" && "${FORCE:-0}" -eq 0 ]]; then
        warn "  $dst is newer than $src; skipping (use --force to overwrite)"
        return 0
    fi
    install -m 0755 "$src" "$dst"
    ok "  installed: $dst"
}
