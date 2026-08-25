#!/usr/bin/env bash
#
# uninstall.sh - tear down what install.sh set up.
#
# Safe by default. It removes the moving parts the installer created - the
# LaunchAgents, the wrapper apps, the agent logs, and regenerable scratch
# state - and leaves your DATA and shared TOOLS alone.
#
# Removed by default:
#   - all LaunchAgents (unloaded + plist deleted from ~/Library/LaunchAgents)
#   - wrapper apps: Markitdown Dropper.app
#   - agent logs under ~/Library/Logs
#   - regenerable state: the per-vault .venv, and logs/.locks/.state/.config
#     under Templates/Scripts, .tag_tracking.json, and the security state dir
#     (~/.local/share/obsidian-security)
#
# Removed ONLY when asked (flags below), because they hold data or need sudo:
#   --llm         stop + remove the open-webui Docker container
#   --secrets     delete ~/dev/secrets/.env and the Keychain entries
#   --newsyslog   remove /etc/newsyslog.d/obsidian-security.conf (sudo)
#   --plugins     remove the DOWNLOADED plugin files named in
#                 installers/plugin-pins.json. Each plugin's data.json - your
#                 ribbon, QuickAdd and Templater settings - is kept, and so is
#                 its folder. A reinstall does not restore data.json, so it is
#                 never removed here.
#   --demo        remove the synthetic demo dataset (see below)
#   --all         all of the above
#
# --demo delegates to Templates/Scripts/seed_demo_content.py --remove-all, so it
# deletes exactly the notes carrying a demo_seed marker - the dated slice that
# script generates AND the static demo notes the template ships (the Widget,
# Wanda cast and friends). Notes without the marker are never considered, so
# this cannot reach your real content. It is opt-in because on a vault you have
# started using, demo notes are indistinguishable from notes at a glance and you
# may have linked to them; and because in a repo checkout the static half is
# tracked, so removing it shows up as deletions to commit or revert.
#
# NEVER touched (remove by hand if you really want to):
#   - your vault content / notes (the ~/Obsidian folder itself) - the demo
#     dataset above is the one exception, and only with --demo
#   - Obsidian.app, Homebrew, and brew packages (python, yt-dlp, ffmpeg,
#     ollama, Docker Desktop)
#   - Ollama models (remove with: ollama rm <model>)
#   - ~/SourceMedia/ and its VoiceInput/ PodcastInput/ drop folders. Created by
#     the installer; kept on purpose, because they can hold captures that have
#     arrived from a device and not yet been processed. Deleting them would
#     discard the user's own recordings, which no reinstall can bring back.
#     Remove by hand once you have confirmed they are empty.
#
# Usage:
#   ./uninstall.sh                 interactive; safe defaults
#   ./uninstall.sh --yes           non-interactive; safe defaults
#   ./uninstall.sh --all --yes     full teardown (for rebuilding test envs)
#   ./uninstall.sh --dry-run       print what would happen, change nothing
#   ./uninstall.sh --vault PATH    operate on a vault other than ~/Obsidian
#   ./uninstall.sh --llm --secrets remove those extras too
#   ./uninstall.sh --demo --dry-run  list the demo notes that would be deleted
#   ./uninstall.sh -h | --help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export REPO_ROOT="$SCRIPT_DIR"

# Reuse the installer's logging/confirm/run helpers.
# shellcheck source=installers/lib/common.sh
source "$REPO_ROOT/installers/lib/common.sh"
# secrets.sh is safe to source: variable assignments and function definitions
# only, no prompts. Needed for _security -- the bounded, latching wrapper for
# /usr/bin/security. See the Keychain section for why a bare call is not
# acceptable here.
source "$REPO_ROOT/installers/lib/secrets.sh"

# ---- defaults & argument parsing -------------------------------------------
INTERACTIVE=1
DRY_RUN=0
DO_LLM=0
DO_SECRETS=0
DO_NEWSYSLOG=0
DO_PLUGINS=0
DO_DEMO=0
VAULT="$HOME/Obsidian"

