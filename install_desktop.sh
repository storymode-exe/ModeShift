#!/usr/bin/env bash
# Installs ModeShift's .desktop entries:
#   - ModeShift Watcher -> autostart on login + app menu
#   - ModeShift Editor  -> app menu
#
# Re-run any time after moving the project folder; the Exec paths below are
# rewritten to match wherever this script currently lives.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
AUTOSTART="$HOME/.config/autostart"
APPS="$HOME/.local/share/applications"

mkdir -p "$AUTOSTART" "$APPS"

# Rewrite the Exec= paths to this folder's actual location.
fix_paths() {
    sed "s|Exec=python3 .*/\([a-z_]*\.py\)|Exec=python3 $DIR/\1|" "$1"
}

fix_paths "$DIR/desktop/modeshift-watcher.desktop" > "$AUTOSTART/modeshift-watcher.desktop"
fix_paths "$DIR/desktop/modeshift-watcher.desktop" > "$APPS/modeshift-watcher.desktop"
fix_paths "$DIR/desktop/modeshift-editor.desktop"  > "$APPS/modeshift-editor.desktop"

update-desktop-database "$APPS" 2>/dev/null || true

echo "Installed:"
echo "  autostart : $AUTOSTART/modeshift-watcher.desktop"
echo "  app menu  : $APPS/modeshift-watcher.desktop"
echo "  app menu  : $APPS/modeshift-editor.desktop"
echo
echo "The watcher will now start automatically on next login."
echo "Both apps should appear in your application menu (search 'ModeShift')."
echo "To start the watcher right now without rebooting:"
echo "  python3 \"$DIR/modeshift_watcher.py\" &"
