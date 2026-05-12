# JARVIS

An Iron-Man-style voice assistant for your Mac. JARVIS lives in a floating
window in the corner of your screen, listens for your voice, and answers
back — and can also see your screen, read your calendar, run commands,
and write code.

> **Status:** v1, in active development. Solo dev project, ships as a
> Mac-only app. macOS 12+ (Monterey or later) on Apple Silicon or Intel.

## What you get

- 🎙️ **Voice in, voice out.** Say "Hey JARVIS" from anywhere on your Mac.
- 👁️ **Screen vision.** "JARVIS, what's this error?" — it reads what you're
  looking at.
- 🧠 **Real conversation.** Powered by Claude or Llama — your choice.
- ⌨️ **Hotkey toggle.** ⌘⇧J shows or hides the HUD from anywhere.
- 🔒 **Stays on your Mac.** Wake word, speech-to-text, and conversation
  memory all run locally. Only the things you explicitly ask are sent
  to the AI provider you chose.

---

## Install — for non-developers

This is the simple path. If you're comfortable with a terminal, see
[Install — for developers](#install--for-developers) below.

### What you need first

- A Mac running macOS 12 (Monterey) or newer.
- About 2 GB of free disk space (most of it is the local speech model).
- An API key from one of these AI providers:
  - **Claude / Anthropic** — best quality, costs about $0.01–0.05 per
    conversation. Sign up at <https://console.anthropic.com>.
  - **Llama / Groq** — free, very fast, less capable. Sign up at
    <https://console.groq.com> (Google sign-in, no credit card).
- About 10 minutes for the first install.

### Step-by-step

1. **Download JARVIS.** Grab the latest `JARVIS-1.0.0-arm64.dmg` from the
   [releases page]([replace with link]).

2. **Open the DMG and drag JARVIS to Applications.** The first time you
   open it, macOS will say "JARVIS can't be opened because Apple cannot
   check it for malicious software." That's because we're a small
   developer — right-click the app and choose **Open** instead. You only
   need to do this once.

3. **The setup wizard runs automatically.** It walks you through:
   - Picking a brain (Claude or Llama, or "Try without a key" demo mode).
   - Pasting your API key — we validate it live.
   - Granting macOS permissions (Microphone, Camera, Accessibility, etc.).
   - That's it.

4. **Talk to JARVIS.** Say "Hey JARVIS, what time is it?" or press ⌘⇧J
   to type instead.

### Wait — what's "demo mode"?

Demo mode lets you try JARVIS without any API key. You'll hear it
speak, see the HUD, and can ask basic things like the time. Real
conversation, screen vision, and tools all need a real key. Switch to
a paid provider any time from Settings (⚙ icon).

### Things that might trip you up

- **The app says "JARVIS server isn't responding"** → Quit and reopen
  the app. If it keeps happening, file an issue with `~/JARVIS/logs.txt`
  attached.
- **Hotkey ⌘⇧J does nothing** → System Settings → Privacy & Security →
  Accessibility → toggle JARVIS on.
- **JARVIS hears itself** → That's a known mic/speaker bleed issue on
  laptop speakers. Use headphones or AirPods if it gets bad.
- **Spinner forever, no response** → Your API key probably ran out of
  credits or hit a rate limit. Open Settings → check your provider's
  billing page.

---

## Install — for developers

Clone, run setup, launch:

```bash
git clone https://github.com/[user]/jarvis.git
cd jarvis
./setup.sh                 # creates .venv, installs deps, downloads Whisper
./start.sh                 # launches Python server + Electron app
```

`setup.sh` will:

1. Find a Python 3.9+ on your PATH (3.11 recommended).
2. Make a venv at `./.venv`.
3. `pip install -r requirements.txt`. The biggest download is openai-whisper,
   which pulls in PyTorch (~1 GB).
4. Install Playwright Chromium (~200 MB) for headless browsing tools.
5. Verify ffmpeg is installed (uses Homebrew if not).
6. Copy `.env.example → .env`. Edit it, or use the in-app wizard.

Other entry points:

| Script           | What it runs               | Use when |
|------------------|---------------------------|----------|
| `./start.sh`     | Python server + Electron  | Normal — this is the product |
| `./run.sh`       | `chat.py` — terminal CLI  | Headless / debugging |
| `./run_app.sh`   | `app.py` — older HUD      | Legacy |
| `chat.py`        | Pure Python REPL          | Quick brain tests |

Build a distributable DMG: see [`jarvis-app/BUILD.md`](jarvis-app/BUILD.md).

Profile voice latency: `JARVIS_PROFILE=1 ./start.sh` — see
[`voice/LATENCY.md`](voice/LATENCY.md).

---

## Configuration

Settings live in `~/JARVIS/.env`. The in-app Settings panel (⚙ icon in
the HUD header) covers the common ones; for advanced flags edit `.env`
directly.

| Variable              | What it does                                     |
|-----------------------|--------------------------------------------------|
| `ANTHROPIC_API_KEY`   | Claude key — primary brain                       |
| `GROQ_API_KEY`        | Groq key — free fallback brain                   |
| `ELEVENLABS_API_KEY`  | ElevenLabs — premium TTS voice                   |
| `WHISPER_MODEL`       | `tiny` / `base` / `small` — STT speed vs accuracy |
| `TTS_VOICE`           | macOS `say` voice when ElevenLabs is unavailable |
| `JARVIS_WAKE_WORDS`   | Comma-separated list of phrases that wake JARVIS |
| `JARVIS_DEMO_MODE`    | `1` to skip the brain and use canned replies     |
| `JARVIS_PROFILE`      | `1` to print per-turn latency breakdowns         |

---

## Privacy + license

- See [`legal/privacy.md`](legal/privacy.md) for a full breakdown of what
  runs locally vs what goes to providers.
- See [`legal/terms.md`](legal/terms.md) for the license + ToS.
- TL;DR: API keys live at `~/JARVIS/.env` (owner-only). Wake-word and
  STT are local. Conversations go directly from your Mac to your chosen
  AI provider — never through us.

---

## Support

- **Bugs** → file at [github.com/.../issues]([replace])
- **Feature requests** → same place, tag `feature`
- **Email** → `[support@jarvis.app]`
