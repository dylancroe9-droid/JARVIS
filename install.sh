#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║   J.A.R.V.I.S  —  One-Command Installer                                   ║
# ║   Run:  bash install.sh                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
# Works on any macOS 13+ machine.  Handles everything from Homebrew to API keys.

set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'
GOLD='\033[0;33m'; DIM='\033[2m'; BOLD='\033[1m'; NC='\033[0m'

_ok()    { echo -e "  ${GREEN}✓${NC}  $*"; }
_info()  { echo -e "  ${CYAN}›${NC}  $*"; }
_warn()  { echo -e "  ${GOLD}⚠${NC}  $*"; }
_err()   { echo -e "  ${RED}✗${NC}  $*"; exit 1; }
_sep()   { echo -e "  ${DIM}──────────────────────────────────────────────────────${NC}"; }
_ask()   { printf "  ${BOLD}?${NC}  $*: "; }
_blank() { echo ""; }

_header() {
  clear
  echo ""
  echo -e "  ${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
  echo -e "  ${CYAN}║${NC}  ${BOLD}J . A . R . V . I . S${NC}  ${DIM}—${NC}  Personal AI  ${DIM}//  v2.0${NC}            ${CYAN}║${NC}"
  echo -e "  ${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
  echo ""
}

_header

echo -e "  ${DIM}Welcome. This installer will set up JARVIS on your Mac in about 5–10 minutes.${NC}"
echo -e "  ${DIM}You'll need an Anthropic or Groq API key (both are free to sign up for).${NC}"
_blank

# ── 0. macOS check ─────────────────────────────────────────────────────────────
if [[ "$(uname -s)" != "Darwin" ]]; then
  _err "JARVIS currently requires macOS.  Linux support coming soon."
fi
MACOS_VER=$(sw_vers -productVersion)
_ok "macOS $MACOS_VER detected"

# ── 1. Determine install directory ────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JARVIS_DIR="$SCRIPT_DIR"

# If running from a temp download (not a git repo), clone into ~/JARVIS
if [ ! -f "$JARVIS_DIR/server.py" ] && [ ! -f "$JARVIS_DIR/brain/jarvis.py" ]; then
  _info "JARVIS source not found here — cloning to ~/JARVIS…"
  JARVIS_DIR="$HOME/JARVIS"
  if [ ! -d "$JARVIS_DIR" ]; then
    git clone https://github.com/dylancroe9-droid/JARVIS.git "$JARVIS_DIR" 2>/dev/null \
      || _err "Clone failed. Download JARVIS manually from github.com/dylancroe9-droid/JARVIS"
  fi
fi

_ok "JARVIS directory: $JARVIS_DIR"
cd "$JARVIS_DIR"

# ── 2. Homebrew ───────────────────────────────────────────────────────────────
_blank; _sep; echo -e "  ${BOLD}Step 1${NC}  ${DIM}System tools${NC}"; _sep; _blank

