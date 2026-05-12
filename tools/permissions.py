"""
Permission callback — lets server.py inject a UI-based confirmation
function that file/shell tools call before making changes.

When running without the server (e.g. tests), _callback is None and
everything is auto-approved.
"""

from __future__ import annotations

_callback = None   # set by server.py on startup


def set_callback(fn) -> None:
    global _callback
    _callback = fn


def request(message: str) -> bool:
    """
    Ask the user for permission. Blocks until they respond (or 30 s timeout).
    Returns True if approved, False if denied/timed out.
    """
    if _callback is None:
        return True   # no UI — auto-approve (terminal / test mode)
    return _callback(message)
