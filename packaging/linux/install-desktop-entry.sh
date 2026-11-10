#!/bin/sh
# Install a "Sentinel" menu/desktop entry for the current user, pointing at this folder.
# Run from inside the built Sentinel folder:  ./install-desktop-entry.sh
# Needs no root. Writes only ~/.local/share/applications/sentinel.desktop (and a copy on
# ~/Desktop if that folder exists).
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$here/sentinel" ]; then
    echo "run this script from the Sentinel folder (sentinel executable not found in $here)" >&2
    exit 1
fi
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$apps"
sed "s|@APPDIR@|$here|g" "$here/sentinel.desktop" > "$apps/sentinel.desktop"
chmod 0644 "$apps/sentinel.desktop"
echo "installed $apps/sentinel.desktop"
if [ -d "$HOME/Desktop" ]; then
    cp "$apps/sentinel.desktop" "$HOME/Desktop/sentinel.desktop"
    chmod 0755 "$HOME/Desktop/sentinel.desktop"   # GNOME/KDE require it executable to launch
    echo "copied to $HOME/Desktop (you may need to right-click it and choose 'Allow Launching')"
fi
