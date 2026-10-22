#!/usr/bin/env bash
# update.sh - bring an existing macOS install up to date in one command.
#
# What it does, in order
# ----------------------
#   1. git pull --ff-only     refuses first if a tracked file has local changes,
#                             or installer code is modified, untracked or hidden
#                             (the tree gate: installers/lib/tree_gate.sh)
#   2. requirements           the same pip step 10-vault-bootstrap runs
#   3. scheduled jobs         re-render and reload the LaunchAgents THIS machine
#                             already has, where the template changed
#   4. plugins                reinstall the pinned plugins if the pins moved or an
#                             installed plugin no longer matches its pin
#   5. script permissions     re-tighten mode bits on Templates/Scripts
#   6. security controls      report their status; never adopt a baseline
#                             (step 3 also refreshes their installed copy)
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
#   ./update.sh --allow-local-changes
#                            run over your own edits to installer code (a
#                            maintainer); logged, and passed to every step
#
# The repo must be the vault: ~/Obsidian is this clone, or a symlink to it.

set -euo pipefail

# Everything runs inside main(), called on the last line. bash reads a script as
# it executes, and step 1 can rewrite this very file; inside a function the whole
# body is parsed before anything runs.
main() {
    local REPO_ROOT DRY_RUN=0 AFTER_PULL=0 FROM="" TG_ALLOW=0
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    export REPO_ROOT

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run)    DRY_RUN=1 ;;
            --after-pull) AFTER_PULL=1 ;;
            --from)       shift; FROM="${1:-}" ;;
            --allow-local-changes) TG_ALLOW=1 ;;
            -h|--help)    sed -n '/^# /s/^# //p' "$0"; return 0 ;;
            *)            echo "Unknown argument: $1" >&2; return 2 ;;
        esac
        shift
    done

    # ---- tree gate (findings 5278120 / 5278133) ----------------------------
    # Before anything else under installers/ is sourced: tree_gate.sh refuses
    # installer code git does not vouch for, and these lines check tree_gate.sh
    # itself against HEAD first. Nothing can check THIS file (see tree_gate.sh).
    # Both passes run it: the --after-pull pass sources the libraries too.
    if [[ "$TG_ALLOW" -eq 0 && -e "$REPO_ROOT/.git" ]] && command -v git >/dev/null 2>&1 \
       && { [[ -L "$REPO_ROOT/installers/lib/tree_gate.sh" ]] \
            || [[ "$(git -C "$REPO_ROOT" hash-object -- installers/lib/tree_gate.sh 2>/dev/null)" \
                  != "$(git -C "$REPO_ROOT" rev-parse -q --verify HEAD:installers/lib/tree_gate.sh 2>/dev/null)" ]]; }; then
        echo "TREE GATE: REFUSED. installers/lib/tree_gate.sh differs from HEAD; not sourcing it." >&2
        echo "  Inspect: git -C \"$REPO_ROOT\" status --untracked-files=all; git -C \"$REPO_ROOT\" diff" >&2
        return 1
    fi
    # shellcheck source=installers/lib/tree_gate.sh
    source "$REPO_ROOT/installers/lib/tree_gate.sh"
    # update.sh only runs on a git checkout (it pulls), so a tree git cannot
    # read is refused here rather than warned about.
    tree_gate_check "$REPO_ROOT" install "$TG_ALLOW" 1 || return 1
    # Passed on to the install.sh steps below and to the --after-pull pass.
    local -a gate_args=()
    [[ "$TG_ALLOW" -eq 1 ]] && gate_args=(--allow-local-changes)

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
        if obsidian_running; then
            if [[ "$DRY_RUN" -eq 1 ]]; then
                warn "  Obsidian is running; a real update would stop here until you quit it."
            else
                err "Obsidian is running; not updating."
                err "Quit Obsidian (Cmd+Q), then run this update again. While it is open it keeps"
                err "the old plugins loaded and writes its own copy of the settings back over the"
                err "updated ones."
                return 1
            fi
        fi
        # The one tree check, in update mode: installer code (modified,
        # untracked or hidden) as at the top, plus any tracked change anywhere,
        # which a pull must not run over. After the Obsidian check, because an
        # open Obsidian is what usually dirties a tracked settings file.
        tree_gate_check "$REPO_ROOT" update "$TG_ALLOW" 1 || return 1
        FROM="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
        if [[ "$DRY_RUN" -eq 1 ]]; then
            info "  dry run: not pulling; reporting against the current checkout ($FROM)."
        else
            git -C "$REPO_ROOT" pull --ff-only
            # Continue from the copy just pulled, so steps 2-6 are the new code.
            exec /bin/bash "$REPO_ROOT/update.sh" --after-pull --from "$FROM" ${gate_args[@]+"${gate_args[@]}"}
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
    # The security agents run an installed copy of the controls outside the
    # vault (5278112). Refreshed from the scripts just pulled, and before any
    # agent is reloaded: their plists name that copy.
    if [[ -f "$LA/com.obsidian.security.integrity.plist" || -f "$LA/com.obsidian.security.plugin-check.plist" ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            info "  dry run: would refresh the installed security controls in $SECURITY_CONTROLS_DIR"
        elif install_security_controls "$VAULT/Templates/Scripts"; then
            ok "  security controls installed in $SECURITY_CONTROLS_DIR"
        else
            err "  Could not install the security controls into $SECURITY_CONTROLS_DIR (see above);"
            err "  not reloading any job. Fix that, then run this update again."
            return 1
        fi
    fi
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
    # Reinstall when the pull moved the pins, or when an installed plugin is not
    # its pinned copy: a re-run after a failed download, or an install that
    # drifted from its pins (plugin_drift.py checks each file against its pin).
    local plugin_reason="" drift plugins_failed=0
    if pins_changed "$REPO_ROOT" "$FROM"; then
        plugin_reason="the plugin pins changed"
    elif drift="$(plugins_drifted "$VAULT")"; then
        plugin_reason="$drift"
    fi
    if [[ -n "$plugin_reason" ]]; then
        if [[ "$DRY_RUN" -eq 1 ]]; then
            info "  dry run: $plugin_reason; would reinstall plugins (30) and re-patch QuickAdd (31)"
        else
            info "  $plugin_reason; reinstalling from the pins"
            # A failure must not stop the steps after it: the QuickAdd patch
            # (a reinstall brings back the unpatched bundle), the retired-plugin
            # removal, permissions and the control report all still run, and
            # the update ends INCOMPLETE instead.
            if ! "$REPO_ROOT/install.sh" --auto --only 30-plugins ${gate_args[@]+"${gate_args[@]}"}; then
                plugins_failed=1
                err "  Plugins NOT updated (see the lines above)."
            elif [[ "$plugin_reason" == "the plugin pins changed" ]]; then
                warn "  The plugin integrity check will now report the new plugin files. Vet what"
                warn "  changed (git diff $FROM $TO -- installers/plugin-pins.json) before adopting it."
            else
                warn "  The plugins were brought back to their pins. The plugin integrity check will"
                warn "  report any whose vetted version differs; vet those before adopting."
            fi
        fi
    else
        ok "  every plugin is at its pin; left as it is"
    fi
    # The QuickAdd patch, on every update: it is idempotent, a reinstall brings
    # back the unpatched bundle, and QuickAdd's patched main.js is exempt from
    # the drift check -- so a patch that failed once is retried here.
    if [[ "$DRY_RUN" -ne 1 ]]; then
        "$REPO_ROOT/install.sh" --auto --only 31-quickadd-patch ${gate_args[@]+"${gate_args[@]}"} || warn "  QuickAdd patch step failed"
        # Every update too: a clone made before this step existed has no
        # ignore list, and the plugin then stamps the docs this pull changed.
        "$REPO_ROOT/install.sh" --auto --only 32-timestamp-ignores ${gate_args[@]+"${gate_args[@]}"} || warn "  timestamp-ignores step failed"
    fi
    retire_plugins "$VAULT" "${backup:-$HOME/Library/Logs/obsidian-template-update/$(date +%Y%m%d-%H%M%S)}" "$DRY_RUN"

    # ---- 5. script permissions ----------------------------------------------
    info "== 5/6 script permissions =="
    if [[ "$DRY_RUN" -eq 1 ]]; then
        info "  dry run: would run ./install.sh --auto --only 56-script-permissions"
    else
        "$REPO_ROOT/install.sh" --auto --only 56-script-permissions ${gate_args[@]+"${gate_args[@]}"}
    fi

    # Local RAG stack, report only, where a container exists: the binding
    # (the installer repairs it; an update only says so) and the image pin.
    # OBSIDIAN_DOCKER: the tests point it at a stub, so they never touch the
    # maintainer's real Docker.
    RAG_DOCKER="${OBSIDIAN_DOCKER:-docker}"
    if has_cmd "$RAG_DOCKER" && "$RAG_DOCKER" ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^open-webui$'; then
        info "== local RAG stack =="
        rag_exposed="$("$RAG_DOCKER" port open-webui 8080 2>/dev/null | grep -v '^127\.0\.0\.1:' || true)"
        if [[ -n "$rag_exposed" ]]; then
            warn "  open-webui is published beyond this Mac; fix with: ./install.sh --only 50-llm-rag"
        fi
        rag_digest="$(python3 "$REPO_ROOT/installers/lib/rag_pins.py" image-digest 2>/dev/null || true)"
        if [[ -z "$rag_digest" ]]; then
            warn "  installers/rag-pins.json is missing or unreadable; cannot check the open-webui image pin"
        else
            rag_image_id="$("$RAG_DOCKER" inspect open-webui --format '{{.Image}}' 2>/dev/null || true)"
            if "$RAG_DOCKER" image inspect "$rag_image_id" --format '{{join .RepoDigests " "}}' 2>/dev/null | grep -q "@$rag_digest"; then
                ok "  open-webui runs the pinned image"
            else
                warn "  open-webui is not on the pinned image ($(python3 "$REPO_ROOT/installers/lib/rag_pins.py" image));"
                warn "  move with: docker pull <that image> && docker rm -f open-webui && ./install.sh --only 50-llm-rag"
            fi
        fi
        # Login and CORS (finding 5268557 / N1): a browser page reaches loopback.
        rag_findings="$("$RAG_DOCKER" inspect open-webui 2>/dev/null \
            | python3 "$REPO_ROOT/installers/lib/rag_pins.py" check-container 2>/dev/null || true)"
        if [[ -n "$rag_findings" ]]; then
            while IFS= read -r line; do warn "  open-webui: $line"; done <<< "$rag_findings"
            warn "  fix with: docker rm -f open-webui && ./install.sh --only 50-llm-rag (volume kept)"
        fi
        if has_cmd ollama; then
            python3 "$REPO_ROOT/installers/lib/rag_pins.py" verify-models 2>/dev/null \
                || warn "  a pinned Ollama model differs from its pin (see above), or Ollama is not running"
        fi
    fi

    # ---- 6. security controls (report only) ---------------------------------
    # Adopting a baseline is a decision for the person reading this, after
    # checking that the drift is the update and nothing else. --json only reads.
    info "== 6/6 security controls =="
    # N8: when the tree was already at TO (pulled by hand), FROM..TO is empty;
    # diff from the commit the last completed update ended at instead.
    local diff_from="$FROM" last_update=""
    last_update="$(cat "$UPDATE_RECORD" 2>/dev/null || true)"
    if [[ "$FROM" == "$TO" ]]; then
        if [[ -n "$last_update" && "$last_update" != "$TO" ]]; then
            diff_from="$last_update"
            info "  The tree was already at $TO when this update started; the diffs below"
            info "  start at $last_update, where the last completed update ended."
        else
            info "  The tree was already at $TO when this update started, so a diff from it"
            info "  shows nothing: compare against the commit your last baseline was taken at."
        fi
    fi
    report_control "integrity monitor" integrity_monitor.py "$diff_from" "$TO"
    report_control "plugin integrity check" plugin_integrity_check.py "$diff_from" "$TO"

    info ""
    if [[ "$plugins_failed" -eq 1 ]]; then
        err "Update INCOMPLETE: everything else is updated, but the plugins are not (see"
        err "\"Plugins NOT updated\" above). A hash mismatch means upstream changed a file under"
        err "its pin: do not work around it. A download failure is safe to retry: run ./update.sh again."
        err "Don't adopt a new plugin baseline until an update completes."
        return 1
    fi
    if [[ "$DRY_RUN" -eq 1 ]]; then
        ok "Dry run at $TO: nothing was changed."
    elif [[ "$FROM" == "$TO" ]]; then
        ok "Already at $TO; requirements, jobs and permissions refreshed."
    else
        ok "Updated $FROM -> $TO."
    fi
    if [[ "$DRY_RUN" -eq 0 ]]; then
        ( umask 077; mkdir -p "$(dirname "$UPDATE_RECORD")" && printf '%s\n' "$TO" > "$UPDATE_RECORD" ) 2>/dev/null || true
    fi
}

# The commit the last completed update ended at (N8).
UPDATE_RECORD="$HOME/.local/share/obsidian-template/last-update-commit"

# report_control <name> <script> <from> <to>
report_control() {
    local name="$1" script="$2" from="$3" to="$4" status
    # The installed copy, as the agents run it (5278112).
    local path="$SECURITY_CONTROLS_DIR/$script"
    # Hard 180 s ceiling: both controls read the Keychain, and a consent
    # prompt nobody can see would otherwise hang the update indefinitely. Not
    # 60: the integrity check hashes the virtualenv, under a minute from a
    # cold disk cache, and a timeout here reads as "unreadable".
    # `|| true` on the control: it exits 1 on drift and 2 with no baseline, and
    # under pipefail that used to append "unreadable" to the status it printed,
    # so "no baseline" fell through to the drift advice. The parser is -I too:
    # -c would put the current directory first on its path.
    status="$({ /usr/bin/perl -e 'alarm 180; exec @ARGV' /usr/bin/python3 -I "$path" --json 2>/dev/null || true; } \
        | /usr/bin/python3 -I -c 'import json,sys; print(json.load(sys.stdin).get("status","unreadable"))' \
        2>/dev/null || echo unreadable)"
    case "$status" in
        ok)          ok "  $name: clean" ;;
        no_baseline) warn "  $name: no baseline, so this control is not active."
                     warn "    Setting one up is a deliberate step: see docs/Security-Harness.md." ;;
        *)
            warn "  $name: $status. An update changes watched files, so this is expected."
            warn "    Before adopting, confirm every finding is part of this update:"
            warn "      findings:  /usr/bin/python3 -I $path --json"
            warn "      the diff:  git -C ~/Obsidian diff --name-status $from $to"
            warn "    Every file finding (scripts, templates, installers, agent_config, script_config)"
            warn "    must be a file in that diff; venv findings fit only if requirements.lock"
            warn "    is in it, or you rebuilt the venv with install.sh since the last baseline"
            warn "    and .venv/bin/python -m pip check is clean; plist findings must be jobs"
            warn "    this update reloaded. An INSTALLED_COPY_MISMATCH means the installed"
            warn "    controls differ from the vault's: never the update, which refreshed them. A state_dir,"
            warn "    secrets, PERMISSIONS or BULK_DELETE finding is NOT the update, except a"
            warn "    state_dir plugin_allowlist.json when you re-vetted plugins since. Plugin"
            warn "    findings must be plugins or settings files this update touched (the diff"
            warn "    under .obsidian/ and Templates/): a REMOVED plugin it retired, or QuickAdd"
            warn "    choices and the Templates/QuickAdd scripts it changed. Then:"
            warn "      /usr/bin/python3 -I $path --update"
            ;;
    esac
}

main "$@"
