#!/usr/bin/env bash
#
# install.sh - single-script installer for the Obsidian Second-Brain Template.
#
# What this does
# --------------
# Runs every component installer under installers/components/ in numbered
# order. Each component is idempotent (detect, then install or skip), so
# re-running is safe.
#
# Components in order:
#   00-preflight             macOS, arch, Homebrew, python3, disk, sudo cache
#   02-classification-audit  recipient-side check: repo carries only public content
#   05-obsidian-app          install Obsidian.app via Homebrew cask
#   10-vault-bootstrap       vault folder skeleton (only fills missing dirs)
#   20-secrets               prompt for the model endpoint key + Open WebUI
#   30-plugins               fetch the 13 community plugins from GitHub
#   31-quickadd-patch        suppress QuickAdd's current-folder topItem suggestion
#   35-ribbon-order          apply tracked ribbon icon order to workspace.json
#   39-source-mail           signed mail drop transport (voice/podcast producers)
#   40-tagger                semantic auto-tagger LaunchAgent
#   41-voice-cleanup         iPhone-dictation cleanup LaunchAgent
#   42-markitdown-cleanup    standalone markitdown_cleanup.py at vault path
#   43-markitdown-dropper    build the .app bundle wrapper
#   44-strip-ads             Clippings ad-stripper LaunchAgent
#   46-youtube               YouTube summarizer (CLI + Share Sheet ready)
#   47-podcast               podcast transcribe (Apple Silicon only)
#   48-meeting-prep          Meeting prep auto-insert LaunchAgent
#   49-security-controls     two security agents + state dir + log rotation
#   50-llm-rag               Ollama + 8B model + Open WebUI (Docker) + RAG sync
#   51-group-photos          group photo refresh LaunchAgent (two-script pass)
#   52-meeting-prepopulate   Meeting pre-population, producer-agnostic (opt-in)
#   53-morning-dashboard     weekday morning HTML dashboard LaunchAgent
#   54-meeting-pull          scheduled MCP calendar fetch producer (opt-in)
#   55-vault-lint            nightly vault lint LaunchAgent
#   56-script-permissions    tighten mode bits on Templates/Scripts
#   57-dashboard-actions     URL-scheme handler for the dashboard's buttons (opt-in)
#   58-classification        nightly data-classification assistant + export gate
#   90-verify                post-install smoke tests, prints status table
#
# Usage
# -----
#   ./install.sh                       interactive: prompt at every component
#   ./install.sh --auto                non-interactive: install all, sensible defaults
#   ./install.sh --profile <name>      pre-answer the prompts from a profile
#   ./install.sh --list-profiles       print available profiles and exit
#   ./install.sh --without-podcast     skip the podcast component
#   ./install.sh --without-llm         skip the LLM RAG stack entirely
#   ./install.sh --only 40-tagger      run just one component
#   ./install.sh --skip 50-llm-rag     run all except this one (repeatable)
#   ./install.sh --dry-run             print the run plan (with filters applied), change nothing
#   ./install.sh --rebaseline          force security controls to re-baseline
#   ./install.sh --force               overwrite scripts at ~/Obsidian/Templates/Scripts even if newer
#   ./install.sh --list                print component list and exit
#
# Profiles
# --------
# The opt-in components (52/54/57) ship OFF and several prompts have no
# sensible universal default, because they depend on things an installer
# cannot provision: an AI gateway, an approved MCP calendar connector, your
# tenant's domains. That is right for a stranger cloning this repo and wrong
# for a colleague who should end up with the same setup you run.
#
# A profile answers those prompts up front:
#
#   ./install.sh --profile gateway            shipped example (placeholders)
#   ./install.sh --profile ~/ours.env         a file handed to you directly
#   ./install.sh --auto --profile gateway     unattended, profile as consent
#
# A profile is sourced, so it is code at the same trust level as this script:
# read one before you run it. See installers/profiles/README.md for the keys.
#
# Logs to ~/Library/Logs/obsidian-template-install.log

set -euo pipefail

# Resolve repo root (the directory this script lives in)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export REPO_ROOT="$SCRIPT_DIR"

# Load shared helpers
# shellcheck source=installers/lib/common.sh
source "$REPO_ROOT/installers/lib/common.sh"

# ---- argument parsing ------------------------------------------------------

INTERACTIVE=1
DRY_RUN=0
REBASELINE=0
FORCE=0
ONLY=""
SKIP_LIST=()
WITHOUT_PODCAST=0
WITHOUT_LLM=0
LIST_ONLY=0
PROFILE=""
LIST_PROFILES=0
PROFILES_DIR="$REPO_ROOT/installers/profiles"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --auto)              INTERACTIVE=0 ;;
        --dry-run)           DRY_RUN=1 ;;
        --rebaseline)        REBASELINE=1 ;;
        --force)             FORCE=1 ;;
        --without-podcast)   WITHOUT_PODCAST=1 ;;
        --without-llm)       WITHOUT_LLM=1 ;;
        --profile)           shift; PROFILE="$1" ;;
        --list-profiles)     LIST_PROFILES=1 ;;
        --only)              shift; ONLY="$1" ;;
        --skip)              shift; SKIP_LIST+=("$1") ;;
        --list)              LIST_ONLY=1 ;;
        -h|--help)           sed -n '/^# /s/^# //p' "$0"; exit 0 ;;
        *)                   err "Unknown argument: $1"; exit 2 ;;
    esac
    shift
