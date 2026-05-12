"""
Screen vision — screenshot + Groq vision, plus direct Chrome tab reading.

read_chrome_tab()      — reads URL/title/text from Chrome's active tab via
                         AppleScript. Works without Screen Recording permission.
                         Supports Chrome, Arc, Brave, Safari.

take_screenshot()      — full-screen capture → Groq vision model analysis.
                         Requires Screen Recording permission for the app.
"""

from __future__ import annotations

import base64
import os
import subprocess

VISION_MODEL    = "meta-llama/llama-4-scout-17b-16e-instruct"
SCREENSHOT_PATH = "/tmp/jarvis_screen.png"

# Min file size (bytes) for a real screenshot — blank/denied screenshots
# are very small PNGs (nearly all one colour).
_MIN_SCREENSHOT_BYTES = 20_000


# ── Chrome / browser tab reading ─────────────────────────────────────────────

def read_chrome_tab(question: str = "") -> str:
    """
    Read the current tab in Chrome (or Arc/Brave/Safari) via AppleScript.
    Returns URL, title, and visible text — no Screen Recording permission needed.
    Much more reliable than screenshot + vision for understanding page content.
    """
    # Try each browser in order of preference
    # JS that works for static pages AND SPAs (React/Vue/Angular/etc.)
    # IMPORTANT: no double-quotes inside — this runs inside an AppleScript string literal.
    _JS = (
        "try{"
        "var t=(document.body||document.documentElement).innerText.trim();"
        # SPA fallback: scrape visible elements when body text is sparse
        "if(t.length<80){"
        "t=Array.from(document.querySelectorAll('h1,h2,h3,h4,p,li,td,span,a,button,label'))"
        ".map(function(e){return e.innerText||e.getAttribute('aria-label')||'';}).filter(function(x){return x.trim().length>3;})"
        ".join(' | ').substring(0,7000);"
        "}"
        # Meta description fallback for apps that block content extraction
        "if(t.length<40){"
        "var m=document.querySelector('meta[name=description]')||document.querySelector('meta[name=Description]');"
        "if(m&&m.content)t='(page description: '+m.content+')';"
        "}"
        "t.substring(0,7000);"
        "}catch(e){''}"
    )

    browsers = [
        ("Google Chrome",
         f'set tabText to execute currentTab javascript "{_JS}"'),
        ("Arc",
         f'set tabText to execute currentTab javascript "{_JS}"'),
        ("Brave Browser",
         f'set tabText to execute currentTab javascript "{_JS}"'),
        ("Safari",
         f'set tabText to do JavaScript "{_JS}" in currentTab'),
    ]

    # Batch all pgrep checks upfront — one pass, no per-iteration overhead
    _running: set[str] = set()
    for _name in ["Google Chrome", "Arc", "Brave Browser", "Safari"]:
        try:
            if subprocess.run(["pgrep", "-ix", _name], capture_output=True, timeout=2).returncode == 0:
                _running.add(_name)
        except Exception:
            pass

    for app_name, js_cmd in browsers:
        if app_name not in _running:
            continue

        if app_name == "Safari":
            script = f'''
tell application "Safari"
    if (count windows) = 0 then return "No Safari windows open"
    set currentTab to current tab of front window
    set tabURL to URL of currentTab
    set tabTitle to name of currentTab
    set tabText to ""
    try
        {js_cmd}
    end try
    return "URL: " & tabURL & "\nTitle: " & tabTitle & "\nContent:\n" & tabText
end tell'''
        else:
            script = f'''
tell application "{app_name}"
    if (count windows) = 0 then return "No {app_name} windows open"
    set currentTab to active tab of front window
    set tabURL to URL of currentTab
    set tabTitle to title of currentTab
    set tabText to ""
    try
        {js_cmd}
    end try
    return "URL: " & tabURL & "\nTitle: " & tabTitle & "\nContent:\n" & tabText
end tell'''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=7,
            )
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0 and result.stdout.strip():
            content = result.stdout.strip()
            if question:
                # Ask vision model about the content (text-only, no screenshot needed)
                from openai import OpenAI
                from config import GROQ_API_KEY
                client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
                try:
                    resp = client.chat.completions.create(
                        model=VISION_MODEL,
                        messages=[{
                            "role": "user",
                            "content": (
                                f"Here is the content of the user's current browser tab:\n\n"
                                f"{content}\n\n"
                                f"Question: {question}"
                            ),
                        }],
                        max_tokens=512,
                    )
                    return resp.choices[0].message.content or content
                except Exception:
                    pass
            return content

    return "No supported browser is open (Chrome, Arc, Brave, or Safari)."