if ! command -v brew &>/dev/null; then
  _warn "Homebrew not found — installing (requires your password)…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Add brew to PATH for Apple Silicon
  if [[ -f /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
  _ok "Homebrew installed"
else
  _ok "Homebrew: $(brew --version | head -1)"
fi

# ── 3. Python ─────────────────────────────────────────────────────────────────
PYTHON=$(command -v python3.12 2>/dev/null \
      || command -v python3.11 2>/dev/null \
      || command -v python3.10 2>/dev/null \
      || command -v python3 2>/dev/null || echo "")

if [ -z "$PYTHON" ]; then
  _warn "Python 3.10+ not found — installing via Homebrew…"
  brew install python@3.11
  PYTHON="$(brew --prefix)/bin/python3.11"
fi

PYVER=$("$PYTHON" --version 2>&1 | cut -d' ' -f2)
PYVERMAJ=$(echo "$PYVER" | cut -d. -f1)
PYVERMID=$(echo "$PYVER" | cut -d. -f2)
if [[ "$PYVERMAJ" -lt 3 ]] || [[ "$PYVERMAJ" -eq 3 && "$PYVERMID" -lt 9 ]]; then
  _warn "Python $PYVER is too old (need 3.9+) — installing 3.11…"
  brew install python@3.11
  PYTHON="$(brew --prefix)/bin/python3.11"
fi
_ok "Python: $("$PYTHON" --version)"

# ── 4. ffmpeg ─────────────────────────────────────────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
  _info "Installing ffmpeg (required for audio processing)…"
  brew install ffmpeg
fi
_ok "ffmpeg: $(ffmpeg -version 2>&1 | head -1 | cut -d' ' -f3)"

# ── 5. Node.js (for Electron UI) ──────────────────────────────────────────────
if ! command -v node &>/dev/null; then
  _info "Installing Node.js (for JARVIS desktop UI)…"
  brew install node
fi
_ok "Node.js: $(node --version)"

# ── 6. Python virtual environment ────────────────────────────────────────────
_blank; _sep; echo -e "  ${BOLD}Step 2${NC}  ${DIM}Python packages${NC}"; _sep; _blank

if [ ! -d "$JARVIS_DIR/.venv" ]; then
  _info "Creating Python virtual environment…"
  "$PYTHON" -m venv "$JARVIS_DIR/.venv"
fi
source "$JARVIS_DIR/.venv/bin/activate"
pip install --upgrade pip setuptools wheel -q
_ok "Virtual environment ready"

_info "Installing Python packages (this may take 2–3 minutes)…"
pip install -r "$JARVIS_DIR/requirements.txt" -q
_ok "Python packages installed"

# ── 7. Playwright browser ─────────────────────────────────────────────────────
_info "Installing Playwright browser (Chromium, ~200 MB)…"
playwright install chromium 2>/dev/null | tail -3 || true
_ok "Playwright Chromium ready"

# ── 8. Electron / npm dependencies ───────────────────────────────────────────
if [ -d "$JARVIS_DIR/jarvis-app" ]; then
  _blank; _sep; echo -e "  ${BOLD}Step 3${NC}  ${DIM}Desktop UI${NC}"; _sep; _blank
  _info "Installing Electron app dependencies…"
  cd "$JARVIS_DIR/jarvis-app"
  npm install --silent 2>/dev/null || npm install
  cd "$JARVIS_DIR"
  _ok "Electron UI ready"
fi

# ── 9. Personalization ────────────────────────────────────────────────────────
_blank; _sep
echo -e "  ${BOLD}Step 4${NC}  ${DIM}Personalization${NC}"; _sep; _blank

echo -e "  ${DIM}JARVIS will greet you by name and adapt its responses to you.${NC}"
_blank

_ask "Your first name"
read -r USER_FIRST_NAME
USER_FIRST_NAME="${USER_FIRST_NAME:-User}"

_ask "How should JARVIS address you? (e.g. 'sir', 'Mr. Smith', 'Dylan') [${USER_FIRST_NAME}]"
read -r USER_ADDRESS_INPUT
USER_ADDRESS="${USER_ADDRESS_INPUT:-$USER_FIRST_NAME}"

_blank

echo -e "  ${DIM}JARVIS can use different voices for text-to-speech.${NC}"
echo -e "  ${DIM}  1. ElevenLabs AI voice (most realistic — needs free API key)${NC}"
echo -e "  ${DIM}  2. Microsoft Neural TTS (free, no key needed — very good)${NC}"
echo -e "  ${DIM}  3. macOS Daniel voice (offline, always works)${NC}"
_blank
_ask "Choose voice [2]"
read -r VOICE_CHOICE
VOICE_CHOICE="${VOICE_CHOICE:-2}"

# ── 10. API keys ──────────────────────────────────────────────────────────────
_blank; _sep
echo -e "  ${BOLD}Step 5${NC}  ${DIM}API Keys${NC}"; _sep; _blank

echo -e "  ${DIM}JARVIS needs at least one AI API key to think and respond.${NC}"
echo -e "  ${DIM}Get a free Anthropic key at: ${CYAN}console.anthropic.com${NC}"
echo -e "  ${DIM}Get a free Groq key at:      ${CYAN}console.groq.com${NC}"
_blank

_ask "Anthropic API key (press Enter to skip)"
read -r -s ANTHROPIC_KEY
echo ""
if [ -n "$ANTHROPIC_KEY" ]; then
  _ok "Anthropic key set"
fi

_ask "Groq API key (press Enter to skip — required if no Anthropic key)"
read -r -s GROQ_KEY
echo ""
if [ -n "$GROQ_KEY" ]; then
  _ok "Groq key set"
fi

ELEVEN_KEY=""
if [ "$VOICE_CHOICE" = "1" ]; then
  _blank
  _ask "ElevenLabs API key (free at elevenlabs.io)"
  read -r -s ELEVEN_KEY
  echo ""
  if [ -n "$ELEVEN_KEY" ]; then _ok "ElevenLabs key set"; fi
fi

if [ -z "$ANTHROPIC_KEY" ] && [ -z "$GROQ_KEY" ]; then
  _warn "No API key provided. JARVIS will boot in demo mode."
  _warn "You can add a key later by editing $JARVIS_DIR/.env"
fi

# ── 11. Write .env ─────────────────────────────────────────────────────────────
ENV_FILE="$JARVIS_DIR/.env"
cat > "$ENV_FILE" << EOF
# ── JARVIS Configuration ──────────────────────────────────────────────────────
# Generated by install.sh — edit as needed

# ── Your identity ──────────────────────────────────────────────────────────────
USER_NAME=$USER_FIRST_NAME
USER_ADDRESS=$USER_ADDRESS

# ── AI Brain (at least one required) ─────────────────────────────────────────
ANTHROPIC_API_KEY=$ANTHROPIC_KEY
GROQ_API_KEY=$GROQ_KEY

# ── Voice TTS ─────────────────────────────────────────────────────────────────
ELEVENLABS_API_KEY=$ELEVEN_KEY
# ELEVEN_VOICE=onwK4e9ZLuTAKqWW03F9   # Voice ID — change at elevenlabs.io
# TTS_VOICE=Daniel                      # macOS fallback voice

# ── Optional ──────────────────────────────────────────────────────────────────
# ANTHROPIC_MODEL=claude-opus-4-5
# GROQ_MODEL=llama-3.3-70b-versatile
# WHISPER_MODEL=base                    # base | small | medium (larger = more accurate)
EOF
_ok ".env written to $ENV_FILE"

# ── 12. macOS permissions reminder ───────────────────────────────────────────
_blank; _sep
echo -e "  ${BOLD}Step 6${NC}  ${DIM}macOS Permissions${NC}"; _sep; _blank

echo -e "  Grant these permissions once in ${BOLD}System Settings › Privacy & Security${NC}:"
_blank
echo -e "  ${CYAN}›${NC}  ${BOLD}Microphone${NC}        — voice input  (Settings › Microphone)"
echo -e "  ${CYAN}›${NC}  ${BOLD}Accessibility${NC}     — keyboard shortcuts  (Settings › Accessibility)"
echo -e "  ${CYAN}›${NC}  ${BOLD}Calendar${NC}           — schedule queries  (Settings › Calendar)"
echo -e "  ${CYAN}›${NC}  ${BOLD}Screen Recording${NC}   — vision mode  (Settings › Screen Recording)"
_blank
echo -e "  ${DIM}macOS will prompt you automatically the first time JARVIS needs each one.${NC}"

# ── 13. Launcher shortcut ─────────────────────────────────────────────────────
_blank; _sep
_ask "Create Desktop launcher? (y/N)"
read -r CREATE_LAUNCHER
if [[ "$CREATE_LAUNCHER" =~ ^[Yy]$ ]]; then
  LAUNCHER="$HOME/Desktop/JARVIS.command"
  cat > "$LAUNCHER" << LAUNCHER_EOF
#!/bin/bash
# JARVIS Desktop Launcher
cd "$JARVIS_DIR"
source .venv/bin/activate
exec ./run_app.sh
LAUNCHER_EOF
  chmod +x "$LAUNCHER"
  _ok "Desktop launcher created: $LAUNCHER"
fi

# ── 14. Shell alias ────────────────────────────────────────────────────────────
_blank
_ask "Add 'jarvis' terminal command? (y/N)"
read -r CREATE_ALIAS
if [[ "$CREATE_ALIAS" =~ ^[Yy]$ ]]; then
  ALIAS_LINE="alias jarvis='cd $JARVIS_DIR && source .venv/bin/activate && python main.py'"
  # Detect shell
  SHELL_RC="$HOME/.zshrc"
  [[ -f "$HOME/.bashrc" ]] && SHELL_RC="$HOME/.bashrc"

  if ! grep -q "alias jarvis=" "$SHELL_RC" 2>/dev/null; then
    echo "" >> "$SHELL_RC"
    echo "# JARVIS AI" >> "$SHELL_RC"
    echo "$ALIAS_LINE" >> "$SHELL_RC"
    _ok "Added 'jarvis' alias to $SHELL_RC"
    _info "Restart your terminal or run: source $SHELL_RC"
  else
    _ok "'jarvis' alias already in $SHELL_RC"
  fi
fi

# ── 15. Done ──────────────────────────────────────────────────────────────────
_blank; _sep
echo -e "  ${GREEN}${BOLD}✓  JARVIS is ready, ${USER_ADDRESS}.${NC}"
_sep; _blank

echo -e "  ${BOLD}Launch options:${NC}"
_blank
echo -e "  ${CYAN}App mode (window + voice):${NC}"
echo -e "    cd $JARVIS_DIR && ./run_app.sh"
_blank
echo -e "  ${CYAN}Terminal mode:${NC}"
echo -e "    cd $JARVIS_DIR && ./run.sh"
_blank
echo -e "  ${CYAN}Text-only mode (no mic):${NC}"
echo -e "    cd $JARVIS_DIR && ./run.sh --text"
_blank
echo -e "  ${DIM}Edit $JARVIS_DIR/.env to change API keys, voice, or name at any time.${NC}"
_blank
