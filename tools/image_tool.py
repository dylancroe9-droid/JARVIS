"""
Image generation — uses Pollinations.ai (free, no API key needed).
Falls back to opening the image in Chrome for viewing.
"""
from __future__ import annotations
import urllib.parse
import subprocess
import os


def generate_image(prompt: str, open_browser: bool = True) -> str:
    """
    Generate an image from a text prompt using Pollinations.ai (free, no key).
    Downloads the image to ~/Downloads and opens it.
    """
    import urllib.request
    import time

    # Build URL — Pollinations generates on the fly
    encoded = urllib.parse.quote(prompt)
    # Add seed for variety
    seed = int(time.time()) % 99999
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&seed={seed}&nologo=true"

    try:
        # Download the image
        downloads = os.path.expanduser("~/Downloads")
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in prompt[:40]).strip()
        filepath   = os.path.join(downloads, f"JARVIS_{safe_name}_{seed}.jpg")

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()

        with open(filepath, "wb") as f:
            f.write(data)

        # Open it
        subprocess.Popen(["open", filepath], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Image generated and opened: {safe_name}"

    except Exception as exc:
        # Fallback: open URL directly in browser
        if open_browser:
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Opening generated image in browser for: {prompt}"
        return f"Image generation failed: {exc}"
