#!/bin/bash
# Quick launcher — activates venv and starts JARVIS.
# Pass --text for keyboard-only mode.

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$JARVIS_DIR"

if [ ! -d ".venv" ]; then
  echo "Virtual environment not found. Run ./setup.sh first."
  exit 1
fi

source .venv/bin/activate
exec python main.py "$@"