usage() { sed -n '2,/^set -euo/{ /^set -euo/d; s/^# \{0,1\}//; p; }' "$0"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes)     INTERACTIVE=0 ;;
        --dry-run)    DRY_RUN=1 ;;
        --llm)        DO_LLM=1 ;;
        --secrets)    DO_SECRETS=1 ;;
        --newsyslog)  DO_NEWSYSLOG=1 ;;
        --plugins)    DO_PLUGINS=1 ;;
        --demo)       DO_DEMO=1 ;;
        --all)        DO_LLM=1; DO_SECRETS=1; DO_NEWSYSLOG=1; DO_PLUGINS=1; DO_DEMO=1 ;;
        --vault)      shift; VAULT="${1:?--vault needs a path}" ;;
        -h|--help)    usage; exit 0 ;;
        *)            err "Unknown argument: $1"; echo; usage; exit 2 ;;
    esac
    shift
done

export INTERACTIVE DRY_RUN

# $USER can be unset in some non-login shells; keep set -u happy.
: "${USER:=$(id -un)}"

VAULT_SCRIPTS="$VAULT/Templates/Scripts"
LA_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"

# Canonical LaunchAgent labels the installer may have loaded.
LABELS=(
    com.tag-clippings
    com.voice-cleanup
    com.obsidian.strip-ads
    com.obsidian.meeting-prep
    com.obsidian.security.plugin-check
    com.obsidian.security.integrity
    com.obsidian.security.proc-audit
    com.obsidian-rag-sync
    com.obsidian.group-photos
    com.meeting-prepopulate
    com.obsidian.meeting-pull
    com.obsidian.vault-lint
    com.obsidian.source-mail-pull
    com.obsidian.podcast-watch
    com.morning-dashboard
    com.obsidian.handoff-blob-pull
    com.obsidian.classify
)

# Log basenames the agents write under ~/Library/Logs (.log and .err).
LOGS=(
    tag-clippings voice-cleanup strip-ads meeting-prep
    obsidian-security obsidian-rag-sync group-photos meeting-prepopulate
    obsidian-template-install vault-lint
    source-mail-pull podcast-watch morning-dashboard meeting-pull
    handoff-blob-pull dashboard-actions obsidian-classify
)

# rm_path <path>: delete a file/dir/symlink if it exists (dry-run aware).
rm_path() {
    local p="$1"
    if [[ -e "$p" || -L "$p" ]]; then
        run rm -rf "$p"
        # Verify rather than trust the exit status. The Windows twin of this
        # helper was observed reporting a successful delete for a state
        # directory that was still on disk (2026-08-25); a teardown that says
        # "removed" about something still present means the next install
        # silently inherits state it was supposed to start without.
        if [[ "$DRY_RUN" -eq 1 ]]; then
            # A dry run removed nothing, so it must not say "removed". The old
            # wording put 24 lines of "removed: <path>" into a --dry-run
            # capture; anyone grepping for `removed:`, or pasting that log into
            # a packet as teardown evidence, would read it as a real teardown.
            ok "  would remove: $p"
        elif [[ ! -e "$p" && ! -L "$p" ]]; then
            ok "  removed: $p"
        else
            warn "  reported removed but still present: $p"
        fi
    fi
}

# ---- banner & plan ---------------------------------------------------------
printf "\n%s%s===================================================%s\n" "$BOLD" "$CYAN" "$RESET"
printf "%s%s Obsidian Second-Brain Template - uninstall.sh      %s\n" "$BOLD" "$CYAN" "$RESET"
printf "%s%s===================================================%s\n\n" "$BOLD" "$CYAN" "$RESET"
info "Vault:        $VAULT"
[[ "$DRY_RUN" -eq 1 ]] && warn "DRY RUN - nothing will be changed."
echo
info "Will remove:  LaunchAgents, wrapper apps, agent logs, regenerable state"
info "Extras:       llm=$DO_LLM  secrets=$DO_SECRETS  newsyslog=$DO_NEWSYSLOG  plugins=$DO_PLUGINS  demo=$DO_DEMO"
if [[ "$DO_DEMO" -eq 1 ]]; then
    info "Never touched: Obsidian.app, Homebrew/brew packages, Ollama models, and"
    info "               every vault note except the demo_seed-marked demo dataset"
