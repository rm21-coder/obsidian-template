#!/usr/bin/env bash
# plugins.sh - install Obsidian community plugins from the PINNED manifest.
#
# Every plugin is installed from installers/plugin-pins.json: an exact
# release tag (or commit, for release-less plugins), an exact download URL,
# and a SHA256 per file. Downloads that do not hash-match are discarded and
# the plugin fails closed — a compromised or merely surprising upstream
# release cannot reach the vault until a maintainer re-pins on purpose:
#
#   python3 installers/lib/pin_plugins.py      # refresh pins (network)
#   git diff installers/plugin-pins.json       # review what changed
#
# No registry lookup, no GitHub API, no "latest": installs are byte-for-byte
# reproducible from the pin file alone. The user still opens Obsidian once
# and clicks "Trust author and enable plugins" - we don't bypass that gate.

PLUGIN_PINS="$REPO_ROOT/installers/plugin-pins.json"

# _pin_field <plugin-id> <field>: print a top-level field of a pin.
_pin_field() {
    python3 -c '
import json, sys
pins = json.load(open(sys.argv[1]))
for p in pins:
    if p["id"] == sys.argv[2]:
        print(p.get(sys.argv[3], ""))
        break
' "$PLUGIN_PINS" "$1" "$2"
}

# _pin_files <plugin-id>: print "filename<TAB>url<TAB>sha256" per pinned file.
_pin_files() {
    python3 -c '
import json, sys
pins = json.load(open(sys.argv[1]))
for p in pins:
    if p["id"] == sys.argv[2]:
        for name, meta in p["files"].items():
            print(name + "\t" + meta["url"] + "\t" + meta["sha256"])
        break
' "$PLUGIN_PINS" "$1"
}

# fetch_plugin <plugin-id> <vault-dir>: install one pinned plugin, verifying
# every file's SHA256. Fails closed on any mismatch or missing pin.
fetch_plugin() {
    local id="$1" vault="$2"
    local ref
    ref="$(_pin_field "$id" "ref")"
    if [[ -z "$ref" ]]; then
        err "  plugin '$id' has no entry in $PLUGIN_PINS — re-pin with pin_plugins.py"
        return 1
    fi
    info "  $id @ $ref (pinned)"

    local plugin_dir="$vault/.obsidian/plugins/$id"
    local staging
    staging="$(mktemp -d -t obsidian_plugin)"

    local name url want got failed=0
    while IFS=$'\t' read -r name url want; do
        [[ -z "$name" ]] && continue
        if ! curl -fsSL "$url" -o "$staging/$name" 2>/dev/null; then
            err "    fetch failed: $url"
            failed=1
            break
        fi
        got="$(shasum -a 256 "$staging/$name" | awk '{print $1}')"
        if [[ "$got" != "$want" ]]; then
            err "    HASH MISMATCH for $id/$name — refusing to install."
            err "    expected $want"
            err "    got      $got"
            err "    Upstream changed under the pin. Re-run pin_plugins.py, review the diff, commit."
            failed=1
            break
        fi
    done < <(_pin_files "$id")

    if [[ "$failed" -eq 1 || -z "$(ls -A "$staging" 2>/dev/null)" ]]; then
        [[ "$failed" -eq 0 ]] && err "    no files staged for $id (pin parse problem?)"
        rm -rf "$staging"
        return 1
    fi

    # All files verified — move into place atomically-ish (per file).
    mkdir -p "$plugin_dir"
    local f
    for f in "$staging"/*; do
        install -m 0644 "$f" "$plugin_dir/$(basename "$f")"
    done
    rm -rf "$staging"
    ok "    installed (verified): $plugin_dir/"
}

# fetch_all_plugins <vault-dir>: install every plugin in the vault's
# community-plugins.json from its pin.
fetch_all_plugins() {
    local vault="$1"
    local manifest="$REPO_ROOT/.obsidian/community-plugins.json"
    if [[ ! -f "$manifest" ]]; then
        err "  $manifest not found"
        return 1
    fi
    if [[ ! -f "$PLUGIN_PINS" ]]; then
        err "  $PLUGIN_PINS not found — run: python3 installers/lib/pin_plugins.py"
        return 1
    fi

    local ids
    ids="$(python3 -c 'import json,sys; [print(p) for p in json.load(open(sys.argv[1]))]' "$manifest")"

    local total=0 ok_count=0 fail_count=0
    while IFS= read -r id; do
        [[ -z "$id" ]] && continue
        total=$((total + 1))
        if fetch_plugin "$id" "$vault"; then
            ok_count=$((ok_count + 1))
        else
            fail_count=$((fail_count + 1))
        fi
    done <<< "$ids"

    info "  plugins: $ok_count/$total installed, $fail_count failed"
    # Copy the community-plugins.json manifest into the vault so Obsidian
    # remembers which to enable on first launch. Skip the copy when the
    # repo IS the vault (REPO_ROOT == vault); the file is already in place.
    local dst="$vault/.obsidian/community-plugins.json"
    if [[ "$manifest" -ef "$dst" ]]; then
        ok "  community-plugins.json already in place (repo is vault)"
    else
        install -m 0644 "$manifest" "$dst"
        ok "  community-plugins.json copied to vault"
    fi

    # A hash mismatch anywhere is a hard stop for the component: unlike a
    # transient fetch error, it means the pin and upstream disagree, and
    # that disagreement should be looked at, not shrugged past.
    if [[ "$fail_count" -gt 0 ]]; then
        err "  $fail_count plugin(s) failed verification or fetch"
        return 1
    fi
}