done

export INTERACTIVE DRY_RUN REBASELINE FORCE

# ---- preflight: log dir, banner --------------------------------------------
# --dry-run and --list are read-only modes: no log dir, no log append.

LOG_FILE="$HOME/Library/Logs/obsidian-template-install.log"
if [[ "$DRY_RUN" -eq 0 && "$LIST_ONLY" -eq 0 ]]; then
    mkdir -p "$HOME/Library/Logs"
    # 0600 before anything is written. This log captures every prompt and answer
    # of an interactive install; at the default 0644 it is readable by any
    # account on the machine. Bounds the damage if something ever echoes a
    # value it should not.
    touch "$LOG_FILE" && chmod 600 "$LOG_FILE"
    exec > >(tee -a "$LOG_FILE") 2>&1
fi

# ---- profiles --------------------------------------------------------------

# List what is on offer: shipped profiles plus anything dropped into the
# directory. The .example files are templates to copy, not runnable profiles,
# so they are listed separately.
list_profiles() {
    local found=0 f
    info "Profiles in $PROFILES_DIR:"
    for f in "$PROFILES_DIR"/*.env; do
        [[ -e "$f" ]] || continue
        found=1
        printf "  %-16s %s\n" "$(basename "$f" .env)" "$(sed -n '2s/^# *//p' "$f")"
    done
    [[ $found -eq 1 ]] || echo "  (none)"
    for f in "$PROFILES_DIR"/*.env.example; do
        [[ -e "$f" ]] || continue
        printf "  %-16s %s\n" "$(basename "$f")" "(template - copy to <name>.env first)"
    done
    info "Use: ./install.sh --profile <name>   (or a path to a profile file)"
}

if [[ "$LIST_PROFILES" -eq 1 ]]; then
    list_profiles
    exit 0
fi

# Resolve a profile spec to a file and source it. A bare name means a profile
# in installers/profiles/; anything with a slash or a .env suffix is taken as
# a path, so a profile can be handed over out-of-band and never committed.
# `set -a` exports every assignment, because components run in subshells.
load_profile() {
    local spec="$1" file
    if [[ "$spec" == */* || "$spec" == *.env ]]; then
        file="$spec"
        # A bare filename is relative to the cwd, which makes the command
        # depend on where it was run from. Fall back to the repo root so
        # `--profile ours.env` works for a profile copied into the vault
        # whether or not the user cd'd there first.
        [[ -f "$file" ]] || file="$REPO_ROOT/$spec"
    else
        file="$PROFILES_DIR/${spec}.env"
    fi
    if [[ ! -f "$file" ]]; then
        err "profile not found: $file"
        list_profiles
        exit 2
    fi
    set -a
    # shellcheck disable=SC1090  # path is chosen by the operator at runtime
    source "$file"
    set +a
    INSTALL_PROFILE="$(basename "$file" .env)"
    export INSTALL_PROFILE
    ok "profile: $INSTALL_PROFILE ($file)"
    [[ -n "${PROFILE_DESCRIPTION:-}" ]] && info "  $PROFILE_DESCRIPTION"
    [[ -n "${LLM_BASE_URL:-}" ]] && info "  Claude calls route through: $LLM_BASE_URL"
    return 0
}

banner

if [[ -n "$PROFILE" ]]; then
    load_profile "$PROFILE"
fi

# ---- one-time prereq notice ------------------------------------------------
# Print up front so the user can fetch keys before the installer reaches the
# 20-secrets component. Only relevant in interactive mode.
if [[ "$INTERACTIVE" -eq 1 && "$DRY_RUN" -eq 0 && "$LIST_ONLY" -eq 0 ]]; then
    if [[ -n "${LLM_BASE_URL:-}" ]]; then
        # A gateway install needs a key from your institution, not from the
        # Anthropic console - naming the wrong console is how people end up
        # billing a personal card for work traffic.
        cat <<PREREQ_GW
Before continuing, have the following ready in another tab:

  ${LLM_API_KEY_NAME:-your AI gateway key}
                      (used by tagger, voice-cleanup, RAG sync)
                      Issued by your institution's AI gateway:
                      ${LLM_GATEWAY_HELP_URL:-$LLM_BASE_URL}
                      This is NOT an api.anthropic.com key.
PREREQ_GW
    else
    cat <<'PREREQ'
Before continuing, have the following ready in another tab:

  Anthropic API key   (used by tagger, voice-cleanup, RAG sync)
                      Get from: https://console.anthropic.com/settings/keys

  Your sudo password  (the security component installs a newsyslog rotation
                      config; one prompt up front, cached for the rest of
                      the run)

