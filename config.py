import os
import subprocess
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _macos_full_name() -> str:
    """Best-effort macOS full name (`id -F`), or login name, for greeting."""
    try:
        out = subprocess.check_output(["id", "-F"], text=True, timeout=1).strip()
        if out:
            return out
    except Exception:
        pass
    try:
        return os.getlogin() or ""
    except Exception:
        return ""

# ── Anthropic / Claude (primary brain) ───────────────────────────────────────
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL     = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")  # override in .env

# ── Groq (cloud fallback — tool-heavy tasks and complex reasoning) ────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# ── Ollama (local brain — runs on YOUR hardware, no internet needed) ──────────
# URL:   change to http://[mac-mini-ip]:11434 when you get the Mac mini
# MODEL: qwen2.5:3b for 8GB Mac, qwen2.5:7b or llama3.1:8b for 16GB+
OLLAMA_URL   = os.getenv("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")

# ── Spotify (optional — for playlist queuing) ─────────────────────────────────
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

# ── User ──────────────────────────────────────────────────────────────────────
# USER_NAME: env override → macOS full name (`id -F`) → "sir" as last resort.
# Avoids hardcoding any individual operator's name into the shipped product.
USER_NAME = os.getenv("USER_NAME") or _macos_full_name() or "sir"
USER_HOME = str(Path.home())

# Concise form used in spoken greetings — last name with honorific if we have
# a full name, otherwise the first token, otherwise "sir".
def _short_address(full_name: str) -> str:
    name = (full_name or "").strip()
    if not name or name.lower() == "sir":
        return "sir"
    parts = name.split()
    if len(parts) >= 2:
        return f"Mr. {parts[-1]}"
    return parts[0]


USER_ADDRESS = os.getenv("USER_ADDRESS") or _short_address(USER_NAME)

# ── Voice — TTS ───────────────────────────────────────────────────────────────
TTS_VOICE = os.getenv("TTS_VOICE", "Daniel")   # macOS say voice (British)
TTS_RATE  = int(os.getenv("TTS_RATE", "175"))  # words per minute

# ── Voice — STT ───────────────────────────────────────────────────────────────
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")   # base = 3-4× faster than small; accuracy fine with tuned VAD

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECTS_DIR   = os.path.expanduser(os.getenv("PROJECTS_DIR", "~/Projects"))
JARVIS_DIR     = str(Path(__file__).parent)
IRON_MAN_DIR   = os.path.expanduser("~/Iron Man Suit")
