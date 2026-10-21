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

# ---- requirements: shared by 10-vault-bootstrap and update.sh ---------------
# One definition, so an install and an update can never install differently.
#
# Installs requirements.lock, never requirements.txt: every package at the
# release a maintainer locked, each file checked against its SHA256
# (--require-hashes), nothing the lock does not name (--no-deps), and no
# source builds (--only-binary), whose build tools pip would fetch unhashed.
# --force-reinstall, but only when the lock is new to this venv: pip skips a
# package already at the locked version without checking its files, so a
# venv built before the lock would keep whatever bytes it had. So the first
# install from a lock, and the first after the lock changes, reinstall every
# package; the lock's SHA256 is then recorded in the venv
# (<venv>/<lock name>.sha256), and later runs with the same lock install only
# what is missing or at the wrong version -- each of those hash-checked too.
# Reinstalling everything every time took 4-5 minutes per update on x64
# Windows (2026-10-02) to guard against an edit inside the venv, which anyone
# able to make it could do worse than. Delete the .sha256 file to force a
# full reinstall.
# pip itself is not upgraded: that would be an unpinned fetch ahead of the
# pinned ones, and the venv's bundled pip handles all of this.
#
# The lock has wheels for Apple Silicon and Python 3.10-3.14 only; the venv's
# own interpreter is what counts (a Rosetta shell or an Intel Homebrew python
# on an Apple Silicon Mac would otherwise pass the preflight and fail in pip).
LOCK_PYTHON_MIN=10
LOCK_PYTHON_MAX=14