else
    info "Never touched: vault notes, Obsidian.app, Homebrew/brew packages, Ollama models"
fi
echo

if [[ "$INTERACTIVE" -eq 1 ]]; then
    if ! confirm "Proceed with uninstall?" Y; then
        info "aborted; nothing changed."
        exit 0
    fi
fi

# ---- 1. LaunchAgents -------------------------------------------------------
section "LaunchAgents"
uid="$(id -u)"
for lbl in "${LABELS[@]}"; do
    plist="$LA_DIR/$lbl.plist"
    # Unload however it was loaded: bootout by label is the reliable path;
    # fall back to unload-by-plist. Both are best-effort.
    if [[ -f "$plist" ]]; then
        run launchctl unload "$plist" 2>/dev/null || true
    fi
    run launchctl bootout "gui/$uid/$lbl" 2>/dev/null || true
    rm_path "$plist"
done
ok "LaunchAgents unloaded and removed"

# ---- 2. Wrapper apps -------------------------------------------------------
section "Wrapper apps"
rm_path "$HOME/Applications/Markitdown Dropper.app"
rm_path "$HOME/Applications/DashboardActions.app"

# ---- 3. Agent logs ---------------------------------------------------------
section "Logs"
for n in "${LOGS[@]}"; do
    rm_path "$LOG_DIR/$n.log"
    rm_path "$LOG_DIR/$n.err"
done

# ---- 4. Regenerable state --------------------------------------------------
section "Regenerable state"
do_state=1
if [[ "$INTERACTIVE" -eq 1 ]]; then
    confirm "Remove the per-vault .venv and scratch state (regenerated on reinstall)?" Y || do_state=0
fi
if [[ "$do_state" -eq 1 ]]; then
    rm_path "$VAULT_SCRIPTS/.venv"
    rm_path "$VAULT_SCRIPTS/logs"
    rm_path "$VAULT_SCRIPTS/.locks"
    rm_path "$VAULT_SCRIPTS/.state"
    # .config is NOT removed. It holds meeting_pull.json and
    # meeting_prepopulate.json -- hand-built configuration the prompt above
    # would otherwise be lying about, since a reinstall only recreates them by
    # re-asking component 54's questions and install.ps1 never provisions them
    # at all. Same principle as each plugin's data.json: if a reinstall cannot
    # put it back, a teardown does not take it.
    rm_path "$VAULT/.tag_tracking.json"
    rm_path "$VAULT_SCRIPTS/.tag_tracking.json"
    rm_path "$HOME/.local/share/obsidian-security"
    [[ -d "$VAULT_SCRIPTS/.config" ]] && \
        info "  kept $VAULT_SCRIPTS/.config (hand-built job configuration)"
else
    info "  kept runtime state."
fi

# ---- 5. Open WebUI / Docker (opt-in: --llm) --------------------------------
if [[ "$DO_LLM" -eq 1 ]]; then
    section "Open WebUI (Docker)"
    if has_cmd docker && docker info >/dev/null 2>&1; then
        if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^open-webui$'; then
            run docker stop open-webui >/dev/null 2>&1 || true
            run docker rm open-webui   >/dev/null 2>&1 || true
            ok "  removed container: open-webui"
        else
            info "  no open-webui container found"
        fi
        if docker volume ls --format '{{.Name}}' 2>/dev/null | grep -q '^open-webui$'; then
            if [[ "$INTERACTIVE" -eq 0 ]] || confirm "Also delete the open-webui Docker volume (its data)?" N; then
                run docker volume rm open-webui >/dev/null 2>&1 || true
                ok "  removed volume: open-webui"
            fi
        fi
    else
        warn "  docker not available; skipping (start Docker Desktop to remove the container)"
    fi
    info "  Ollama and its models are left in place; remove models with: ollama rm <model>"
