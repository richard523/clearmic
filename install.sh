#!/bin/sh
# ClearMic — install the desktop entry and app icon with this
# checkout's absolute path.
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ICON=io.github.richard523.ClearMic

mkdir -p "$HOME/.local/share/applications"
sed "s|@BINDIR@|$DIR|g" "$DIR/data/clearmic.desktop.in" \
    > "$HOME/.local/share/applications/clearmic.desktop"

mkdir -p "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "$DIR/data/icons/$ICON.svg" \
   "$HOME/.local/share/icons/hicolor/scalable/apps/$ICON.svg"

gtk-update-icon-cache -q -t -f "$HOME/.local/share/icons/hicolor" \
    2>/dev/null || true

# remove the pre-rename entry if present
rm -f "$HOME/.local/share/applications/mic-dsp-ui.desktop"
echo "Installed: desktop entry + $ICON icon"
