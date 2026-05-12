#!/bin/bash
# Launch JARVIS in floating overlay / hotkey app mode.
# Press ⌘⇧J from any app to show/hide the window.

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$JARVIS_DIR"

if [ ! -d ".venv" ]; then
  echo "Run ./setup.sh first to install dependencies."
  exit 1
fi

source .venv/bin/activate
export TK_SILENCE_DEPRECATION=1
exec python app.py "$@"
