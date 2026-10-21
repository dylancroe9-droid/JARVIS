# Alex — distribution & runtime playbook

Everything about getting Alex onto a student's Mac. Read top to bottom once.

## Build it

```bash
cd jarvis-app
npm run build:alex          # → dist/Alex-<version>-arm64.dmg  (unsigned, ad-hoc)
```

`build:alex` writes a `product.build` marker so the packaged app self-identifies
as Alex (no env var needed), bakes in the blue accent + Alex icon, and tells the
Python backend `JARVIS_PRODUCT=alex`. The engine is shared with JARVIS — this is
the same source, flagged.

## Getting it on a buyer's Mac

Alex is **not notarized** (that needs a paid Apple Developer account — 18+ and a
card, deferred). How you hand it over decides how much Gatekeeper fights you:

| Method | Quarantined? | Experience |
|---|---|---|
| **USB stick / direct copy** | No | Opens right away (ad-hoc signed). **Best for cash-at-school.** |
| AirDrop | Yes | "Unidentified developer" — needs the unlock step below |
| Download / email | Yes | Same as AirDrop |

**So for in-person sales: put `Alex.app` on a USB stick, copy it to their
`/Applications`, done.** No Gatekeeper wall at all.

For the quarantined cases, ship these two files (in `dist-extras/`) alongside:
- **`Install Alex.md`** — the student's 30-second guide.
- **`Unlock Alex.command`** — double-click to clear the quarantine flag, no typing.

## The one thing left before it actually works on someone else's Mac: the RUNTIME

⚠️ **The DMG today bundles the Python *source* but not a Python *runtime*.** On a
Mac without the dev `.venv`, the backend won't start — Alex opens, shows its face,
then "Alex isn't responding." This is the real packaging wall, and there are two
ways through it. **This is a product decision — pick one:**

### Option A — bundle the full runtime (keep voice)
Ship a self-contained Python (via [python-build-standalone]) with every dep
installed. torch has 3.14 wheels so it *can* build, but:
- ~1.2 GB app (torch 442M + playwright 135M + whisper's numba/llvmlite/scipy).
- Playwright also needs its browser binaries bundled.
- Big, slow, and must be tested on a *clean* Mac — fragile.

### Option B — text + screenshot Alex, voice as v2  ✅ recommended
Alex's whole whitelist (screenshot→answer, practice tests, web/dict/calc) needs
**none** of torch/whisper/resemblyzer — those are only for always-on *voice input*.
Drop them and the bundle is ~150–250 MB, trivially self-contained, and ships now.

The two features Alex is sold on work fully without voice. A student types or
screenshots; that's a complete product. Voice becomes a v2 upgrade.

**Cost:** `server.py:44` hard-imports `from voice.speaker import Speaker`, so the
voice stack has to become an optional/lazy import before the server can start
without it. That's shared-core work (`server.py`, `voice/`) — coordinate with the
JARVIS session so JARVIS's always-on voice is untouched.

**Recommendation:** Option B. It's the difference between a 200 MB app that ships
this week and a 1.2 GB one that might not build clean. Sell text+screenshot Alex
first; add bundled voice once there's revenue to justify the 18+ Apple account.

## Later: real notarization (when an adult account exists)
See `BUILD.md` — set the Apple Developer cert + `notarytool` credentials and
`electron-builder` auto-signs. That removes the unlock step entirely. Needs the
$99/yr membership (18+).

[python-build-standalone]: https://github.com/astral-sh/python-build-standalone