fi

# ---- 6. Secrets (opt-in: --secrets) ----------------------------------------
if [[ "$DO_SECRETS" -eq 1 ]]; then
    section "Secrets"
    # $HOME/dev/secrets/.env is NOT deleted, deliberately -- matching what the
    # Windows uninstaller already does and states: "other tools may share it,
    # this uninstaller doesn't own it and never deletes it." That directory is
    # a machine-wide secrets location, not a per-project one, so removing the
    # file to uninstall one project can break everything else reading it.
    # Name this project's keys instead and let the operator edit.
    ENV_FILE="$HOME/dev/secrets/.env"
    if [[ -f "$ENV_FILE" ]]; then
        info "  keeping $ENV_FILE - a shared secrets file this project does"
        info "  not own. To remove only this project's entries, delete these"
        info "  keys by hand:"
        info "    LLM_BASE_URL, LLM_API_KEY_NAME, OPEN_WEBUI_API_KEY,"
        info "    SOURCE_MAIL_*, TAGGER_*, CLASSIFIER_*"
    fi
    # gemini_api_key is LEGACY: the YouTube summarizer used Gemini before it
    # moved to llm_endpoint, and nothing writes this item any more. It stays in
    # the list on purpose - an install predating that change still holds the
    # key, and uninstall is the right place to offer a stale credential for
    # deletion rather than leaving it in the Keychain forever.
    # Every Keychain call goes through _security: /usr/bin/security -- not
    # whatever `security` resolves to on PATH -- under a hard timeout, latching
    # so the first hang skips the Keychain for the rest of the run.
    #
    # Not defensive styling. A `security` call blocked on a consent dialog
    # nobody can answer wedges the whole Keychain stack for the user, including
    # git-over-HTTPS through the osxkeychain helper, and every retry queues
    # another dialog. secret_store.py was hardened for exactly this on
    # 2026-08-20 and this caller was missed -- so an uninstall could still wedge
    # a machine whose Keychain was already unhappy, which is the state someone
    # reaching for uninstall.sh is most likely to be in.
    for svc in gemini_api_key obsidian-allowlist-hmac; do
        if _security "$KEYCHAIN_TIMEOUT_SECONDS" find-generic-password \
                -a "$USER" -s "$svc" >/dev/null 2>&1; then
            if [[ "$INTERACTIVE" -eq 0 ]] || confirm "Delete Keychain entry '$svc'?" N; then
                if [[ "$DRY_RUN" -eq 1 ]]; then
                    info "  [dry-run] would delete Keychain entry: $svc"
                elif _security "$KEYCHAIN_TIMEOUT_SECONDS" delete-generic-password \
                        -a "$USER" -s "$svc" >/dev/null 2>&1; then
                    ok "  removed Keychain entry: $svc"
                else
                    warn "  could not remove Keychain entry: $svc"
                fi
            fi
        fi
    done
fi

# ---- 7. newsyslog rotation config (opt-in: --newsyslog, sudo) --------------
if [[ "$DO_NEWSYSLOG" -eq 1 ]]; then
    section "Log rotation (newsyslog)"
    NS="/etc/newsyslog.d/obsidian-security.conf"
    if [[ -f "$NS" ]]; then
        info "  removing $NS (sudo required)"
        run sudo rm -f "$NS" && ok "  removed: $NS"
    else
        info "  $NS not present"
    fi
fi

