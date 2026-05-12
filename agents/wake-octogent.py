#!/usr/bin/env python3
"""Wake all live OctoGent terminals and submit any pending input.

Why this exists:
  OctoGent only spawns the underlying `claude` CLI inside a terminal when a
  websocket client attaches to that terminal. Creating terminals via the
  CLI alone leaves them as idle zsh shells — claude never boots, so they
  appear "idle" but aren't actually agents yet.

  Additionally, channel messages are written to the PTY but the trailing
  Enter sometimes doesn't register in Claude Code's TUI, so messages get
  pasted but not submitted.

What this does:
  1. Lists all live terminals via the OctoGent API.
  2. Opens a websocket to each (which triggers the bootstrap that actually
     launches claude).
  3. Holds the connection long enough for claude to come up.
  4. Sends a final Enter to submit any pending pasted input.
  5. Disconnects.

Run any time the agents look "stuck" or after creating new terminals.
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

import websockets

OCTOGENT_URL = "http://127.0.0.1:8787"
WS_BASE = "ws://127.0.0.1:8787/api/terminals"
HOLD_SECONDS = 12.0


def list_live_terminals() -> list[str]:
    with urllib.request.urlopen(f"{OCTOGENT_URL}/api/terminal-snapshots") as r:
        data = json.load(r)
    return [
        t["terminalId"]
        for t in data
        if t.get("state") == "live" and t.get("terminalId", "").startswith("terminal-")
    ]


async def wake_one(terminal_id: str, hold: float = HOLD_SECONDS) -> str:
    url = f"{WS_BASE}/{terminal_id}/ws"
    try:
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            # Drain initial history so we know claude has started
            try:
                await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                pass
            # Hold connection so bootstrap + initial-prompt timers fire
            await asyncio.sleep(hold)
            # Submit anything that might be sitting in the input box
            await ws.send(json.dumps({"type": "input", "data": "\r"}))
            await asyncio.sleep(0.5)
        return f"  ✓ {terminal_id}"
    except Exception as e:  # noqa: BLE001
        return f"  ✗ {terminal_id}: {e}"


async def main() -> None:
    ids = list_live_terminals()
    if not ids:
        print("No live terminals.")
        return
    print(f"Waking {len(ids)} terminals...")
    results = await asyncio.gather(*(wake_one(t) for t in ids))
    for r in results:
        print(r)
    print("\nFinal states:")
    with urllib.request.urlopen(f"{OCTOGENT_URL}/api/terminal-snapshots") as r:
        data = json.load(r)
    for t in data:
        if t.get("state") == "live" and t.get("terminalId", "").startswith("terminal-"):
            print(
                f"  {t['terminalId']:14s}  {t.get('tentacleName',''):14s}"
                f"  rt={t.get('agentRuntimeState','?')}"
            )


if __name__ == "__main__":
    asyncio.run(main())
