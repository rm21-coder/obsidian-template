# shellcheck shell=bash
# tree_gate.sh - refuse to run installer code that the git checkout does not vouch for.
#
# Findings 5278120 / 5278133 (prevention half). In the documented install the
# vault IS the git checkout, and the vault is synced by Obsidian Sync. A paired
# device that is compromised can therefore write into installers/ or
# Templates/Scripts/, and the next ./install.sh or ./update.sh would source or
# run what it wrote. The integrity monitor's installers scope detects that
# after the fact; this refuses it before anything runs.
#
# What it checks, when the tree is a git checkout:
#   - `git status --porcelain --untracked-files=all` over installers/,
#     Templates/Scripts/, the three root entry scripts, .gitignore and
#     .gitattributes: any modified, deleted, added or untracked path refuses.
#     Gitignored paths are not in that output (the .venv, logs, user profiles).
#   - ignored files under installers/ beyond a short allow-list, and anywhere
#     in the gated paths a nested .gitignore or an ignored file of a type that
#     runs (.sh .py .plist .ps1 ...): a .gitignore can ignore itself, and so
#     hide everything else from the first check.
#   - in update mode, also any tracked change anywhere (what update.sh and
#     update.ps1 used to check on their own, and the reason a pull is refused).
# A tree that is not a git checkout (a release zip) cannot be verified: it gets
# a warning and the install continues, unless --strict-tree.
#
# THE LIMIT. This file is sourced before common.sh, so it is self-contained:
# no helper from installers/ is used here. The entry script checks this file's
# blob against HEAD before sourcing it. Nothing can check the entry script
# itself: an edited install.sh, update.sh or uninstall.sh runs whatever it was
# edited to run, gate or no gate. What keeps a paired device from editing it is
# the Obsidian Sync setting printed at the end of an install
# (tree_gate_sync_notice). Git's own configuration (.git/) is trusted too; it
# is not synced (Obsidian Sync skips dot-folders other than .obsidian).
#
# Definitions only; sourcing this runs nothing. Bash 3.2 compatible.

# Paths whose content the installers run, source or install.
TREE_GATE_PATHS=(installers Templates/Scripts install.sh update.sh uninstall.sh .gitignore .gitattributes)

_tg_say()  { printf '%s\n' "$*"; }
_tg_err()  { printf '%s\n' "$*" >&2; }

# git with nothing from the local config that runs a program during status.
_tg_git() {
    git -C "$TREE_GATE_ROOT" -c core.fsmonitor=false -c core.untrackedCache=false \
        -c core.quotepath=off "$@"
}

# tree_gate_is_checkout <root>: true when <root> is the top of a git work tree
# and git runs. A tree unpacked inside some other repository is not one.
tree_gate_is_checkout() {
    local root="$1" top phys
    [[ -e "$root/.git" ]] || return 1
    command -v git >/dev/null 2>&1 || return 1
    top="$(git -C "$root" rev-parse --show-toplevel 2>/dev/null)" || return 1
    phys="$(cd "$root" && pwd -P)" || return 1
    top="$(cd "$top" && pwd -P)" || return 1
    [[ "$top" == "$phys" ]]
}

