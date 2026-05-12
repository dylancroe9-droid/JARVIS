"""
Global hotkey listener for macOS.
Requires Accessibility permission (System Settings > Privacy > Accessibility).
Default shortcut: ⌘+⇧+J (Command + Shift + J)

On first run macOS will prompt for Accessibility access — grant it once
and the hotkey will work from any app.
"""

from __future__ import annotations

import threading
from typing import Callable

from pynput import keyboard

# Default hotkey — configurable via .env JARVIS_HOTKEY
DEFAULT_HOTKEY = "<cmd>+<shift>+j"


class HotkeyManager:
    def __init__(self, callback: Callable[[], None], hotkey: str = DEFAULT_HOTKEY) -> None:
        self._callback = callback
        self._hotkey_str = hotkey
        self._listener: keyboard.GlobalHotKeys | None = None

    def start(self) -> None:
        """Start listening in a daemon thread."""
        self._listener = keyboard.GlobalHotKeys(
            {self._hotkey_str: self._on_hotkey}
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()

    def _on_hotkey(self) -> None:
        self._callback()
