#!/usr/bin/env bash
# plist.sh - render YOUR_USERNAME-templated plists and load them.
#
# Sourced after common.sh, so logging helpers are available.

# render_plist <src.plist> <dst.plist>
# Copies src to dst, substituting YOUR_USERNAME with $USER.
# Idempotent and safe to re-run.
render_plist() {
    local src="$1" dst="$2"
    if [[ ! -f "$src" ]]; then
        err "render_plist: source not found: $src"
        return 1
    fi
    if [[ -z "${USER:-}" ]]; then
        err "render_plist: \$USER is empty"
        return 1
    fi
    # sed -i '' for BSD/macOS sed; substitute on a copy so the source stays templated.
    local tmp
    tmp="$(mktemp -t obsidian_plist).plist"
    cp "$src" "$tmp"
    sed -i '' "s|YOUR_USERNAME|$USER|g" "$tmp"
    # Sanity: ensure no YOUR_USERNAME remains
    if grep -q YOUR_USERNAME "$tmp"; then
        err "render_plist: substitution failed; YOUR_USERNAME still present in $tmp"
        rm -f "$tmp"
        return 1
    fi
    install -m 0644 "$tmp" "$dst"
    rm -f "$tmp"
}

# install_plist_and_load <repo-relative-plist> <label>
# Renders the templated plist from $REPO_ROOT/<repo-relative-plist> into
# ~/Library/LaunchAgents/<label>.plist, then reload-launches it.
install_plist_and_load() {
    local rel="$1"
    local label="$2"
    local src="$REPO_ROOT/$rel"
    local dst="$HOME/Library/LaunchAgents/${label}.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    render_plist "$src" "$dst"
    ok "  rendered: $dst"
    launchctl_reload "$dst"
}
