#!/usr/bin/env python3
"""
Hotkey daemon — runs in its own subprocess.
Detects ⌘⇧J and prints TOGGLE to stdout.
If pynput crashes, only this subprocess dies — main app is unaffected.
"""
import sys

try:
    from pynput import keyboard

    def on_activate():
        sys.stdout.write("TOGGLE\n")
        sys.stdout.flush()

    with keyboard.GlobalHotKeys({"<cmd>+<shift>+j": on_activate}) as h:
        h.join()

except Exception as e:
    sys.stderr.write(f"hotkey_daemon: {e}\n")
    sys.exit(1)
