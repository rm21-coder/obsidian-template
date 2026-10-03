#!/usr/bin/env bash
# update.sh - bring an existing macOS install up to date in one command.
#
# What it does, in order
# ----------------------
#   1. git pull --ff-only     refuses first if a tracked file has local changes
#   2. requirements           the same pip step 10-vault-bootstrap runs
#   3. scheduled jobs         re-render and reload the LaunchAgents THIS machine
#                             already has, where the template changed
#   4. plugins                reinstall the pinned plugins, only if the pins moved
#   5. script permissions     re-tighten mode bits on Templates/Scripts
#   6. security controls      report their status; never adopt a baseline
#
# Why not just re-run ./install.sh
# --------------------------------
# install.sh does not record which components a person declined. Re-run with
# --auto it installs every component, including the local LLM stack (Ollama,
# Docker, Open WebUI, a sudo sysctl). Re-run interactively it re-asks every
# question. This updates only what is already installed. A job that is new
# upstream, or that was declined, is listed with the command that adds it.
#
# A changed scheduled job overwrites the installed plist, so any hand edit to it
# is replaced. The diff is printed first and the old plist is backed up to
# ~/Library/Logs/obsidian-template-update/<timestamp>/.
#
# Usage
# -----
#   ./update.sh              update
#   ./update.sh --dry-run    say what would change, change nothing (no pull)
#
# The repo must be the vault: ~/Obsidian is this clone, or a symlink to it.

set -euo pipefail

