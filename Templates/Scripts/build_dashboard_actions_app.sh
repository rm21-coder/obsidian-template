#!/bin/bash
#
# build_dashboard_actions_app.sh
#
# Builds ~/Applications/DashboardActions.app: a tiny AppleScript applet that
# registers the obsidian-dashboard:// URL scheme and dispatches to
# dashboard_actions.sh. This is what makes the Morning Dashboard's buttons
# actually do something — a static file:// HTML page can't spawn local
# processes from a click, so its buttons link to
# obsidian-dashboard://run/<action> instead, and macOS routes that here.
#
# Technique: osacompile + ad-hoc codesign + Launch Services registration.
# No TCC-gated permissions involved — the app only ever touches plain
# home-directory folders.
#
# Run once (installer component 57-dashboard-actions does this for you),
# and re-run any time DashboardActions.applescript changes:
#   bash ~/Obsidian/Templates/Scripts/build_dashboard_actions_app.sh
#
set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPTS_DIR/DashboardActions.applescript"
APP_DIR="$HOME/Applications"
APP="$APP_DIR/DashboardActions.app"
BUNDLE_ID="org.obsidian-template.dashboard-actions"

mkdir -p "$APP_DIR"

echo '1. Building the wrapper app (AppleScript applet) ...'
rm -rf "$APP"
/usr/bin/osacompile -o "$APP" "$SRC"

echo '2. Giving it a unique identity, ad-hoc signing, and a URL scheme ...'
INFO="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string $BUNDLE_ID" "$INFO" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $BUNDLE_ID" "$INFO"
/usr/libexec/PlistBuddy -c 'Delete :CFBundleURLTypes' "$INFO" 2>/dev/null || true
/usr/libexec/PlistBuddy -c 'Add :CFBundleURLTypes array' "$INFO"
/usr/libexec/PlistBuddy -c 'Add :CFBundleURLTypes:0 dict' "$INFO"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLName string $BUNDLE_ID" "$INFO"
/usr/libexec/PlistBuddy -c 'Add :CFBundleURLTypes:0:CFBundleURLSchemes array' "$INFO"
/usr/libexec/PlistBuddy -c 'Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string obsidian-dashboard' "$INFO"
/usr/bin/codesign --force --sign - "$APP"

echo '3. Sanity check: app bundle is valid ...'
/usr/bin/codesign --verify --verbose=2 "$APP" && echo '   signature OK'

echo '4. Registering the URL scheme with Launch Services ...'
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"

echo
echo 'DONE. Test each action from Terminal:'
echo "  open 'obsidian-dashboard://run/refresh-dashboard'"
echo "  open 'obsidian-dashboard://run/refresh-rag'"
echo "  open 'obsidian-dashboard://run/rebaseline-security'"
echo "  open 'obsidian-dashboard://run/pull-meetings'"
echo
echo "Watch results in:  tail -f ~/Library/Logs/dashboard-actions.log"
