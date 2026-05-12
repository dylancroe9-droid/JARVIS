"""
User profile — stores first-run onboarding answers (name, city).
Completely separate from .env so each operator gets their own identity.

Rules:
  - is_setup_complete() returns True if .env already has an API key OR if the
    profile JSON has setup_complete=True.  This means Dylan's machine (which has
    a key in .env already) is never shown the wizard.
  - Friends start with an empty .env → wizard runs → key + name written out →
    profile flagged setup_complete.
  - "go back to setup" clears setup_complete so the wizard re-runs next time.
"""

from __future__ import annotations

import json
from pathlib import Path

_DIR          = Path(__file__).parent
_PROFILE_PATH = _DIR / "user_profile.json"
_ENV_PATH     = _DIR / ".env"


# ── helpers ───────────────────────────────────────────────────────────────────

def _env_has_key() -> bool:
    """Return True if .env already contains a non-empty API key."""
    if not _ENV_PATH.exists():
        return False
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() in ("ANTHROPIC_API_KEY", "GROQ_API_KEY") and v.strip():
            return True
    return False


# ── public API ────────────────────────────────────────────────────────────────

def load_profile() -> dict:
    """Return the saved profile dict, or {} if none exists."""
    if _PROFILE_PATH.exists():
        try:
            return json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_profile(data: dict) -> None:
    """Merge data into the profile and persist."""
    current = load_profile()
    current.update(data)
    _PROFILE_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")


def is_setup_complete() -> bool:
    """
    True if JARVIS can start without running the first-run wizard.

    - .env already has an API key → skip wizard (operator configured manually,
      e.g. Dylan's own machine).
    - OR user_profile.json exists with setup_complete = True → wizard already ran.
    """
    if _env_has_key():
        return True
    p = load_profile()
    return bool(p.get("setup_complete"))


def mark_setup_complete() -> None:
    save_profile({"setup_complete": True})


def reset_setup() -> None:
    """Allow the wizard to re-run.  Clears setup_complete but keeps name/city."""
    p = load_profile()
    p.pop("setup_complete", None)
    _PROFILE_PATH.write_text(json.dumps(p, indent=2), encoding="utf-8")


def get_user_name() -> str:
    """Return saved first name, or empty string."""
    return load_profile().get("name", "")


def get_user_city() -> str:
    """Return saved city, or empty string."""
    return load_profile().get("city", "")


def write_env_key(provider: str, api_key: str) -> None:
    """
    Write (or overwrite) an API key in .env.
    provider: "groq" | "anthropic"
    """
    env_key = "GROQ_API_KEY" if provider == "groq" else "ANTHROPIC_API_KEY"

    lines: list[str] = []
    if _ENV_PATH.exists():
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines()

    # Replace existing line or append
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{env_key}=") or stripped == env_key:
            lines[i] = f"{env_key}={api_key}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{env_key}={api_key}")

    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
