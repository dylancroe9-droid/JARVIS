#!/bin/bash
# JARVIS Setup Script

set -e

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$JARVIS_DIR"

echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║   J.A.R.V.I.S  —  Setup             ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# ── Python version check ────────────────────────────────────────────────────
PYTHON=$(which python3.11 2>/dev/null || which python3.10 2>/dev/null || which python3.9 2>/dev/null || which python3 2>/dev/null)
if [ -z "$PYTHON" ]; then
  echo "  ERROR: Python 3.9+ required."
  echo "  Install via: brew install python@3.11"
  exit 1
fi
echo "  Using Python: $($PYTHON --version)"

# ── Virtual environment ──────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "  Creating virtual environment…"
  $PYTHON -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
echo "  Virtual environment: .venv"

# ── Homebrew system deps ─────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  echo "  Please install Homebrew first: https://brew.sh"
  exit 1
fi

if ! command -v ffmpeg &>/dev/null; then
  echo "  Installing ffmpeg…"
  brew install ffmpeg
fi
echo "  ffmpeg: $(ffmpeg -version 2>&1 | head -1 | cut -d' ' -f3)"

# portaudio is required by pyaudio (microphone input)
if ! brew list portaudio &>/dev/null 2>&1; then
  echo "  Installing portaudio (required for microphone)…"
  brew install portaudio
fi
echo "  portaudio: ok"

# ── Python packages ──────────────────────────────────────────────────────────
echo ""
echo "  Installing Python packages…"
echo "  (openai-whisper includes PyTorch — ~1 GB download on first run)"
echo ""
pip install -r requirements.txt

# ── Playwright browser ───────────────────────────────────────────────────────
echo ""
echo "  Installing Playwright Chromium browser…"
# Use the venv's playwright binary directly so this works even if the system
# PATH doesn't have playwright on it yet.
.venv/bin/playwright install chromium

# ── .env file ────────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "  ✏️  Created .env — open it and add one of:"
  echo "       ANTHROPIC_API_KEY=...   (preferred — console.anthropic.com)"
  echo "       GROQ_API_KEY=...        (free fallback — console.groq.com)"
fi

# ── macOS permissions reminder ───────────────────────────────────────────────
echo ""
echo "  ┌──────────────────────────────────────────────────────────────────┐"
echo "  │  macOS permissions — grant these once in System Settings:        │"
echo "  │                                                                   │"
echo "  │  • Accessibility  — for the ⌘⇧J global hotkey (pynput)          │"
echo "  │  • Microphone     — for voice input                              │"
echo "  │  • Calendar       — for schedule queries                          │"
echo "  │                                                                   │"
echo "  │  Path: System Settings > Privacy & Security > [each one]         │"
echo "  └──────────────────────────────────────────────────────────────────┘"
echo ""
echo "  ✅  Setup complete."
echo ""
echo "  ─── How to run ──────────────────────────────────────────────────"
echo ""
echo "  App mode (floating window + ⌘⇧J hotkey):"
echo "    ./run_app.sh"
echo ""
echo "  Terminal mode (keyboard only, good for testing):"
echo "    ./run.sh --text"
echo ""
echo "  Terminal mode (with voice):"
echo "    ./run.sh"
echo ""
