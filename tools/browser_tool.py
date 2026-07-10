"""
Browser automation via Playwright.

JARVIS uses a persistent Chromium profile (~/.jarvis_browser/) so sessions
(Google login, etc.) survive between runs. First visit to a site will require
normal login — after that it's automatic.

Screenshot results are returned as Anthropic-format content blocks containing
both an image and text so Claude can *see* what's on screen via vision.
"""

from __future__ import annotations

import base64
import os
import time

# Lazy playwright import — not loaded until the first browser call
_pw_instance = None
_browser_context = None
_page = None


PROFILE_DIR = os.path.expanduser("~/.jarvis_browser")
os.makedirs(PROFILE_DIR, exist_ok=True)


# --------------------------------------------------------------------------- #
# Session management                                                           #
# --------------------------------------------------------------------------- #

def _get_page():
    global _pw_instance, _browser_context, _page

    if _page is not None:
        return _page

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "The browser tool needs Playwright. Run: pip install playwright && playwright install chromium"
        )

    _pw_instance = sync_playwright().start()
    try:
        _browser_context = _pw_instance.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
            ],
            ignore_https_errors=False,
        )
    except Exception:
        # Launch failed (commonly: the persistent profile is locked by a
        # lingering Chromium). Stop the driver we just started so we don't
        # leak a node process every retry, and reset globals so the next
        # call starts clean.
        try:
            _pw_instance.stop()
        except Exception:
            pass
        _pw_instance = None
        _browser_context = None
        _page = None
        raise

    if _browser_context.pages:
        _page = _browser_context.pages[0]
    else:
        _page = _browser_context.new_page()

    return _page


def close_browser() -> str:
    global _pw_instance, _browser_context, _page
    try:
        if _browser_context:
            _browser_context.close()
        if _pw_instance:
            _pw_instance.stop()
        _page = None
        _browser_context = None
        _pw_instance = None
        return "Browser closed."
    except Exception as e:
        return f"Error closing browser: {e}"


# --------------------------------------------------------------------------- #
# Navigation                                                                   #
# --------------------------------------------------------------------------- #

def browser_navigate(url: str, wait_for: str = "networkidle") -> str:
    """Navigate to a URL. Returns page title and current URL."""
    page = _get_page()
    try:
        page.goto(url, wait_until=wait_for, timeout=30_000)
        time.sleep(0.5)  # let JS settle
        return f"Navigated to: {page.url}\nPage title: {page.title()}"
    except Exception as e:
        err = str(e)
        if "net::ERR_NAME_NOT_RESOLVED" in err or "net::ERR_CONNECTION_REFUSED" in err:
            return "Couldn't reach that URL — make sure the address is correct and you're online."
        if "Timeout" in err or "timeout" in err:
            return "Page took too long to load. Try again, or the site may be down."
        return f"Navigation failed: {err}"


# --------------------------------------------------------------------------- #
# Screenshot + vision                                                          #
# --------------------------------------------------------------------------- #

def browser_screenshot() -> list:
    """
    Take a screenshot and return Anthropic content blocks so Claude can *see*
    what's on screen. Returns a list with an image block + text block.
    """
    page = _get_page()
    try:
        shot_bytes = page.screenshot(type="png")
        b64 = base64.b64encode(shot_bytes).decode("utf-8")

        # Get readable text from the page
        try:
            text = page.inner_text("body")
            text = text[:4000]   # cap so we don't flood context
        except Exception:
            text = "(could not extract page text)"

        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": b64,
                },
            },
            {
                "type": "text",
                "text": (
                    f"Current URL: {page.url}\n"
                    f"Page title: {page.title()}\n\n"
                    f"Visible page text (first 4000 chars):\n{text}"
                ),
            },
        ]
    except Exception as e:
        return [{"type": "text", "text": f"Screenshot failed: {e}"}]


# --------------------------------------------------------------------------- #
# Interaction                                                                  #
# --------------------------------------------------------------------------- #

def browser_click(target: str) -> str:
    """
    Click an element. `target` can be:
    - A CSS selector:  "#submit-btn", ".assignment-title"
    - Text to find:    "text=Chapter 5 Quiz"
    - Coordinates:     "x=640,y=450"
    """
    page = _get_page()
    try:
        if target.startswith("x=") and "y=" in target:
            # coordinate click
            parts = dict(p.split("=") for p in target.split(","))
            page.mouse.click(float(parts["x"]), float(parts["y"]))
            time.sleep(0.3)
            return f"Clicked at ({parts['x']}, {parts['y']})"

        if target.startswith("text="):
            locator = page.get_by_text(target[5:], exact=False).first
        else:
            locator = page.locator(target).first

        locator.click(timeout=8_000)
        time.sleep(0.5)
        return f"Clicked: {target}"
    except Exception:
        return f"Couldn't find anything to click for '{target}' — the element may not exist or the page hasn't loaded yet."


def browser_type(selector: str, text: str, clear_first: bool = True) -> str:
    """Type text into an input field identified by CSS selector or 'text=...'."""
    page = _get_page()
    try:
        if selector.startswith("text="):
            locator = page.get_by_text(selector[5:], exact=False).first
        else:
            locator = page.locator(selector).first

        if clear_first:
            locator.fill(text, timeout=8_000)
        else:
            locator.type(text, timeout=8_000)

        return f"Typed into '{selector}'"
    except Exception:
        return f"Couldn't type into '{selector}' — the field may not exist or isn't editable."


def browser_select_option(selector: str, value: str) -> str:
    """Select a dropdown option by value or label."""
    page = _get_page()
    try:
        page.locator(selector).first.select_option(value, timeout=8_000)
        return f"Selected '{value}' in '{selector}'"
    except Exception:
        return f"Couldn't select that option — the dropdown may not exist or '{value}' isn't available."


def browser_scroll(direction: str = "down", amount: int = 300) -> str:
    """Scroll the page up or down by `amount` pixels."""
    page = _get_page()
    dy = amount if direction == "down" else -amount
    page.mouse.wheel(0, dy)
    time.sleep(0.2)
    return f"Scrolled {direction} {amount}px"


def browser_wait(seconds: float = 1.5) -> str:
    """Wait for a number of seconds (for animations / page loads)."""
    time.sleep(min(seconds, 10))
    return f"Waited {seconds}s"


def browser_get_text() -> str:
    """Get all visible text on the current page (no screenshot)."""
    page = _get_page()
    try:
        text = page.inner_text("body")
        url = page.url
        title = page.title()
        return f"URL: {url}\nTitle: {title}\n\nPage content:\n{text[:8000]}"
    except Exception as e:
        return f"Could not get page text: {e}"


def browser_press_key(key: str) -> str:
    """Press a keyboard key (e.g. 'Enter', 'Tab', 'Escape')."""
    page = _get_page()
    try:
        page.keyboard.press(key)
        time.sleep(0.2)
        return f"Pressed key: {key}"
    except Exception:
        return f"Couldn't press '{key}' — make sure the browser is open and focused."


def browser_go_back() -> str:
    # Wrapped like the other browser ops — a go_back timeout (or no history)
    # raises, and an unguarded raise would propagate out of the tool.
    try:
        page = _get_page()
        page.go_back(wait_until="networkidle", timeout=10_000)
        return f"Navigated back to: {page.url}"
    except Exception:
        return "Couldn't go back — there may be no page to go back to."


def browser_get_url() -> str:
    return _get_page().url if _page else "No browser open"
