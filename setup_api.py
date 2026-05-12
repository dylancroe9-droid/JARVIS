"""
First-run setup wizard backend.

Exposes HTTP endpoints the Electron renderer calls to:
  - check whether ~/JARVIS/.env is configured (`GET /setup/status`)
  - validate a candidate Anthropic / Groq key live (`POST /setup/validate-key`)
  - persist the user's choices into ~/JARVIS/.env (`POST /setup/save`)
  - open System Settings panes for permissions (`POST /setup/open-pref`)

Designed to run in two modes:
  - SETUP_MODE (no key in .env): server.py boots with a stub brain; only these
    endpoints work. The renderer shows the wizard and asks for relaunch on save.
  - normal mode: endpoints stay available so the Settings panel can re-validate
    or rotate keys.

Note on persistence: the wizard updates .env in place, preserving comments and
unrelated keys. A relaunch is required for the change to take effect because
config.py reads env vars at import time.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter
from pydantic import BaseModel


JARVIS_DIR = Path(__file__).parent.resolve()
ENV_PATH   = JARVIS_DIR / ".env"

# ── helpers ───────────────────────────────────────────────────────────────────


def _read_env() -> dict[str, str]:
    """Return a flat dict of the keys currently in .env (no .env? empty dict)."""
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _write_env(updates: dict[str, str]) -> None:
    """
    Merge `updates` into .env. Preserves the file's existing comments and any
    keys we don't touch. Creates the file from scratch if it doesn't exist.

    Empty-string values delete the key entirely (so the user can clear keys).
    """
    existing_lines: list[str] = []
    if ENV_PATH.exists():
        existing_lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    seen: set[str] = set()
    new_lines: list[str] = []
    for raw in existing_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(raw)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            seen.add(key)
            val = updates[key]
            if val == "":
                # Comment the line out rather than dropping it — makes it
                # easier to see what was cleared.
                new_lines.append(f"# {key}=")
            else:
                new_lines.append(f"{key}={val}")
        else:
            new_lines.append(raw)

    # Append any new keys that weren't already present.
    appended_header = False
    for key, val in updates.items():
        if key in seen or not val:
            continue
        if not appended_header:
            if new_lines and new_lines[-1].strip():
                new_lines.append("")
            new_lines.append("# Added by setup wizard")
            appended_header = True
        new_lines.append(f"{key}={val}")

    ENV_PATH.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


# ── live key validation ───────────────────────────────────────────────────────


def _validate_anthropic(key: str) -> tuple[bool, str]:
    """Tiny ping to Anthropic. Returns (ok, message)."""
    if not key.startswith("sk-ant-"):
        return False, "Anthropic keys start with sk-ant-…"
    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=8.0,
        )
    except httpx.HTTPError as exc:
        return False, f"Network error: {exc}"
    if r.status_code == 200:
        return True, "Anthropic key works."
    if r.status_code in (401, 403):
        return False, "Anthropic rejected the key (invalid or revoked)."
    # 400 with an "input" error means the key is fine but our test ping was
    # rejected on content — still proves auth.
    if r.status_code == 400 and b"authentication" not in r.content.lower():
        return True, "Anthropic key works."
    return False, f"Anthropic returned {r.status_code}: {r.text[:160]}"


def _validate_groq(key: str) -> tuple[bool, str]:
    if not key.startswith("gsk_"):
        return False, "Groq keys start with gsk_…"
    try:
        r = httpx.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=8.0,
        )
    except httpx.HTTPError as exc:
        return False, f"Network error: {exc}"
    if r.status_code == 200:
        return True, "Groq key works."
    if r.status_code in (401, 403):
        return False, "Groq rejected the key."
    return False, f"Groq returned {r.status_code}: {r.text[:160]}"


# ── pydantic request models ───────────────────────────────────────────────────


class ValidateKeyReq(BaseModel):
    provider: str  # "anthropic" | "groq"
    key:      str


class SaveSetupReq(BaseModel):
    anthropic_key: Optional[str] = None
    groq_key:      Optional[str] = None
    user_name:     Optional[str] = None
    user_address:  Optional[str] = None
    user_city:     Optional[str] = None   # saved to user_profile.json (not .env)


class OpenPrefReq(BaseModel):
    pane: str  # "microphone" | "camera" | "accessibility" | "calendars" | "screen"


class SaveSettingsReq(BaseModel):
    """Catch-all settings update — every field optional, only set keys are written."""
    user_name:        Optional[str] = None
    user_address:     Optional[str] = None
    anthropic_key:    Optional[str] = None
    groq_key:         Optional[str] = None
    elevenlabs_key:   Optional[str] = None
    whisper_model:    Optional[str] = None      # tiny | base | small
    tts_voice:        Optional[str] = None
    tts_rate:         Optional[int] = None
    wake_words:       Optional[str] = None      # comma-separated
    projects_dir:     Optional[str] = None
    anthropic_model:  Optional[str] = None
    groq_model:       Optional[str] = None


_PREF_URLS = {
    "microphone":    "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
    "camera":        "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "calendars":     "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars",
    "screen":        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
}


# ── router ────────────────────────────────────────────────────────────────────


def build_setup_router() -> APIRouter:
    router = APIRouter(prefix="/setup", tags=["setup"])

    @router.get("/status")
    def status() -> dict:
        env = _read_env()
        return {
            "env_path":       str(ENV_PATH),
            "env_exists":     ENV_PATH.exists(),
            "has_anthropic":  bool(env.get("ANTHROPIC_API_KEY")),
            "has_groq":       bool(env.get("GROQ_API_KEY")),
            "user_name":      env.get("USER_NAME", ""),
            "user_address":   env.get("USER_ADDRESS", ""),
            "demo_mode":      env.get("JARVIS_DEMO_MODE", "").lower() in ("1", "true", "yes"),
            "configured":     (
                bool(env.get("ANTHROPIC_API_KEY") or env.get("GROQ_API_KEY"))
                or env.get("JARVIS_DEMO_MODE", "").lower() in ("1", "true", "yes")
            ),
        }

    @router.post("/validate-key")
    def validate_key(req: ValidateKeyReq) -> dict:
        prov = (req.provider or "").lower().strip()
        key  = (req.key or "").strip()
        if not key:
            return {"ok": False, "message": "Key is empty."}
        if prov == "anthropic":
            ok, msg = _validate_anthropic(key)
        elif prov == "groq":
            ok, msg = _validate_groq(key)
        else:
            return {"ok": False, "message": f"Unknown provider: {prov!r}"}
        return {"ok": ok, "message": msg}

    @router.post("/save")
    def save(req: SaveSetupReq) -> dict:
        updates: dict[str, str] = {}
        if req.anthropic_key is not None:
            updates["ANTHROPIC_API_KEY"] = req.anthropic_key.strip()
        if req.groq_key is not None:
            updates["GROQ_API_KEY"] = req.groq_key.strip()
        if req.user_name is not None:
            updates["USER_NAME"] = req.user_name.strip()
        if req.user_address is not None:
            updates["USER_ADDRESS"] = req.user_address.strip()
        if not updates:
            return {"ok": False, "message": "Nothing to save."}
        try:
            _write_env(updates)
        except OSError as exc:
            return {"ok": False, "message": f"Could not write {ENV_PATH}: {exc}"}

        # ── Persist name + city to user_profile.json ─────────────────────────
        try:
            import sys as _sys
            _sys.path.insert(0, str(JARVIS_DIR))
            from profile import save_profile, mark_setup_complete
            profile_data: dict = {}
            if req.user_name:
                profile_data["name"] = req.user_name.strip()
            if req.user_city:
                profile_data["city"] = req.user_city.strip()
            if profile_data:
                save_profile(profile_data)
            mark_setup_complete()
        except Exception as _pe:
            print(f"[setup] profile save skipped: {_pe}")

        return {
            "ok":            True,
            "message":       "Saved. Relaunch JARVIS for changes to take effect.",
            "env_path":      str(ENV_PATH),
            "needs_restart": True,
        }

    @router.get("/all-settings")
    def all_settings() -> dict:
        """Everything the Settings panel can read or write. Keys are masked."""
        env = _read_env()

        def _mask(v: str) -> str:
            if not v:
                return ""
            return f"{v[:6]}…{v[-4:]}" if len(v) > 12 else "•" * len(v)

        return {
            "user_name":       env.get("USER_NAME", ""),
            "user_address":    env.get("USER_ADDRESS", ""),
            "anthropic_key_mask": _mask(env.get("ANTHROPIC_API_KEY", "")),
            "anthropic_key_set":  bool(env.get("ANTHROPIC_API_KEY")),
            "groq_key_mask":      _mask(env.get("GROQ_API_KEY", "")),
            "groq_key_set":       bool(env.get("GROQ_API_KEY")),
            "elevenlabs_key_mask": _mask(env.get("ELEVENLABS_API_KEY", "")),
            "elevenlabs_key_set":  bool(env.get("ELEVENLABS_API_KEY")),
            "anthropic_model": env.get("ANTHROPIC_MODEL", "claude-opus-4-5"),
            "groq_model":      env.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
            "whisper_model":   env.get("WHISPER_MODEL", "base"),
            "tts_voice":       env.get("TTS_VOICE", "Daniel"),
            "tts_rate":        int(env.get("TTS_RATE", "175") or 175),
            "wake_words":      env.get("JARVIS_WAKE_WORDS", "jarvis, hey jarvis, ok jarvis, yo jarvis, okay jarvis"),
            "projects_dir":    env.get("PROJECTS_DIR", "~/Projects"),
            "demo_mode":       env.get("JARVIS_DEMO_MODE", "").lower() in ("1", "true", "yes"),
        }

    @router.post("/save-settings")
    def save_settings(req: SaveSettingsReq) -> dict:
        """Settings panel save — same .env merge, but covers every knob."""
        m: dict[str, Optional[str]] = {
            "USER_NAME":          req.user_name,
            "USER_ADDRESS":       req.user_address,
            "ANTHROPIC_API_KEY":  req.anthropic_key,
            "GROQ_API_KEY":       req.groq_key,
            "ELEVENLABS_API_KEY": req.elevenlabs_key,
            "ANTHROPIC_MODEL":    req.anthropic_model,
            "GROQ_MODEL":         req.groq_model,
            "WHISPER_MODEL":      req.whisper_model,
            "TTS_VOICE":          req.tts_voice,
            "TTS_RATE":           str(req.tts_rate) if req.tts_rate is not None else None,
            "JARVIS_WAKE_WORDS":  req.wake_words,
            "PROJECTS_DIR":       req.projects_dir,
        }
        updates = {k: (v or "").strip() for k, v in m.items() if v is not None}
        if not updates:
            return {"ok": False, "message": "Nothing to save."}
        try:
            _write_env(updates)
        except OSError as exc:
            return {"ok": False, "message": f"Could not write {ENV_PATH}: {exc}"}
        return {
            "ok":            True,
            "message":       "Saved. Some changes need a relaunch (model, wake words, voice).",
            "needs_restart": True,
        }

    @router.post("/enable-demo")
    def enable_demo() -> dict:
        """Flip JARVIS_DEMO_MODE=1 in .env so the user can try without a key."""
        try:
            _write_env({"JARVIS_DEMO_MODE": "1"})
        except OSError as exc:
            return {"ok": False, "message": f"Could not write {ENV_PATH}: {exc}"}
        return {
            "ok":            True,
            "message":       "Demo mode enabled. Relaunch JARVIS to try it out.",
            "needs_restart": True,
        }

    @router.post("/disable-demo")
    def disable_demo() -> dict:
        try:
            _write_env({"JARVIS_DEMO_MODE": ""})
        except OSError as exc:
            return {"ok": False, "message": f"Could not write {ENV_PATH}: {exc}"}
        return {"ok": True, "needs_restart": True}

    @router.post("/open-pref")
    def open_pref(req: OpenPrefReq) -> dict:
        url = _PREF_URLS.get(req.pane)
        if not url:
            return {"ok": False, "message": f"Unknown pane: {req.pane!r}"}
        try:
            subprocess.Popen(["open", url])
        except OSError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    return router
