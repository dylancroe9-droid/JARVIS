"""
Autonomous homework answering loop.

JARVIS watches the screen continuously, reads each question, determines the
correct answer, clicks it, and moves to the next one — until told to stop.

Flow per iteration:
  1. screencapture → raw PNG → base64
  2. Claude/Groq Vision: "What's the question? What's the answer? Where to click?"
  3. click_screen(x, y) on the correct option
  4. brief pause → scroll down slightly → repeat
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
from typing import Callable, Optional

# ── Global state ──────────────────────────────────────────────────────────────

_loop_thread:    Optional[threading.Thread] = None
_loop_active:    bool = False
_loop_lock:      threading.Lock = threading.Lock()
_broadcast_fn:   Optional[Callable] = None   # set by server.py


SCREENSHOT_PATH = "/tmp/jarvis_hw_loop.jpg"

_VISION_PROMPT = """\
You are helping autonomously answer multiple-choice homework questions.

Look at this screenshot and find the FIRST unanswered multiple-choice question visible.

Respond with ONLY a JSON object — no explanation, no markdown:

If you see an unanswered question:
{"question": "short question text (max 80 chars)", "answer": "letter and text of correct answer", "x": 847, "y": 432}
x,y = pixel coordinates of the radio button or checkbox for the CORRECT answer.
Click the bubble/dot itself, not the label text. Be very precise.

If all visible questions are already answered and there is a Next/Continue button:
{"action": "next_page", "x": 600, "y": 780}

If all visible questions are already answered and there is a Submit button:
{"action": "submit", "x": 600, "y": 780}

If all questions are answered and no next/submit button is visible:
{"action": "done"}