# ── Full-screen screenshot + vision ──────────────────────────────────────────

def _logical_screen_size() -> tuple:
    try:
        out = subprocess.check_output(
            ["osascript", "-e",
             'tell application "Finder" to get bounds of window of desktop'],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        parts = [int(x.strip()) for x in out.split(",")]
        return parts[2], parts[3]
    except Exception:
        return 1440, 900


def take_screenshot(question: str = "", for_control: bool = False) -> str:
    """
    Capture the full screen and ask the vision model a question about it.
    Requires Screen Recording permission for the app.

    If Screen Recording is denied, automatically falls back to read_chrome_tab()
    for Chrome content questions.
    """
    # 1. Capture
    r = subprocess.run(
        ["screencapture", "-x", "-t", "png", SCREENSHOT_PATH],
        capture_output=True,
    )
    if r.returncode != 0:
        return (
            "Screenshot failed. JARVIS needs Screen Recording permission:\n"
            "System Settings → Privacy & Security → Screen Recording → add JARVIS."
        )

    # 2. Check if screenshot looks real (not blank/denied)
    try:
        file_size = os.path.getsize(SCREENSHOT_PATH)
    except Exception:
        file_size = 0

    if file_size < _MIN_SCREENSHOT_BYTES:
        # Screen Recording permission likely denied — fall back to Chrome reading
        os.remove(SCREENSHOT_PATH)
        q = question or "What is on screen?"
        chrome_result = read_chrome_tab(q)
        if "No supported browser" not in chrome_result:
            return f"[Screen Recording not permitted — read Chrome tab instead]\n{chrome_result}"
        return (
            "Screenshot returned a blank image — Screen Recording permission required.\n"
            "Go to: System Settings → Privacy & Security → Screen Recording → add JARVIS.\n"
            "Then restart JARVIS."
        )

    # 3. Resize
    sw, sh = _logical_screen_size()
    if for_control:
        subprocess.run(["sips", "-z", str(sh), str(sw), SCREENSHOT_PATH], capture_output=True)
        coord_note = (
            f"IMPORTANT: This image is exactly {sw}×{sh} pixels matching the "
            f"screen's logical coordinate space. When you identify UI elements, "
            f"report their CENTER as (x, y) where x is 0–{sw} and y is 0–{sh}. "
            f"These values can be passed directly to click_screen(). "
        )
    else:
        subprocess.run(["sips", "-Z", "1280", SCREENSHOT_PATH], capture_output=True)
        coord_note = ""

    # 4. Encode
    try:
        with open(SCREENSHOT_PATH, "rb") as f:
            image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    except Exception as exc:
        return f"Could not read screenshot file: {exc}"

    # 5. Ask vision model
    from openai import OpenAI
    from config import GROQ_API_KEY
    client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")

    user_q = question.strip() or "Describe everything you see on screen in detail."
    full_prompt = f"{coord_note}{user_q}" if coord_note else user_q

    try:
        response = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    {"type": "text", "text": full_prompt},
                ],
            }],
            max_tokens=1024,
        )
        return response.choices[0].message.content or "No response from vision model."
    except Exception as exc:
        # Vision failed — try reading the active browser tab as plain-text fallback
        try:
            fallback = read_chrome_tab(question)
            if "No supported browser" not in fallback:
                return f"[Vision unavailable — read Chrome tab instead]\n{fallback}"
        except Exception:
            pass
        return f"Vision model unavailable: {exc}"
    finally:
        try:
            os.remove(SCREENSHOT_PATH)
        except Exception:
            pass
