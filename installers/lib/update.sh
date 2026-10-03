#!/usr/bin/env bash
# update.sh (lib) - the decisions behind ./update.sh, kept apart so they can be
# tested against a scratch HOME without touching a real machine.
#
# Sourced after common.sh and plist.sh.

# vault_is_repo <repo_root> <vault>
# True when <vault> is this repository, directly or through a symlink.
# Resolved, not string-compared: ~/Obsidian is often a symlink to the clone,
# and 10-vault-bootstrap's plain string test fails exactly that case.
vault_is_repo() {
    local repo="$1" vault="$2"
    [[ -d "$vault" ]] || return 1
    [[ "$repo" -ef "$vault" ]]
}

# component_for_label <repo_root> <label>
# The installer component that installs ~/Library/LaunchAgents/<label>.plist,
# by the exact template path it names. Empty if none does (an agent installed
# by hand, such as com.obsidian.claude-auth-check).
component_for_label() {
    local repo="$1" label="$2" f
    for f in "$repo"/installers/components/[0-9][0-9]-*.sh; do
        if grep -q "Templates/Scripts/${label}\.plist" "$f"; then
            basename "$f" .sh
            return 0
        fi
    done
    return 0
}

# render_to <template> <out>
# render_plist's substitution into a scratch file, for comparison. A template
# with no YOUR_USERNAME is copied as-is.
render_to() {
    local src="$1" out="$2"
    if grep -q YOUR_USERNAME "$src"; then
        render_plist "$src" "$out"
    else
        install -m 0644 "$src" "$out"
    fi
}

# plan_agents <repo_root> <launchagents_dir>
# One line per agent template, "<state> <label> <component>":
#   unchanged      installed, and identical to the template rendered for $USER
#   changed        installed, and different: the update re-renders and reloads it
#   not-installed  the template exists but this machine never installed it --
#                  either declined at install time or new upstream. Left alone:
#                  an update must not add a job the user did not choose.
plan_agents() {
    local repo="$1" la="$2" tpl label dst tmp state comp
    for tpl in "$repo"/Templates/Scripts/*.plist; do
        [[ -f "$tpl" ]] || continue
        label="$(basename "$tpl" .plist)"
        dst="$la/${label}.plist"
        comp="$(component_for_label "$repo" "$label")"
        if [[ ! -f "$dst" ]]; then
            state="not-installed"
        else
            tmp="$(mktemp -t obsidian_update_plist)"
            render_to "$tpl" "$tmp"
            if cmp -s "$tmp" "$dst"; then state="unchanged"; else state="changed"; fi
            rm -f "$tmp"
        fi
        printf '%s %s %s\n' "$state" "$label" "${comp:--}"
    done
}

# Jobs this template once installed and has since retired. An update removes
# them: nothing else would, and a retired job either fails on every run or
# runs code that no longer ships. Names only ever get added here.
#   com.obsidian.handoff-blob-pull   Azure Blob relay, removed 2026-09-30
RETIRED_AGENTS=(com.obsidian.handoff-blob-pull)

# retire_agents <launchagents_dir> <backup_dir> <dry_run>
# Unload and remove each retired agent that is still installed, keeping a copy
# of its plist in <backup_dir>. Prints one line per agent removed; a failure to
# unload is a warning, not a stop -- the plist is still moved aside.
retire_agents() {
    local la="$1" backup="$2" dry="$3" label
    for label in "${RETIRED_AGENTS[@]}"; do
        [[ -f "$la/$label.plist" ]] || continue
        if [[ "$dry" -eq 1 ]]; then
            info "  $label: retired upstream; dry run: would unload and remove it"
            continue
        fi
        launchctl bootout "gui/$(id -u)/$label" 2>/dev/null \
            || launchctl unload "$la/$label.plist" 2>/dev/null || true
        mkdir -p "$backup"
        if mv "$la/$label.plist" "$backup/$label.plist"; then
            ok "  $label: retired upstream; unloaded and removed (old copy in $backup)"
        else
            warn "  $label: retired upstream, but could not move its plist aside"
        fi
    done
}

# Community plugins the template once shipped and has since retired. An update
# disables and removes them: the enabled list arrives with the pull, and the
# plugin's own files -- fetched by the installer, never tracked -- are moved
# out of the vault here, so no dormant plugin code stays behind.
#   templater-obsidian   Templater, retired 2026-10-03 (ran commands from note
#                        text in reading view; QuickAdd user scripts replace it)
RETIRED_PLUGINS=(templater-obsidian)

# retire_plugins <vault> <backup_dir> <dry_run>
# Move each retired plugin's folder from <vault>/.obsidian/plugins into
# <backup_dir>/plugins. Prints one line per plugin removed.
retire_plugins() {
    local vault="$1" backup="$2" dry="$3" id dir
    for id in "${RETIRED_PLUGINS[@]}"; do
        dir="$vault/.obsidian/plugins/$id"
        [[ -d "$dir" ]] || continue
        if [[ "$dry" -eq 1 ]]; then
            info "  $id: retired upstream; dry run: would remove the plugin"
            continue
        fi
        mkdir -p "$backup/plugins"
        if mv "$dir" "$backup/plugins/$id"; then
            ok "  $id: retired upstream; removed (old copy in $backup/plugins)"
        else
            warn "  $id: retired upstream, but could not move $dir aside"
        fi
    done
}

# obsidian_running
# True while the Obsidian app is open. An update must not run then: Obsidian
# keeps the old plugins loaded (a retired one included) until it restarts, and
# writes its in-memory copies of tracked settings -- community-plugins.json,
# hotkeys.json, a plugin's data.json -- back over the pulled ones, leaving the
# tree dirty so the next update refuses. OBSIDIAN_PGREP is a test seam.
obsidian_running() {
    "${OBSIDIAN_PGREP:-pgrep}" -x Obsidian >/dev/null 2>&1
}

# plugins_drifted <vault>
# True when an enabled plugin's installed manifest is not the pinned one
# (installers/lib/plugin_drift.py), or the check itself cannot run -- then the
# reinstall reports why. Catches what pins_changed cannot: a re-run after a
# failed download, and an install that drifted from its pins some other way.
# Prints the reason.
plugins_drifted() {
    local vault="$1" ids
    if ! ids="$(python3 "$vault/installers/lib/plugin_drift.py" "$vault" 2>/dev/null)"; then
        echo "the plugin drift check failed"
        return 0
    fi
    [[ -n "$ids" ]] || return 1
    echo "not at their pins: $(echo "$ids" | paste -sd ' ' -)"
    return 0
}

# pins_changed <repo_root> <from_commit>
# True when the update moved installers/plugin-pins.json, i.e. the pinned
# plugin versions or hashes changed and the plugins need reinstalling.
pins_changed() {
    local repo="$1" from="$2"
    [[ -n "$from" ]] || return 1
    ! git -C "$repo" diff --quiet "$from" HEAD -- installers/plugin-pins.json
}
