#!/usr/bin/env bash
# 43-markitdown-dropper.sh - build the Markitdown Dropper .app bundle.
#
# The dropper is a PySide6 GUI that lives in its OWN venv (separate from
# the vault venv) because markitdown[all] and PySide6 are heavy and only
# this one app needs them. The .app bundle is an AppleScript wrapper that
# launches a bundled copy of markitdown_dropper.py via that venv's python.

set -euo pipefail

VAULT="$HOME/Obsidian"
SRC_PY="$VAULT/Templates/Scripts/markitdown_dropper.py"
DROPPER_VENV="$HOME/.markitdown-dropper-venv"
INSTALL_DIR="$HOME/Applications"
APP_PATH="$INSTALL_DIR/Markitdown Dropper.app"

if [[ ! -f "$SRC_PY" ]]; then
    err "  $SRC_PY missing"
    exit 1
fi

# ---- venv for markitdown + PySide6 -----------------------------------------
info "Setting up Markitdown Dropper venv at $DROPPER_VENV..."
if [[ -x "$DROPPER_VENV/bin/python3" ]]; then
    ok "  venv already exists"
else
    # The dropper expects python3.13 specifically per its build script.
    PY=""
    for c in /opt/homebrew/bin/python3.13 /usr/local/bin/python3.13 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        [[ -x "$c" ]] && PY="$c" && break
    done
    if [[ -z "$PY" ]]; then
        err "  no python3.13 found; brew install python@3.13 first"
        exit 1
    fi
    "$PY" -m venv "$DROPPER_VENV"
    ok "  created: $DROPPER_VENV"
fi
"$DROPPER_VENV/bin/pip" install --upgrade pip >/dev/null
"$DROPPER_VENV/bin/pip" install 'markitdown[all]' PySide6

# ---- compile the .app -------------------------------------------------------
mkdir -p "$INSTALL_DIR"
SCPT_TMP="$(mktemp -t markitdown_dropper).applescript"
cat > "$SCPT_TMP" <<APPLESCRIPT
-- Markitdown Dropper.app
property venvPython : "$DROPPER_VENV/bin/python3"

on run
    launchDropper()
end run

on open theFiles
    launchDropper()
end open

on launchDropper()
    try
        set appPosix to POSIX path of (path to me)
        set bundledPy to appPosix & "Contents/Resources/markitdown_dropper.py"
        set pyOk to do shell script "test -f " & quoted form of bundledPy & " && echo yes || echo no"
        if pyOk is not "yes" then
            display dialog "Bundled Python script missing." buttons {"OK"} default button 1 with icon stop
            return
        end if
        set isRunning to do shell script "pgrep -f " & quoted form of bundledPy & " >/dev/null 2>&1 && echo yes || echo no"
        if isRunning is "yes" then
            tell application "System Events"
                try
                    set frontmost of (first process whose name contains "Python") to true
                end try
            end tell
            return
        end if
        do shell script "nohup " & quoted form of venvPython & " " & quoted form of bundledPy & " >/dev/null 2>&1 &"
    on error errMsg
        display dialog "Failed to launch: " & errMsg buttons {"OK"} default button 1 with icon stop
    end try
end launchDropper
APPLESCRIPT

info "Compiling AppleScript -> $APP_PATH"
rm -rf "$APP_PATH"
osacompile -o "$APP_PATH" "$SCPT_TMP"
rm -f "$SCPT_TMP"

info "Bundling markitdown_dropper.py into the .app"
RES="$APP_PATH/Contents/Resources"
mkdir -p "$RES"
install -m 0755 "$SRC_PY" "$RES/markitdown_dropper.py"

touch "$APP_PATH"
ok "  built: $APP_PATH"
ok "markitdown-dropper component complete"
info "  Test: ⌘-Space, type 'Markitdown', press Return"
info "  If Gatekeeper warns on first launch: right-click -> Open"
