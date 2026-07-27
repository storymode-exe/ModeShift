#!/usr/bin/env bash
# Installs the ModeShift icon into the hicolor theme so that Icon=modeshift in
# the .desktop files resolves in the app menu, task switcher and title bar.
# Safe to re-run.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
ICONS="$HOME/.local/share/icons/hicolor"

for size in 16 24 32 48 64 128 256 512; do
    src="$DIR/assets/modeshift-$size.png"
    [ -f "$src" ] || continue
    mkdir -p "$ICONS/${size}x${size}/apps"
    cp "$src" "$ICONS/${size}x${size}/apps/modeshift.png"
done

if [ -f "$DIR/assets/modeshift-logo.svg" ]; then
    mkdir -p "$ICONS/scalable/apps"
    cp "$DIR/assets/modeshift-logo.svg" "$ICONS/scalable/apps/modeshift.svg"
fi

gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true
echo "Installed the ModeShift icon into $ICONS"
echo "If the app menu still shows the old icon, log out and back in."
