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

# pins_changed <repo_root> <from_commit>
# True when the update moved installers/plugin-pins.json, i.e. the pinned
# plugin versions or hashes changed and the plugins need reinstalling.
pins_changed() {
    local repo="$1" from="$2"
    [[ -n "$from" ]] || return 1
    ! git -C "$repo" diff --quiet "$from" HEAD -- installers/plugin-pins.json
}