# Everything runs inside main(), called on the last line. bash reads a script as
# it executes, and step 1 can rewrite this very file; inside a function the whole
# body is parsed before anything runs.
main() {
    local REPO_ROOT DRY_RUN=0 AFTER_PULL=0 FROM=""
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    export REPO_ROOT

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run)    DRY_RUN=1 ;;
            --after-pull) AFTER_PULL=1 ;;
            --from)       shift; FROM="${1:-}" ;;
            -h|--help)    sed -n '/^# /s/^# //p' "$0"; return 0 ;;
            *)            echo "Unknown argument: $1" >&2; return 2 ;;
        esac
        shift
    done

    # shellcheck source=installers/lib/common.sh
    source "$REPO_ROOT/installers/lib/common.sh"
    # shellcheck source=installers/lib/plist.sh
    source "$REPO_ROOT/installers/lib/plist.sh"
    # shellcheck source=installers/lib/update.sh
    source "$REPO_ROOT/installers/lib/update.sh"

    local VAULT="$HOME/Obsidian"
    local LA="$HOME/Library/LaunchAgents"
    local VENV_PY="$VAULT/Templates/Scripts/.venv/bin/python3"

    if [[ "$(uname -s)" != "Darwin" ]]; then
        err "update.sh is the macOS updater. On Windows run Templates/Scripts/windows/update.ps1."
        return 1
    fi
    if ! vault_is_repo "$REPO_ROOT" "$VAULT"; then
        err "$VAULT is not this repository ($REPO_ROOT)."
        err "update.sh updates a vault that IS its clone (or a symlink to it), the layout"
        err "install.sh sets up. A vault deployed some other way is updated by whoever set it up."
        return 1
    fi
    if [[ ! -x "$VENV_PY" ]]; then
        err "No venv at $VENV_PY: this is not an existing install. Run ./install.sh."
        return 1
    fi

    # ---- 1. pull ------------------------------------------------------------
    if [[ "$AFTER_PULL" -eq 0 ]]; then
        info "== 1/6 pull =="
        local dirty
        dirty="$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no)"
        if [[ -n "$dirty" ]]; then
            err "Local changes to tracked files; not updating:"
            printf '%s\n' "$dirty" | sed 's/^/  /'
            if [[ "$dirty" =~ ^\ ?M\ \.obsidian/types\.json$ ]]; then
                info ""
                info "Obsidian rewrites .obsidian/types.json when it sees new note properties."
                info "It only records property types, so taking the upstream copy is safe:"
                info "  git -C \"$REPO_ROOT\" checkout -- .obsidian/types.json"
                info "Then run this update again."
            fi
            return 1
        fi
        FROM="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
        if [[ "$DRY_RUN" -eq 1 ]]; then
            info "  dry run: not pulling; reporting against the current checkout ($FROM)."
        else
            git -C "$REPO_ROOT" pull --ff-only
            # Continue from the copy just pulled, so steps 2-6 are the new code.
            exec /bin/bash "$REPO_ROOT/update.sh" --after-pull --from "$FROM"
        fi
    fi
    local TO
    TO="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"

    # ---- 2. requirements ----------------------------------------------------
    info "== 2/6 requirements =="
    if [[ "$DRY_RUN" -eq 1 ]]; then
        info "  dry run: would install Templates/Scripts/requirements.lock (hash-checked)"
    else
        install_requirements "$VENV_PY" "$VAULT/Templates/Scripts/requirements.txt"
        ok "  requirements installed"
        # The Markitdown Dropper app keeps its own venv (component 43), locked
        # for Python 3.13 only. An older install may have built it on another
        # Python; say so and carry on -- the app is optional, the update isn't.
        local dpy="$HOME/.markitdown-dropper-venv/bin/python3"
        if [[ -x "$dpy" ]]; then
            if [[ "$("$dpy" -c 'import sys; print(sys.version_info[1])' 2>/dev/null)" != "13" ]]; then
                warn "  Markitdown Dropper venv is not Python 3.13; not refreshed. Remove"
                warn "  ~/.markitdown-dropper-venv and re-run ./install.sh to rebuild it from its lock."
            elif install_requirements "$dpy" "$VAULT/Templates/Scripts/requirements-dropper.txt" \
                    requirements-dropper.lock; then
                ok "  Markitdown Dropper requirements installed"
            else
                warn "  Markitdown Dropper requirements did not install; the app may not start"
            fi
        fi
    fi

    # ---- 3. scheduled jobs ----------------------------------------------------
    info "== 3/6 scheduled jobs =="
    local state label comp backup="" n_changed=0 not_installed=()
    while read -r state label comp; do
        case "$state" in
            unchanged) ;;
            changed)
                n_changed=$((n_changed + 1))
                info "  $label: definition changed"
                local tmp
                tmp="$(mktemp -t obsidian_update_plist)"
                render_to "$REPO_ROOT/Templates/Scripts/$label.plist" "$tmp"
                diff -u "$LA/$label.plist" "$tmp" | sed 's/^/      /' || true
                if [[ "$DRY_RUN" -eq 0 ]]; then
                    if [[ -z "$backup" ]]; then
                        backup="$HOME/Library/Logs/obsidian-template-update/$(date +%Y%m%d-%H%M%S)"
                        mkdir -p "$backup"
                    fi
                    cp -p "$LA/$label.plist" "$backup/$label.plist"
                    install -m 0644 "$tmp" "$LA/$label.plist"
                    launchctl_reload "$LA/$label.plist"
                    ok "  $label: updated and reloaded (old copy in $backup)"
                fi
                rm -f "$tmp"
                ;;
            not-installed) not_installed+=("$label $comp") ;;
        esac
    done < <(plan_agents "$REPO_ROOT" "$LA")
    [[ "$n_changed" -eq 0 ]] && ok "  every installed job already matches its template"
    retire_agents "$LA" "${backup:-$HOME/Library/Logs/obsidian-template-update/$(date +%Y%m%d-%H%M%S)}" "$DRY_RUN"
    if [[ ${#not_installed[@]} -gt 0 ]]; then
        info "  Not installed on this machine (declined at install, or new upstream); left alone:"
        local entry
        for entry in "${not_installed[@]}"; do
            label="${entry%% *}"; comp="${entry#* }"
            if [[ "$comp" == "-" ]]; then
                info "    $label  (no installer component; see the comment inside its plist)"
            else
                info "    $label  add with: ./install.sh --only $comp"
            fi
        done
    fi

    # ---- 4. plugins ---------------------------------------------------------
    info "== 4/6 plugins =="
    if pins_changed "$REPO_ROOT" "$FROM"; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            info "  dry run: the plugin pins changed; would reinstall plugins (30) and re-patch QuickAdd (31)"
        else
            info "  the plugin pins changed; reinstalling"
            "$REPO_ROOT/install.sh" --auto --only 30-plugins
            "$REPO_ROOT/install.sh" --auto --only 31-quickadd-patch
            warn "  The plugin integrity check will now report the new plugin files. Vet what"
            warn "  changed (git diff $FROM $TO -- installers/plugin-pins.json) before adopting it."
        fi
    else
        ok "  pins unchanged; plugins left as they are"
    fi

    # ---- 5. script permissions ----------------------------------------------
    info "== 5/6 script permissions =="
    if [[ "$DRY_RUN" -eq 1 ]]; then
        info "  dry run: would run ./install.sh --auto --only 56-script-permissions"
    else
        "$REPO_ROOT/install.sh" --auto --only 56-script-permissions
    fi

    # ---- 6. security controls (report only) ---------------------------------
    # Adopting a baseline is a decision for the person reading this, after
    # checking that the drift is the update and nothing else. --json only reads.
    info "== 6/6 security controls =="
    report_control "integrity monitor" integrity_monitor.py "$FROM" "$TO"
    report_control "plugin integrity check" plugin_integrity_check.py "$FROM" "$TO"

    info ""
    if [[ "$DRY_RUN" -eq 1 ]]; then
        ok "Dry run at $TO: nothing was changed."
    elif [[ "$FROM" == "$TO" ]]; then
        ok "Already at $TO; requirements, jobs and permissions refreshed."
    else
        ok "Updated $FROM -> $TO."
    fi
}

# report_control <name> <script> <from> <to>
report_control() {
    local name="$1" script="$2" from="$3" to="$4" status
    local path="$HOME/Obsidian/Templates/Scripts/$script"
    # Hard 60 s ceiling: both controls read the Keychain, and a consent
    # prompt nobody can see would otherwise hang the update indefinitely.
    status="$(/usr/bin/perl -e 'alarm 60; exec @ARGV' /usr/bin/python3 "$path" --json 2>/dev/null \
        | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("status","unreadable"))' \
        2>/dev/null || echo unreadable)"
    case "$status" in
        ok)          ok "  $name: clean" ;;
        no_baseline) warn "  $name: no baseline, so this control is not active."
                     warn "    Setting one up is a deliberate step: see docs/Security-Harness.md." ;;
        *)
            warn "  $name: $status. An update changes watched files, so this is expected."
            warn "    Before adopting, confirm every finding is part of this update:"
            warn "      findings:  /usr/bin/python3 $path --json"
            warn "      the diff:  git -C ~/Obsidian diff --name-status $from $to"
            warn "    Script and plist findings must be files this update changed or jobs it"
            warn "    reloaded; a state_dir or BULK_DELETE finding is NOT the update. Then:"
            warn "      /usr/bin/python3 $path --update"
            ;;
    esac
}

main "$@"