# ---- 8. Community plugins (opt-in: --plugins) ------------------------------
# Removes the DOWNLOADED artifacts named in installers/plugin-pins.json, never
# the plugins tree. Each plugin folder also holds data.json - the settings that
# drive ribbon icons, QuickAdd choices, Templater config - which is user
# content and is NOT restored by a reinstall: 30-plugins.sh fetches only
# manifest.json / main.js / styles.css. The Windows twin of this step deleted
# the tree wholesale and destroyed 464 lines of tracked configuration in a
# 2026-08-25 teardown. In a repo clone that is recoverable with git checkout;
# in a DEPLOYED vault, which is the normal case here and is not under version
# control, it is not recoverable at all. Removing only the pinned filenames
# preserves data.json by construction, with no allow-list to keep in sync.
if [[ "$DO_PLUGINS" -eq 1 ]]; then
    section "Community plugins"
    PLUGINS="$VAULT/.obsidian/plugins"
    PINS="$REPO_ROOT/installers/plugin-pins.json"
    if [[ ! -d "$PLUGINS" ]]; then
        info "  no plugins directory at $PLUGINS"
    elif [[ ! -f "$PINS" ]]; then
        warn "  $PINS not found - cannot tell downloaded artifacts from your"
        warn "  settings, so nothing was removed. Delete by hand if you are"
        warn "  sure, keeping each plugin's data.json."
    elif [[ "$INTERACTIVE" -eq 0 ]] || confirm "Remove fetched plugin files at $PLUGINS (settings kept)?" N; then
        while IFS=$'\t' read -r pid fname; do
            [[ -n "$pid" ]] || continue
            rm_path "$PLUGINS/$pid/$fname"
        done < <(python3 -c '
import json, sys
for pin in json.load(open(sys.argv[1])):
    for fname in pin.get("files", {}):
        print(pin["id"], fname, sep="\t")
' "$PINS")
        # Only if nothing survives. A folder still holding data.json - or
        # anything else the pin manifest does not name - keeps its directory.
        for d in "$PLUGINS"/*/; do
            [[ -d "$d" ]] || continue
            if [[ -z "$(ls -A "$d" 2>/dev/null)" ]]; then
                rm_path "${d%/}"
            else
                info "  kept $(basename "${d%/}")/ ($(ls -A "$d" | wc -l | tr -d ' ') file(s) that are not downloaded artifacts, e.g. data.json)"
            fi
        done
    fi
fi

# ---- 9. Synthetic demo dataset (opt-in: --demo) ----------------------------
if [[ "$DO_DEMO" -eq 1 ]]; then
    section "Demo content"
    SEEDER="$VAULT_SCRIPTS/seed_demo_content.py"
    if [[ ! -f "$SEEDER" ]]; then
        warn "  no seed_demo_content.py at $SEEDER; skipping"
    elif ! has_cmd python3; then
        warn "  python3 not on PATH; skipping (the seeder is stdlib-only, so any python3 works)"
    else
        # Not routed through run(): in a dry run the seeder's own --dry-run
        # prints the actual file list, which is the thing worth seeing here,
        # where run() would only echo the command. The seeder is stdlib-only,
        # so this deliberately uses the system python3 rather than the vault
        # .venv, which section 4 has already deleted by now.
        demo_args=(--remove-all)
        [[ "$DRY_RUN" -eq 1 ]] && demo_args+=(--dry-run)
        if [[ "$INTERACTIVE" -eq 0 ]] || confirm "Delete the demo dataset (every note marked demo_seed) from $VAULT?" N; then
            if OBSIDIAN_VAULT="$VAULT" python3 "$SEEDER" "${demo_args[@]}"; then
                [[ "$DRY_RUN" -eq 1 ]] || ok "  demo dataset removed"
            else
                warn "  seed_demo_content.py exited non-zero; demo content may remain"
            fi
        else
            info "  kept demo content."
        fi
    fi
fi

# ---- done ------------------------------------------------------------------
echo
section "uninstall.sh complete"
[[ "$DRY_RUN" -eq 1 ]] && warn "(dry run - nothing was actually changed)"
info "Left in place: your vault at $VAULT, Obsidian.app, Homebrew + brew packages, Ollama models."
info "To remove the vault itself (this also removes the cloned template): rm -rf \"$VAULT\""
