#!/usr/bin/env python3
"""OctoGent 24/7 autopilot.

Responsibilities:
  1. Keep the OctoGent server (localhost:8787) up. Restart if it dies.
  2. Wake any newly-created terminal so claude actually launches inside it.
  3. Nudge any agent that goes idle while it still has unchecked todo items.
  4. Respect a runtime "mode" file so Dylan can throttle aggression on demand.

Modes (stored in ~/JARVIS/agents/.mode as a single word):
  chill  (default) — gentle: longer idle threshold, longer cooldown.
  night            — aggressive: short threshold, no cooldown, work hard.
  stop             — pause: don't nudge anything this tick (does NOT kill agents).

Run via launchd (see ~/Library/LaunchAgents/com.dylan.jarvis-autopilot.plist).
Or manually:
    nohup ~/JARVIS/.venv/bin/python ~/JARVIS/agents/autopilot.py > /tmp/autopilot.log 2>&1 &
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

# ---- paths and config ------------------------------------------------------

JARVIS = Path.home() / "JARVIS"
AGENTS_DIR = JARVIS / "agents"
TENTACLES = JARVIS / ".octogent" / "tentacles"
MODE_FILE = AGENTS_DIR / ".mode"
INBOX_FILE = AGENTS_DIR / "inbox.txt"

OCTOGENT_BIN = str(Path.home() / ".npm-global/bin/octogent")
START_SCRIPT = str(JARVIS / "start-octogent.sh")
OCTOGENT_URL = "http://127.0.0.1:8787"
WS_BASE = "ws://127.0.0.1:8787/api/terminals"

MODE_PROFILES = {
    # (check_interval, idle_threshold, nudge_cooldown) all in seconds
    "chill": (60, 60, 180),
    "night": (20, 15, 45),
    "stop":  (30, 99999, 99999),  # functionally pauses
}

NUDGE_MESSAGE = (
    "AUTOPILOT NUDGE: keep going. Read your todo.md, pick the next unchecked "
    "[ ] item, do it completely, edit todo.md to mark it [x], then move to "
    "the next one. Don't ask permission for routine tool use. Only stop if "
    "a todo genuinely needs human input (real choice, money spend, "
    "destructive op)."
)

NIGHT_BOOST_MESSAGE = (
    "NIGHT MODE: Dylan is asleep / hands-off. Work as much as you can without "
    "him. Make reasonable defaults. If something genuinely needs his "
    "decision, write the question to ~/JARVIS/agents/asks.md and skip that "
    "todo for now — keep going on the others."
)

# ---- helpers ---------------------------------------------------------------


def now() -> float:
    return time.time()


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def read_mode() -> str:
    try:
        m = MODE_FILE.read_text().strip().lower()
    except FileNotFoundError:
        return "chill"
    return m if m in MODE_PROFILES else "chill"


def octogent_alive() -> bool:
    try:
        with urllib.request.urlopen(OCTOGENT_URL, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def start_octogent() -> bool:
    if not Path(START_SCRIPT).exists():
        log(f"start script not found: {START_SCRIPT}")
        return False
    log("OctoGent down — restarting via start-octogent.sh")
    try:
        subprocess.run(["bash", START_SCRIPT], timeout=20, capture_output=True)
    except Exception as e:
        log(f"start failed: {e}")
        return False
    # Give it a moment to bind the port
    for _ in range(10):
        if octogent_alive():
            log("OctoGent back up")
            return True
        time.sleep(1)
    log("OctoGent did not come back up in time")
    return False


def list_terminals() -> list[dict]:
    try:
        with urllib.request.urlopen(f"{OCTOGENT_URL}/api/terminal-snapshots", timeout=3) as r:
            return json.load(r)
    except Exception as e:
        log(f"list_terminals failed: {e}")
        return []


def has_unchecked_todos(tentacle_id: str) -> bool:
    todo = TENTACLES / tentacle_id / "todo.md"
    if not todo.exists():
        return False
    return bool(re.search(r"^\s*-\s*\[\s*\]\s+", todo.read_text(), flags=re.MULTILINE))


def count_todos(tentacle_id: str) -> tuple[int, int]:
    todo = TENTACLES / tentacle_id / "todo.md"
    if not todo.exists():
        return (0, 0)
    text = todo.read_text()
    unchecked = len(re.findall(r"^\s*-\s*\[\s*\]\s+", text, flags=re.MULTILINE))
    checked = len(re.findall(r"^\s*-\s*\[x\]\s+", text, flags=re.MULTILINE | re.IGNORECASE))
    return (unchecked, checked)


def channel_send(terminal_id: str, message: str) -> bool:
    try:
        r = subprocess.run(
            [OCTOGENT_BIN, "channel", "send", terminal_id, message],
            capture_output=True, text=True, timeout=10,
            cwd=str(JARVIS),
        )
        return r.returncode == 0
    except Exception:
        return False


async def ws_attach_and_submit(terminal_id: str, hold_seconds: float = 0.0) -> None:
    """Attach to a terminal's WS so OctoGent bootstraps claude in it,
    optionally hold for `hold_seconds`, then submit Enter to land any
    pasted input."""
    url = f"{WS_BASE}/{terminal_id}/ws"
    try:
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                pass
            if hold_seconds > 0:
                await asyncio.sleep(hold_seconds)
            await ws.send(json.dumps({"type": "input", "data": "\r"}))
            await asyncio.sleep(0.5)
    except Exception as e:
        log(f"  ws_attach_and_submit({terminal_id}) failed: {e}")


# ---- inbox processing ------------------------------------------------------

# When ~/JARVIS/agents/inbox.txt has lines, route each to an agent and clear.

def _agent_for_keyword(line: str, terminals: list[dict]) -> str | None:
    """Cheap heuristic router. Picks tentacle id from the line text."""
    s = line.lower()
    keywords = {
        "jarvis":   ["jarvis", "voice", "wake word", "stt", "tts", "server.py", "app.py", "brain/", "tools/"],
        "personal": ["text ", "imessage", "calendar", "remind", "gym", "workout", "max ", "bennett", "neil", "andrew", "holden", "friend"],
        "web-leads":["lead", "prospect", "cold call", "cold email", "no website", "outreach", "pitch"],
        "web-build":["build a site", "build site", "scaffold", "landing", "website for", "deploy", "astro", "tailwind"],
        "car-flips":["flip", "marketplace", "craigslist", "car ", "convertible", "miata", "wrx", "bmw", "subaru", "civic", "mustang"],
    }
    score: dict[str, int] = {k: 0 for k in keywords}
    for k, kws in keywords.items():
        score[k] += sum(1 for kw in kws if kw in s)
    best = max(score.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        return None
    # Make sure that tentacle has a live terminal
    live_tentacles = {t.get("tentacleId") for t in terminals if t.get("state") == "live"}
    if best[0] not in live_tentacles:
        return None
    return best[0]


def _terminal_id_for(tentacle: str, terminals: list[dict]) -> str | None:
    # Prefer the most recently created live terminal for that tentacle
    cands = [t for t in terminals if t.get("state") == "live" and t.get("tentacleId") == tentacle]
    if not cands:
        return None
    cands.sort(key=lambda t: t.get("createdAt", ""), reverse=True)
    return cands[0].get("terminalId")


async def drain_inbox(terminals: list[dict]) -> None:
    if not INBOX_FILE.exists():
        return
    text = INBOX_FILE.read_text()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return
    leftover: list[str] = []
    for line in lines:
        tentacle = _agent_for_keyword(line, terminals)
        if not tentacle:
            log(f"  inbox: couldn't route '{line[:60]}' — leaving in inbox")
            leftover.append(line)
            continue
        tid = _terminal_id_for(tentacle, terminals)
        if not tid:
            log(f"  inbox: no live terminal for {tentacle}")
            leftover.append(line)
            continue
        ok = channel_send(tid, line)
        if ok:
            await ws_attach_and_submit(tid)
            log(f"  inbox: sent '{line[:60]}' -> {tentacle} ({tid})")
        else:
            leftover.append(line)
    # Re-write inbox with only the lines we couldn't route
    if leftover:
        INBOX_FILE.write_text("\n".join(leftover) + "\n")
    else:
        INBOX_FILE.write_text("")


# ---- the main loop ---------------------------------------------------------

async def tick(state: dict) -> None:
    mode = read_mode()
    check_interval, idle_threshold, nudge_cooldown = MODE_PROFILES.get(mode, MODE_PROFILES["chill"])
    state["last_mode"] = mode

    # 1. Make sure OctoGent server is up
    if not octogent_alive():
        if not start_octogent():
            return  # nothing else we can do this tick

    terminals = list_terminals()
    if not terminals:
        return

    # 2. Drain the inbox if anything was dropped in
    await drain_inbox(terminals)

    # Refresh after potentially sending things
    terminals = list_terminals()

    # 3. Apply night-mode boost message once when mode flipped to night
    if mode == "night" and not state.get("sent_night_boost"):
        log("entering NIGHT mode — broadcasting boost message to all live agents")
        for t in terminals:
            if t.get("state") == "live" and t.get("terminalId", "").startswith("terminal-"):
                channel_send(t["terminalId"], NIGHT_BOOST_MESSAGE)
                await ws_attach_and_submit(t["terminalId"])
        state["sent_night_boost"] = True
    if mode != "night":
        state["sent_night_boost"] = False

    # 4. Nudge idle agents with unchecked todos
    if mode == "stop":
        log(f"mode=stop  ({len(terminals)} terminals — not nudging)")
        return

    for t in terminals:
        if t.get("state") != "live":
            continue
        tid = t.get("terminalId", "")
        if not tid.startswith("terminal-"):
            continue
        runtime = t.get("agentRuntimeState", "")
        tentacle = t.get("tentacleId", "")

        # Newly created terminal with no agent state yet — wake it
        if runtime in (None, "", "?") and not state.get("woke_" + tid):
            log(f"  waking new terminal {tid}")
            await ws_attach_and_submit(tid, hold_seconds=10)
            state["woke_" + tid] = True
            continue

        # Stuck on a permission prompt — press Enter (default = Yes)
        if runtime == "waiting_for_permission":
            last_perm = state.get("last_perm_" + tid, 0)
            if now() - last_perm > 5:
                log(f"  auto-approve permission prompt on {tid} ({tentacle})")
                await ws_attach_and_submit(tid)
                state["last_perm_" + tid] = now()
            continue

        if runtime == "idle":
            state.setdefault("idle_since_" + tid, now())
        else:
            state.pop("idle_since_" + tid, None)
            continue

        unchecked, checked = count_todos(tentacle)
        idle_age = now() - state["idle_since_" + tid]
        last_nudge = state.get("last_nudge_" + tid, 0)
        nudge_age = now() - last_nudge if last_nudge else None

        if (
            unchecked > 0
            and idle_age >= idle_threshold
            and (nudge_age is None or nudge_age >= nudge_cooldown)
        ):
            log(f"  nudge {tid} ({tentacle}) — todos {checked}+{unchecked}, idle {idle_age:.0f}s")
            if channel_send(tid, NUDGE_MESSAGE):
                await ws_attach_and_submit(tid)
                state["last_nudge_" + tid] = now()


async def main() -> None:
    log(f"autopilot starting — mode file: {MODE_FILE}")
    state: dict = {}
    while True:
        try:
            await tick(state)
        except Exception as e:
            log(f"tick error: {e}")
        # Sleep based on current mode
        ci, _, _ = MODE_PROFILES.get(state.get("last_mode", "chill"), MODE_PROFILES["chill"])
        await asyncio.sleep(ci)


if __name__ == "__main__":
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    if not MODE_FILE.exists():
        MODE_FILE.write_text("chill\n")
    if not INBOX_FILE.exists():
        INBOX_FILE.write_text("")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("autopilot stopped")