If the screen doesn't show a quiz or homework form at all:
{"action": "no_questions"}
"""


# ── Public API ────────────────────────────────────────────────────────────────

def start_loop(broadcast_fn: Callable) -> str:
    global _loop_active, _loop_thread, _broadcast_fn
    with _loop_lock:
        if _loop_active:
            return "Auto-answer loop is already running. Say 'stop' to end it."
        _loop_active = True
        _broadcast_fn = broadcast_fn
        _loop_thread = threading.Thread(
            target=_run_loop,
            daemon=True,
            name="hw-auto-answer",
        )
        _loop_thread.start()
    return "Auto-answer mode active. I'll work through every question until you say stop."


def stop_loop() -> str:
    global _loop_active
    _loop_active = False
    return "Auto-answer stopped."


def is_running() -> bool:
    return _loop_active


# ── Core loop ─────────────────────────────────────────────────────────────────

def _run_loop() -> None:
    global _loop_active
    q_num = 0
    consecutive_failures = 0

    _send_status("running", question=0, text="Starting — scanning screen…")

    while _loop_active:
        try:
            # ── 1. Screenshot ────────────────────────────────────────────────
            b64 = _capture_screen()
            if not b64:
                consecutive_failures += 1
                if consecutive_failures >= 4:
                    _send_status("error", text="Screen capture failed 4 times — stopping.")
                    break
                time.sleep(1.5)
                continue

            # ── 2. Ask vision model what to click ───────────────────────────
            instruction = _ask_vision(b64)
            if not instruction:
                consecutive_failures += 1
                if consecutive_failures >= 4:
                    _send_status("error", text="Vision model failed 4 times — stopping.")
                    break
                time.sleep(1.5)
                continue

            consecutive_failures = 0

            # ── 3. Act on the instruction ────────────────────────────────────
            action = instruction.get("action")

            if action == "done":
                _send_status("done", question=q_num,
                             text=f"Finished — answered {q_num} question(s).")
                break

            if action == "no_questions":
                _send_status("waiting", text="No quiz visible — waiting…")
                time.sleep(3)
                continue

            if action in ("next_page", "submit"):
                label = "Next page" if action == "next_page" else "Submit"
                _send_status("running", question=q_num,
                             text=f"{label} — clicking button…")
                _click(instruction.get("x"), instruction.get("y"))
                time.sleep(2.0)   # wait for page transition
                continue

            # Normal answer
            q_num += 1
            q_text  = instruction.get("question", "")
            a_text  = instruction.get("answer",   "")
            x       = instruction.get("x")
            y       = instruction.get("y")

            _send_status("answering", question=q_num, text=q_text, answer=a_text)

            if x is not None and y is not None:
                _click(x, y)
                time.sleep(0.6)   # let the UI register the click
            else:
                # No coordinates — scroll down and hope the next question appears
                _scroll_down()
                time.sleep(0.8)
                continue

            # Small pause between questions so the page doesn't miss rapid clicks
            time.sleep(1.0)

            # Scroll down a little to reveal the next question if needed
            _scroll_down(amount=2)
            time.sleep(0.5)

        except Exception as exc:
            _send_status("error", text=f"Loop error: {exc}")
            time.sleep(2)

    _loop_active = False
    _send_status("stopped", text="Auto-answer loop ended.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _capture_screen() -> Optional[str]:
    """Capture full screen → JPEG base64 string."""
    try:
        # screencapture -x = silent (no shutter), -t jpeg for smaller file
        r = subprocess.run(
            ["screencapture", "-x", "-t", "jpg", SCREENSHOT_PATH],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        size = os.path.getsize(SCREENSHOT_PATH)
        if size < 10_000:   # blank/denied screenshot
            return None
        with open(SCREENSHOT_PATH, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return None


def _ask_vision(b64: str) -> Optional[dict]:
    """Send screenshot to vision model, get back a structured instruction dict."""
    try:
        from config import ANTHROPIC_API_KEY, GROQ_API_KEY

        raw = None

        # ── Claude (best) ────────────────────────────────────────────────────
        if ANTHROPIC_API_KEY.startswith("sk-ant-"):
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                resp = client.messages.create(
                    model="claude-opus-4-5",
                    max_tokens=300,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type":   "image",
                                "source": {
                                    "type":       "base64",
                                    "media_type": "image/jpeg",
                                    "data":        b64,
                                },
                            },
                            {"type": "text", "text": _VISION_PROMPT},
                        ],
                    }],
                )
                raw = resp.content[0].text.strip()
            except Exception as exc:
                print(f"[hw-loop] Claude vision error: {exc}")

        # ── Groq vision fallback ─────────────────────────────────────────────
        if raw is None and GROQ_API_KEY:
            try:
                from openai import OpenAI
                client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
                resp = client.chat.completions.create(
                    model="meta-llama/llama-4-scout-17b-16e-instruct",
                    max_tokens=300,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type":      "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                            },
                            {"type": "text", "text": _VISION_PROMPT},
                        ],
                    }],
                )
                raw = (resp.choices[0].message.content or "").strip()
            except Exception as exc:
                print(f"[hw-loop] Groq vision error: {exc}")

        if not raw:
            return None

        # Extract JSON from response (model sometimes wraps it in markdown)
        m = re.search(r'\{.*?\}', raw, re.DOTALL)
        if not m:
            return None
        return json.loads(m.group())

    except Exception as exc:
        print(f"[hw-loop] _ask_vision error: {exc}")
        return None


def _click(x, y) -> None:
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.click(int(x), int(y))
    except Exception as exc:
        print(f"[hw-loop] click error: {exc}")


def _scroll_down(amount: int = 3) -> None:
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.scroll(-amount * 100)
    except Exception as exc:
        print(f"[hw-loop] scroll error: {exc}")


def _send_status(status: str, question: int = 0, text: str = "", answer: str = "") -> None:
    if _broadcast_fn:
        try:
            _broadcast_fn({
                "type":     "hw_loop_status",
                "status":   status,
                "question": question,
                "text":     text,
                "answer":   answer,
            })
        except Exception:
            pass
    print(f"[hw-loop] {status} | Q{question} | {text[:60]}")
