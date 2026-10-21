# Unified Mode System

JARVIS runs in one of four **modes** that decide how intrusive he is and how the
HUD looks. The mode is picked **automatically** from what's on screen, and can be
overridden by voice.

| Mode     | When                                   | Behavior                                                            |
|----------|----------------------------------------|---------------------------------------------------------------------|
| `normal` | Desktop, chat, settings, plain browsing| Full HUD (v2 sphere), notifications spoken.                          |
| `work`   | Code editor, terminal, docs, work site | Compact corner strip, notifications **on**, screen context cached.  |
| `watch`  | A video is playing (YouTube/Netflix/…) | Compact strip, notifications **silenced**, YouTube ads auto-skipped.|
| `gaming` | A game / launcher is frontmost         | Compact strip, notifications **silenced**, **sticky** (see below).  |

All three active modes (`work`/`watch`/`gaming`) show the same small corner
widget with a colored **badge** naming the mode.

## How the mode is chosen

`tools/mode_manager.py` polls the **frontmost app + window title** every 30s and
classifies it (`classify()`), priority **watch > gaming > work > normal**. This is
a cheap, deterministic heuristic — *not* a vision/LLM call. A 30s LLM screenshot
judgment would burn the Groq rate limit (the same limit that caused the old 47s
hangs) and wouldn't be more accurate for "which app is in front." Ad-skipping,
which *does* need pixels, is handled separately and continuously by
`tools/screen_watcher.py` (Apple Vision OCR every 1.5s).

> Window-title classification (e.g. detecting "YouTube" in a Chrome title) needs
> macOS **Screen Recording** permission — the same permission the screen watcher
> already requires. Without it, native-app detection (VLC, Steam, VS Code) still
> works, but browser-title detection does not.

## Sticky vs. tracking

- **gaming is sticky.** Once entered (auto or by voice) it stays until you say
  *"exit gaming mode"* — games constantly alt-tab to Discord, a wiki, or a
  loading screen, and flapping out of gaming mode every 30s would be worse than
  useless.
- **watch / work track the screen.** They switch as the frontmost app changes.
- A **forced** watch/work (voice or a pull-up) holds until the frontmost **app**
  changes, then auto-detection takes over again.

## Voice commands

| Say                                              | Effect                               |
|--------------------------------------------------|--------------------------------------|
| "gaming mode"                                    | Force gaming (sticky).               |
| "exit gaming mode" / "back to normal"            | Leave the current mode.              |
| "work mode"                                      | Force work.                          |
| "exit work mode" / "back to normal"              | Leave work/watch.                    |
| "pull up <site>" (Netflix, Peacock, YouTube, …)  | Open it → **watch** mode.            |
| "pull up <site>" (non-streaming)                 | Open it → **work** mode.             |
| "stop auto mode" / "stop switching modes"        | Turn OFF auto-switching, go normal.  |
| "resume auto mode"                               | Turn auto-switching back on.         |

If you say *"exit gaming mode"* while the game is still frontmost, JARVIS tells
you honestly that it's staying in gaming mode until you close the game.

## Wiring (server.py)

- `_on_mode_change(mode, reason)` — the single place that applies a mode's
  side-effects (flags, HUD broadcast, notification mute, screen monitor,
  announcement). Registered as the manager callback at startup.
- `_enter_work_mode()` — pins work mode via the manager and speaks the custom
  line (used by the "work mode" command and the pull-up intercept).
- The manager fires its callback **outside** its state lock (the callback speaks,
  and TTS blocks), serialized by a fire-lock with a current-mode recheck so a
  voice command racing an auto-poll can't leave a stale badge.

## Concurrency notes

`_on_mode_change` can run on the manager's poll thread **or** a request thread
(when a voice command forces/exits). Module bool flags (`_work_mode`,
`_gaming_mode`, `_notifications_muted`) are only ever set to definite values per
mode (not read-modify-write), the screen monitor start is lock-guarded, and the
manager's fire-lock serializes callbacks — so the two threads can't leave the
HUD, the flags, and the spoken line disagreeing.

## Tests

`tests/test_mode_manager.py` — classification matrix, force/exit, sticky gaming,
pin-release-on-app-change, pause/resume, and the fire supersession guard.
