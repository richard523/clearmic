#!/bin/sh
# Mic DSP launcher
cd "$(dirname "$0")"
exec python3 main.py "$@"