You can press enter at any key prompt to skip it - the .env / Keychain
entry can be set manually later.

PREREQ
    fi
    read -r -p "Press Enter when ready to continue (or Ctrl-C to abort): " _
    echo
fi

# ---- enumerate components --------------------------------------------------

COMPONENT_DIR="$REPO_ROOT/installers/components"
if [[ ! -d "$COMPONENT_DIR" ]]; then
    err "components directory not found: $COMPONENT_DIR"
    exit 2
fi

# Sort by filename so 00-, 10-, 20-, ... run in order.
# Portable Bash 3.2 compatible (macOS ships Bash 3.2; `mapfile` is Bash 4+).
COMPONENTS=()
while IFS= read -r f; do
    COMPONENTS+=("$f")
done < <(find "$COMPONENT_DIR" -maxdepth 1 -name '[0-9][0-9]-*.sh' -type f | sort)

if [[ "$LIST_ONLY" -eq 1 ]]; then
    info "Components in run order:"
    for c in "${COMPONENTS[@]}"; do
        echo "  $(basename "$c" .sh)"
    done
    exit 0
fi

# ---- run each component ----------------------------------------------------

for comp in "${COMPONENTS[@]}"; do
    name="$(basename "$comp" .sh)"

    # --only filter
    if [[ -n "$ONLY" && "$name" != "$ONLY" ]]; then
        continue
    fi

    # --skip filter
    skip=0
    for s in "${SKIP_LIST[@]:-}"; do
        [[ "$name" == "$s" ]] && skip=1 && break
    done
    [[ $skip -eq 1 ]] && { info "skip: $name (--skip)"; continue; }

    # --without-podcast / --without-llm short-circuit
    if [[ "$WITHOUT_PODCAST" -eq 1 && "$name" == 47-podcast ]]; then
        info "skip: $name (--without-podcast)"
        continue
    fi
    if [[ "$WITHOUT_LLM" -eq 1 && "$name" == 50-llm-rag ]]; then
        info "skip: $name (--without-llm)"
        continue
    fi

    # Dry run: report the component in the plan and move on. Components are
    # never sourced in this mode — that is the entire guarantee. (Do not try
    # to push DRY_RUN awareness down into the components instead: they mkdir,
    # render plists, write configs, and launchctl-load directly, and one
    # missed call means --dry-run performs a live install.)
    if [[ "$DRY_RUN" -eq 1 ]]; then
        # Component headers open with "# NN-name.sh - description." (some
        # use an em-dash). Bash prefix-stripping is multibyte-safe; sed
        # bracket classes are not.
        line="$(sed -n '2s/^# *//p' "$comp" 2>/dev/null || true)"
        desc="${line#"${name}.sh — "}"
        desc="${desc#"${name}.sh - "}"
        # Opt-in components declare their gate as `if ! pconfirm KEY ...`.
        # Report how the loaded profile answers it, so --dry-run says what a
        # profile actually turns ON rather than just listing every component
        # (the whole question a profile exists to answer).
        note=""
        gate="$(sed -n 's/^if ! pconfirm \([A-Z_][A-Z_]*\).*/\1/p' "$comp" | head -1)"
        if [[ -n "$gate" ]]; then
            gate_var="PROFILE_$gate"
            case "${!gate_var:-}" in
                1|[Yy]|[Yy][Ee][Ss]|true) note="  [opt-in: YES per profile]" ;;
                0|[Nn]|[Nn][Oo]|false)    note="  [opt-in: no per profile]" ;;
                *)                        note="  [opt-in: will ask]" ;;
            esac
        fi
        printf "%s[dry-run]%s would run: %-24s %s%s\n" "$YELLOW" "$RESET" "$name" "$desc" "$note"
        continue
    fi

    echo
    section "$name"

    # Interactive prompt: install? skip? abort?
    if [[ "$INTERACTIVE" -eq 1 ]]; then
        if ! confirm "Run component '$name'?" Y; then
            info "skipped by user"
            continue
        fi
    fi

    # Source the component. It defines and calls its own work; we wrap in a
    # subshell so set -e inside a component doesn't kill the orchestrator.
    # shellcheck source=/dev/null  # component path is computed by design
    if ( source "$comp" ); then
        ok "$name complete"
    else
        rc=$?
        err "$name failed (exit $rc) - see $LOG_FILE"
        if [[ "$INTERACTIVE" -eq 1 ]]; then
            if ! confirm "Continue with remaining components?" N; then
                err "aborted after failure in $name"
                exit "$rc"
            fi
        else
            err "aborted after failure in $name (use --interactive to override)"
            exit "$rc"
        fi
    fi
done

echo
if [[ "$DRY_RUN" -eq 1 ]]; then
    profile_active && info "profile in effect: $INSTALL_PROFILE"
    section "dry run complete — nothing was changed"
else
    section "install.sh complete"
    info "log: $LOG_FILE"
    info "Open Obsidian to vault: $HOME/Obsidian"
    info "On first launch, click 'Trust author and enable plugins' when prompted."
fi