# An ignored path that may sit in a gated directory without refusing.
_tg_ignored_ok() {
    local p="$1"
    case "$p" in
        .gitignore|*/.gitignore)                   return 1 ;;
        */.DS_Store)                               return 0 ;;
        installers/profiles/*/*)                   return 1 ;;
        installers/profiles/*.env)                 return 0 ;;   # user profiles, by design
        installers/lib/__pycache__/)               return 0 ;;
        installers/lib/identity-denylist.local)    return 0 ;;
        installers/lib/identity-allowed-identities.local) return 0 ;;
        installers/*)                              return 1 ;;
        *.sh|*.py|*.pyc|*.plist|*.ps1|*.psm1|*.psd1|*.js|*.applescript|*.cmd|*.bat|*.exe|*.command)
                                                   return 1 ;;
    esac
    return 0
}

# tree_gate_log <line>: append to the override log, owner-only. Best effort.
tree_gate_log() {
    local dir="$HOME/Library/Logs"
    ( umask 077; mkdir -p "$dir" 2>/dev/null \
        && printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >> "$dir/obsidian-template-tree-gate.log" ) \
        2>/dev/null || true
}

# tree_gate_check <root> <mode> <allow_local_changes 0|1> <strict 0|1>
#   mode: install | update | uninstall
# Returns 0 to proceed, 1 to refuse (the reason is already printed).
tree_gate_check() {
    local root="$1" mode="$2" allow="${3:-0}" strict="${4:-0}"
    local flag="--allow-local-changes" head changes other ignored p bad=""
    TREE_GATE_ROOT="$root"
    # N5: an ignored __pycache__/ is one entry to git, so the gate cannot see
    # a planted unchecked-hash .pyc inside it, and installer helpers run
    # without -I import their siblings. From here on no Python this run starts
    # reads or writes bytecode in the tree (-I ignores these, but imports
    # nothing from the tree either).
    export PYTHONDONTWRITEBYTECODE=1
    export PYTHONPYCACHEPREFIX="$HOME/Library/Caches/obsidian-template/pycache"

    if ! tree_gate_is_checkout "$root"; then
        if [[ "$strict" -eq 1 ]]; then
            _tg_err "TREE GATE: REFUSED. $root is not a git checkout (or git is not available),"
            _tg_err "so its installer code cannot be checked. This run requires a checkout (--strict-tree, or an update, which pulls)."
            return 1
        fi
        _tg_say "WARNING: $root is not a git checkout (or git is not available), so this"
        _tg_say "WARNING: installer cannot check that its code is the code that was published."
        _tg_say "WARNING: Continuing. Install from a git clone to get that check, or pass"
        _tg_say "WARNING: --strict-tree to refuse instead."
        return 0
    fi

    head="$(_tg_git rev-parse --short HEAD 2>/dev/null)" || {
        _tg_err "TREE GATE: REFUSED. git cannot read HEAD in $root."
        return 1
    }
    changes="$(_tg_git status --porcelain --untracked-files=all -- "${TREE_GATE_PATHS[@]}")" || {
        _tg_err "TREE GATE: REFUSED. git status failed in $root."
        return 1
    }
    ignored="$(_tg_git ls-files --others --ignored --exclude-standard --directory -- "${TREE_GATE_PATHS[@]}")" || {
        _tg_err "TREE GATE: REFUSED. git ls-files failed in $root."
        return 1
    }
    while IFS= read -r p; do
        [[ -z "$p" ]] && continue
        _tg_ignored_ok "$p" || bad="${bad}!! ${p}"$'\n'
    done <<< "$ignored"
    [[ -n "$bad" ]] && changes="${changes:+$changes$'\n'}${bad%$'\n'}"

    other=""
    if [[ "$mode" == update ]]; then
        # Tracked changes outside the installer code: notes are untracked and
        # fine, but a pull over a changed tracked file is refused, as before.
        other="$(_tg_git status --porcelain --untracked-files=no)" || {
            _tg_err "TREE GATE: REFUSED. git status failed in $root."
            return 1
        }
        if [[ -n "$changes" && -n "$other" ]]; then
            # Drop what the first list already names.
            other="$(printf '%s\n' "$other" | grep -vxF -f <(printf '%s\n' "$changes") || true)"
        fi
        # N9: outside the installer code, a file whose only change is its line
        # endings (Windows editors, autocrlf) is not a reason to refuse an
        # update. Installer paths never get here: they are in $changes.
        local kept="" eol_only="" line path
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            path="${line:3}"
            if [[ "${line:0:3}" == " M " ]] \
                    && _tg_git diff --ignore-cr-at-eol --quiet -- "$path" 2>/dev/null; then
                eol_only="${eol_only}${path}"$'\n'
            else
                kept="${kept}${line}"$'\n'
            fi
        done <<< "$other"
        other="${kept%$'\n'}"
        if [[ -n "$eol_only" ]]; then
            _tg_say "tree gate: line endings only, not refused (reset with git -C \"$root\" checkout -- <path>):"
            printf '%s' "$eol_only" | sed 's/^/  /'
        fi
    fi

    if [[ -z "$changes" && -z "$other" ]]; then
        _tg_say "tree gate: installer code matches the git checkout (HEAD $head)."
        return 0
    fi

    if [[ "$allow" -eq 1 ]]; then
        _tg_say "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        _tg_say "TREE GATE OVERRIDE: $flag given. Running $mode with local changes"
        _tg_say "that git does not vouch for (HEAD $head):"
        [[ -n "$changes" ]] && printf '%s\n' "$changes" | sed 's/^/  /'
        [[ -n "$other" ]] && printf '%s\n' "$other" | sed 's/^/  /'
        _tg_say "Recorded in ~/Library/Logs/obsidian-template-tree-gate.log."
        _tg_say "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        tree_gate_log "OVERRIDE $mode root=$root head=$head $(printf '%s' "$changes $other" | tr '\n' ';')"
        return 0
    fi

    if [[ -n "$changes" ]]; then
        _tg_err "TREE GATE: REFUSED. The installer code in this tree differs from the git checkout."
        _tg_err "These paths under installers/, Templates/Scripts/ or the root scripts are modified,"
        _tg_err "untracked, or hidden from git (HEAD $head):"
        printf '%s\n' "$changes" | sed 's/^/  /' >&2
        _tg_err ""
        _tg_err "This vault is synced, so another device could have written them. Nothing has run."
        _tg_err "Look at what changed before you run anything:"
        _tg_err "  git -C \"$root\" status --untracked-files=all"
        _tg_err "  git -C \"$root\" diff"
        _tg_err "To go back to the published code: git -C \"$root\" checkout -- <path> for a"
        _tg_err "changed file, and delete an untracked one."
        _tg_err "If these are your own edits (a maintainer working on the installer), re-run with"
        _tg_err "$flag. That is logged."
    fi
    if [[ -n "$other" ]]; then
        _tg_err "Local changes to tracked files; not updating:"
        printf '%s\n' "$other" | sed 's/^/  /' >&2
        if [[ -z "$changes" && "$other" =~ ^\ ?M\ \.obsidian/types\.json$ ]]; then
            _tg_err ""
            _tg_err "Obsidian rewrites .obsidian/types.json when it sees new note properties."
            _tg_err "It only records property types, so taking the upstream copy is safe:"
            _tg_err "  git -C \"$root\" checkout -- .obsidian/types.json"
            _tg_err "Then run this update again."
        fi
    fi
    return 1
}

# tree_gate_profile <root> <file>: a profile is sourced as shell code. Allowed
# in installers/profiles/ (gitignored by design, so git cannot vouch for it)
# or outside the vault (where Obsidian Sync cannot reach it); refused anywhere
# else inside the vault, where it is both synced and invisible to the gate.
# Prints which file is sourced and its SHA-256 either way. Returns 1 to refuse.
tree_gate_profile() {
    local root="$1" file="$2" dir phys rootp sum
    if [[ -L "$file" ]]; then
        _tg_err "TREE GATE: REFUSED. The profile $file is a symlink; pass the file itself."
        return 1
    fi
    dir="$(cd "$(dirname "$file")" && pwd -P)" || return 1
    phys="$dir/$(basename "$file")"
    rootp="$(cd "$root" && pwd -P)" || return 1
    case "$phys" in
        "$rootp"/*)
            if [[ "$dir" != "$rootp/installers/profiles" ]]; then
                _tg_err "TREE GATE: REFUSED. The profile $phys is inside the vault but not in"
                _tg_err "installers/profiles/. Anything in the vault is synced, and a profile is run"
                _tg_err "as code. Move it to installers/profiles/, or keep it outside the vault"
                _tg_err "(for example ~/Downloads), and pass that path."
                return 1
            fi
            ;;
    esac
    if command -v shasum >/dev/null 2>&1; then
        sum="$(shasum -a 256 "$phys" | awk '{print $1}')"
    elif command -v sha256sum >/dev/null 2>&1; then
        sum="$(sha256sum "$phys" | awk '{print $1}')"
    else
        sum="$(openssl dgst -sha256 "$phys" 2>/dev/null | awk '{print $NF}')"
    fi
    _tg_say "profile file:   $phys"
    _tg_say "profile sha256: ${sum:-unavailable}"
    _tg_say "  A profile is run as code. Compare that hash with the one its sender gave you."
    return 0
}

# Printed at the end of a full install. ASCII only.
tree_gate_sync_notice() {
    cat <<'NOTICE'

REQUIRED STEP (findings 5278120, 5278133): keep other devices from changing the code.
This vault is the installer's git checkout. If it syncs with Obsidian Sync, then
in Obsidian on EVERY device that syncs this vault:
  Settings > Sync > turn OFF "Sync all other types" (at least, do not sync code
  file types)
and on THIS machine, the one that runs the jobs:
  Settings > Sync > Excluded folders > add Templates/Scripts and installers
Otherwise a paired device can change the scripts these jobs run and the
installer code the next update runs.
NOTICE
}
