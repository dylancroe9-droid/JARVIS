# Building JARVIS for distribution

## Quick build (unsigned, for local testing)

```bash
cd jarvis-app
npm run build:mac:unsigned
```

Output: `dist/JARVIS-1.0.0-arm64.dmg` (~180 MB) and `dist/JARVIS-1.0.0.dmg` (x64).

The unsigned build will trigger Gatekeeper on a fresh Mac:

> "JARVIS can't be opened because Apple cannot check it for malicious software."

Right-click → Open is the workaround. Acceptable for personal testing — **not** acceptable for paying customers.

## Signed + notarized build (required before charging money)

You need an active **Apple Developer Program** membership ($99/yr) and a
"Developer ID Application" certificate installed in Keychain.

1. Set up the cert (one time):
   ```bash
   # In Apple Developer portal:
   # Certificates → + → Developer ID Application → download → double-click
   ```

2. Set up notarization credentials (one time):
   ```bash
   xcrun notarytool store-credentials JARVIS-NOTARY \
     --apple-id YOUR_APPLE_ID \
     --team-id YOUR_TEAM_ID \
     --password APP_SPECIFIC_PASSWORD
   ```
   App-specific password: appleid.apple.com → Sign-In and Security → App-Specific Passwords.

3. Build:
   ```bash
   export APPLE_ID="your_apple_id@example.com"
   export APPLE_APP_SPECIFIC_PASSWORD="xxxx-xxxx-xxxx-xxxx"
   export APPLE_TEAM_ID="XXXXXXXXXX"
   npm run build:mac
   ```

   electron-builder will auto-sign + notarize if those env vars are set.

## What the build contains

- The Electron app (renderer, preload, main.js)
- Python source from the repo: `brain/`, `voice/`, `tools/`, `ui/`,
  `server.py`, `app.py`, `chat.py`, `config.py`, `requirements.txt`,
  `setup.sh`, `.env.example`
- **Not bundled**: `.venv/` (user installs at first launch via setup.sh),
  `.env`, `memory.txt`, `.setup_done`, `node_modules/`, the previous `dist/`,
  the `jarvis-app/` folder itself, `agents/`, `study_notes/`, `__pycache__/`.

## Known limitations (today)

- The DMG ships only Python source, not a Python runtime. On first launch,
  the app still expects `.venv` from `setup.sh`. **Next step**: bundle a
  portable Python (e.g. python-build-standalone) so non-developers don't
  need to run setup.sh at all.
- ffmpeg is not bundled. The voice loop will fail until the user runs
  `brew install ffmpeg`. Fix: bundle a static ffmpeg binary in `extraResources`.
- No code signing yet — see above.

## Common build failures

**`ENAMETOOLONG`** — usually caused by leftover `dist/` from a previous run
being copied recursively into itself. Run `rm -rf dist` (the `prebuild`
script does this automatically now).

**Missing `assets/icon.icns`** — run `python jarvis-app/assets/build_assets.py`
to regenerate.
