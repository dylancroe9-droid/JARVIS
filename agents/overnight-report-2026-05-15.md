# Overnight Report — 2026-05-15

Autonomous improvement session. Worked through the full backlog — 7 tasks, 7 commits.

---

## What changed

| Commit | Change |
|--------|--------|
| `4f4b3dc` | Import `JARVIS_DIR` from config in server.py instead of inline `os.path` calls |
| `155b76a` | Hardened browser tool errors — friendly messages for DNS failures, timeouts, bad selectors; Playwright not-installed now gives a clear install instruction |
| `66fdc93` | Added `what can you do` voice command — speaks a full capability overview instantly |
| `14942b1` | Memory timestamps — every new fact now stamped `[YYYY-MM-DD]`; dedup still works across timestamps |
| `06a5a24` | README — fixed `[replace with link]` placeholder, added Voice Commands table, Friends Install section, fixed Issues link |
| `4fc2da3` | Added `what's new` voice command — reads last 10 git commits conversationally |
| `0668ffe` | **Bug fix**: `main.py` was importing `GROQ_API_KEY as ANTHROPIC_API_KEY` — anyone using Anthropic key would fail startup validation. Fixed to check either key. |

---

## Bug worth noting

The `GROQ_API_KEY as ANTHROPIC_API_KEY` alias in `main.py` was a real bug — `python main.py` would exit with "No ANTHROPIC_API_KEY found" even if you had a valid Anthropic key in `.env`. Fixed in the last commit.

---

## Suggested next improvements

1. **`what's new` should pull from GitHub** — right now it reads local git log. If Dylan's machine is behind by a few commits, it'll show stale info. Could add a `git fetch --dry-run` first.

2. **Voice command for memory** — "what do you remember about me" / "forget X" as shortcuts in server.py, same pattern as the others.

3. **`install.sh` audit** — haven't verified the install script handles the case where Python 3.11+ isn't installed (some Macs ship with 3.9). Worth a check.

4. **`brain/jarvis.py` model hardcode** — `main.py` prints "Model: claude-sonnet-4-6" but the actual model in `config.py` defaults to `claude-opus-4-5`. These should reference the same constant.

5. **Error recovery in server.py** — when the WebSocket disconnects mid-stream, the worker thread swallows the exception silently. Could add a reconnect backoff.