# lock_is_plain <lock>
# Finding 5278623: --require-hashes does not stop a lock from naming its own
# source. A "pkg @ file:///x.whl --hash=<its own hash>" line, a bare wheel
# path, or an option line (-i, --extra-index-url, -r, -e, ...) installs
# whatever it points at, hash-checked against a hash the same line supplies.
# So before pip sees the lock, every line must be one of the three things
# lock_requirements.py (uv) writes: a comment, an indented
# "--hash=sha256:<64 hex>" continuation, or "name[extras]==version[ ; marker]".
# Anything else fails with the line number. Prints nothing on success.
LOCK_HASH_RE='^--hash=sha256:[0-9a-f]{64}( \\)?$'
LOCK_REQ_RE="^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9.+!_-]+( ;[A-Za-z0-9_ .'\"<>=!~*()-]*)?( \\\\)?$"
lock_is_plain() {
    local lock="$1" s n=0 why
    # read into one variable trims leading and trailing spaces and tabs in
    # linear time; then the CR of a Windows checkout, and any blanks it hid.
    # Substrings, not ${s%...}: suffix removal is quadratic on a long line
    # under a UTF-8 locale (1.8 s for a 100 kB line).
    while IFS=$' \t' read -r s || [[ -n "$s" ]]; do
        n=$((n + 1))
        while [[ "$s" == *[[:space:]] ]]; do s="${s:0:${#s}-1}"; done
        [[ -z "$s" || "$s" == \#* ]] && continue
        why=""
        if [[ "$s" == --hash=* ]]; then
            [[ "$s" =~ $LOCK_HASH_RE ]] || why="a --hash line that is not exactly --hash=sha256:<64 hex>"
        elif [[ "$s" == -* ]]; then
            why="a pip option line"
        elif [[ "$s" == *" @ "* || "$s" == *@* || "$s" == *"://"* ]]; then
            why="a direct-URL requirement"
        elif [[ "$s" =~ [[:space:]]- ]]; then
            why="a pip option on a requirement line"
        elif [[ ! "$s" =~ $LOCK_REQ_RE ]]; then
            why="not a name==version requirement"
        fi
        if [[ -n "$why" ]]; then
            err "  REFUSED $lock line $n: $why. A lock may hold only name==version lines and"
            err "  their --hash=sha256 continuations; regenerate it with installers/lib/lock_requirements.py."
            return 1
        fi
    done < "$lock"
    return 0
}
# install_requirements <venv python> <file in the same folder as the lock> [lock name]
install_requirements() {
    local venv_py="$1" lock
    lock="$(dirname "$2")/${3:-requirements.lock}"
    if [[ ! -f "$lock" ]]; then
        err "  $lock is missing; refusing to install unpinned requirements"
        return 1
    fi
    lock_is_plain "$lock" || return 1
    local probe machine minor
    # "<machine> <minor> <gil|nogil>": a free-threaded build has no wheels here.
    probe="$("$venv_py" -c 'import platform, sys, sysconfig; print(platform.machine(), sys.version_info[1], "nogil" if sysconfig.get_config_var("Py_GIL_DISABLED") else "gil")' 2>/dev/null | tail -1)" || probe=""
    local gil
    read -r machine minor gil <<<"$probe"
    if [[ "$machine" != "arm64" ]]; then
        err "  $venv_py is not an Apple Silicon interpreter (${machine:-unknown}); the pinned dependencies ship no wheels for it."
        err "  Rebuild the venv with Homebrew's /opt/homebrew/bin/python3.13."
        return 1
    fi
    if [[ "$gil" != "gil" ]]; then
        err "  $venv_py is a free-threaded (no-GIL) Python; the pinned dependencies ship no wheels for it."
        err "  Rebuild the venv with Homebrew's /opt/homebrew/bin/python3.13."
        return 1
    fi
    if [[ ! "$minor" =~ ^[0-9]+$ ]] || (( minor < LOCK_PYTHON_MIN || minor > LOCK_PYTHON_MAX )); then
        err "  $venv_py is Python 3.${minor:-?}; the pinned dependencies need 3.${LOCK_PYTHON_MIN}-3.${LOCK_PYTHON_MAX}."
        err "  Rebuild the venv with Homebrew's /opt/homebrew/bin/python3.13."
        return 1
    fi
    local stamp want have="" reinstall=()
    stamp="$(dirname "$(dirname "$venv_py")")/$(basename "$lock").sha256"
    want="$(shasum -a 256 "$lock" | cut -d' ' -f1)"
    [[ -f "$stamp" ]] && have="$(tr -d '[:space:]' < "$stamp")"
    if [[ -n "$want" && "$have" == "$want" ]]; then
        info "  $(basename "$lock") unchanged since this venv's last install: installing only what is missing"
    else
        info "  $(basename "$lock") is new to this venv: reinstalling every package from it"
        reinstall=(--force-reinstall)
    fi
    "$venv_py" -m pip install --disable-pip-version-check --require-hashes --no-deps \
        --only-binary :all: ${reinstall[@]+"${reinstall[@]}"} -r "$lock" || return 1
    # Only after pip succeeded: a failed install retries in full next time.
    [[ -n "$want" ]] && printf '%s\n' "$want" > "$stamp"
    local extra
    # -I: nothing beside the lock (a planted re.py, say) is imported.
    extra="$("$venv_py" -I "$(dirname "$lock")/lock_extras.py" "$lock" | tr '\n' ' ')" || true
    if [[ -n "${extra// /}" ]]; then
        warn "  installed but not in $(basename "$lock") (never hash-checked, not audited): $extra"
        warn "  delete $(dirname "$(dirname "$venv_py")") and re-run the installer to clear them"
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

# ---- the installed copy of the security controls ----------------------------
# Finding 5278112: the two security LaunchAgents run a copy of the controls
# installed outside the vault, under /usr/bin/python3 -I, so nothing written
# into the folder they watch is imported by them. The file list, the copy and
# its check live in security_common.install_controls (one definition for macOS
# and Windows); this names the place, which the plists hard-code.
SECURITY_CONTROLS_DIR="$HOME/.local/share/obsidian-security/bin"

# install_security_controls <Templates/Scripts dir> [<dest>]
# Copies the controls into <dest> (default $SECURITY_CONTROLS_DIR), owner-only,
# and fails unless every copy matches what it was copied from.
install_security_controls() {
    local src="$1" dest="${2:-$SECURITY_CONTROLS_DIR}"
    /usr/bin/python3 -I "$src/security_common.py" install-controls "$src" "$dest"
}
