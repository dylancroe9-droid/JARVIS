# Self-Inspect (Tier 1)

How JARVIS reads his own code, logs, and diagnostic findings so he can answer
"why did you do X" questions with real evidence instead of guesses.

## Quick map

```
┌─────────────────────────────────────────────────────────────────────┐
│  User: "why did the render connection fail?"                        │
└──────────────────┬──────────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  server.py self-inspect intercept                                   │
│  - Detects phrase via _INVESTIGATE_PHRASES or _INVESTIGATE_FAIL_RE  │
│  - Looks up matching check in _LAST_INVESTIGATIONS stash            │
│  - Appends [INTERNAL CONTEXT...] to the brain prompt                │
│  - Says "Let me check, sir." to user                                │
└──────────────────┬──────────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  brain/jarvis.py                                                    │
│  - Receives enriched prompt with diagnostic evidence                │
│  - Personality nudge tells it: NEVER web_search for self-questions  │
│  - Calls one of the 6 self-inspect tools                            │
└──────────────────┬──────────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  brain/_executor.py — dispatch + rate limit                         │
│  - Enforces 6 self-inspect calls per turn (cap)                     │
│  - Logs every call to stdout as [self-inspect] #N name(args)        │
│  - Calls into tools/self_inspect.py                                 │
└──────────────────┬──────────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│  tools/self_inspect.py — actual file / log / history access        │
│  - read_jarvis_file, grep_jarvis, list_jarvis_dir                   │
│  - read_jarvis_log (tails /tmp/jarvis_server.log)                   │
│  - read_jarvis_history (reads ~/.jarvis_history.json)               │
│  - Hard safety: blocked dirs, blocked files, path-escape blocks     │
└─────────────────────────────────────────────────────────────────────┘
```

## The 6 brain tools

| Tool | Purpose | Cap |
|------|---------|-----|
| `read_my_code(path)` | Read a JARVIS source file | 1 MB |
| `search_my_code(pattern, file_glob?, max_results?)` | Regex grep across source | 50 hits |
| `list_my_files(path)` | Directory listing | — |
| `read_my_logs(lines?)` | Tail captured stdout | 500 lines, 2 MB tail |
| `read_my_history(max_turns?)` | Recent conversation turns | 20 turns default |
| `get_last_diagnostic_findings(check_name?)` | Pull stashed investigation evidence | — |

All 6 share a **per-turn rate cap of 6 calls**. Counter resets at the start
of each `brain.chat()` turn. Going over returns `__self_inspect_cap__` so the
brain stops looking and answers with what it has.

## Safety boundaries

`tools/self_inspect.py:_safe_path` enforces:

- **Containment**: paths must resolve under `/Users/dylanroe/JARVIS`. Escapes
  like `../../etc/passwd` return `None`.
- **Blocked files**: `.env`, `.env.local`, `.env.production`, `credentials.json`,
  `credentials.yaml`, anything ending in `.pem` / `.key` / `.crt` / `.pyc` /
  `.so` / `.dylib`.
- **Blocked dirs**: `node_modules/`, `__pycache__/`, `.venv/`, `venv/`, `.git/`,
  `dist/`, `build/`, `.cache/` — invisible to all four tools.
- **Privacy files outside the project**:
  - `~/.jarvis_history.json` — accessible *only* via the dedicated
    `read_jarvis_history()` (intentional, separate path, not via generic
    `read_jarvis_file`).
  - `~/.jarvis_voice_profile.npy` — blocked entirely.

## The investigation stash

`tools/diagnostics.py:_LAST_INVESTIGATIONS` is an in-memory dict keyed by
check name. Populated automatically when `run_full_diagnostic()` detects a
failure — the runner reads `/tmp/jarvis_server.log`, filters lines matching
the check's `log_keywords`, and stashes:

```python
{
    "check": "screen_watcher",
    "label": "Screen watcher",
    "log_matches": ["[watcher] failed to start: ..."],
    "source_files": ["tools/screen_watcher.py"],
    "summary": "Found 3 related log line(s)."
}
```

**TTL**: 30 minutes. After expiration `get_last_investigations()` returns an
empty dict so the brain doesn't reference stale findings.

**Lifecycle**:
- Cleared at the start of every `run_full_diagnostic()` run
- Populated by the runner on each failure
- Auto-cleared after `_STASH_TTL_SEC` (30 min)

## The intercept

`server.py` runs a self-inspect intercept in `_process_unsafe`:

1. **Trigger detection** — two paths, OR'd together:
   - `_INVESTIGATE_PHRASES` (literal substring match) — covers
     "look at your code", "what went wrong", "what does your X look like",
     "explain that failure", etc.
   - `_INVESTIGATE_FAIL_RE` (regex) — catches `why|how + ... + fail|failed|
     broken|down|off|wrong|stop|stopped|drop|dropped|crash|crashed`. Handles
     natural phrasings like "why the render connection failed" without
     needing every variant in the substring list.
2. **Check matching** — if findings exist, try to match the user's text
   against a registered check via `(name, label words)`. If matched, pre-load
   that specific finding. Otherwise pre-load a compact summary of all
   findings.
3. **Self-referential gate** — only speak the "Let me check, sir."
   acknowledgement if either a check was matched OR a self-referential hint
   was found in the text (`your code`, `yourself`, `your watcher`, etc.).
   This avoids surprise acknowledgements on questions about external things
   that happened to match the regex.
4. **Brain dispatch** — does NOT bypass the brain. Just appends an
   `[INTERNAL CONTEXT — DO NOT speak the brackets aloud: ...]` block to the
   text passed to `jarvis.chat()`. The original user message stays clean
   for chat display.

The `_display_text` capture at the top of `_process_unsafe` ensures the user
message bubble in the chat UI never shows the injected context block.

## Audit trail

Every self-inspect call prints to stdout:

```
[self-inspect] #1 get_last_diagnostic_findings(check_name='screen_watcher')
[self-inspect] #2 read_my_code(path='tools/screen_watcher.py')
```

That stream is captured by `start.sh`'s `tee` into `/tmp/jarvis_server.log`,
so JARVIS can review his own audit trail via `read_my_logs`.

## Tests

Three suites under `tests/`:

- `test_self_inspect.py` — 33 unit tests over the file / grep / list / log /
  history helpers and `_safe_path`. Covers all safety boundaries and the
  graceful-degradation paths.
- `test_diagnostic_flow.py` — 14 integration tests over the
  `run_full_diagnostic()` event contract, investigation stash population /
  clearing / TTL expiration.
- `test_intercept_compliance.py` — 43 parametrized tests over real user
  phrasings — *should fire* vs *should NOT fire* against the trigger
  detection. Extracts the live phrase tuple from server.py source so the
  tests stay in sync with code.

Run all: `.venv/bin/python -m pytest tests/`. Expected: 90 passed.

## Restart-to-reload

Most changes require a server restart (`./start.sh`) because the personality
prompt, intercept list, and check registry are loaded at module import. The
investigation stash is in-process memory and gets cleared on restart by
definition.
