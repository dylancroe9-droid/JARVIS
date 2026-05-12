#!/bin/bash
# ── JARVIS — Full reset and launch ──────────────────────────────────────────
# Run this whenever something breaks. It clears all caches, reinstalls
# every package, verifies the .env, and starts the app.

set -e
cd "$(dirname "$0")"

echo ""
echo "  ◈  JARVIS — Fixing & Launching..."
echo ""

# ── 1. Activate venv ──────────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "  Creating virtual environment..."
  python3 -m venv .venv
fi
source .venv/bin/activate

# ── 2. Nuke all stale Python bytecode ─────────────────────────────────────────
echo "  Clearing Python cache..."
find . -path "./.venv" -prune -o -type d -name "__pycache__" -print | \
  grep -v ".venv" | xargs rm -rf 2>/dev/null || true
find . -path "./.venv" -prune -o -name "*.pyc" -print | \
  grep -v ".venv" | xargs rm -f 2>/dev/null || true

# ── 3. Install / upgrade all required packages ────────────────────────────────
echo "  Installing packages..."
pip install -q --upgrade \
  openai \
  python-dotenv \
  rich \
  customtkinter \
  playwright \
  requests \
  SpeechRecognition \
  numpy \
  scipy \
  sounddevice \
  edge-tts \
  pynput \
  pyautogui \
  pyobjc-framework-Cocoa

# ── 4. Check .env ─────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  echo ""
  echo "  ⚠️  No .env file found."
  echo "  Run:  echo 'GROQ_API_KEY=your_key' > ~/JARVIS/.env"
  echo ""
  exit 1
fi

if ! grep -q "GROQ_API_KEY=" .env || grep -q "GROQ_API_KEY=your" .env; then
  echo ""
  echo "  ⚠️  GROQ_API_KEY not set in .env"
  echo "  Get a free key at console.groq.com then run:"
  echo "  echo 'GROQ_API_KEY=gsk_...' > ~/JARVIS/.env"
  echo ""
  exit 1
fi

echo ""
echo "  ✅  All good. Starting JARVIS..."
echo ""

# ── 5. Launch ──────────────────────────────────────────────────────────────────
export TK_SILENCE_DEPRECATION=1
exec python app.py "$@"
