#!/bin/sh
# ClearMic — install the desktop entry with this checkout's absolute path.
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
mkdir -p "$HOME/.local/share/applications"
sed "s|@BINDIR@|$DIR|g" "$DIR/data/clearmic.desktop.in" \
    > "$HOME/.local/share/applications/clearmic.desktop"
# remove the pre-rename entry if present
rm -f "$HOME/.local/share/applications/mic-dsp-ui.desktop"
echo "Installed: ~/.local/share/applications/clearmic.desktop"
