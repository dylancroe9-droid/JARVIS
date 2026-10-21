"""
Practical macOS-native tools — no API keys, built on utilities already on the Mac.

  read_document  — pull text out of a PDF / Word / RTF / HTML / text file (to summarize)
  convert_image  — change an image's format via sips
  facetime_call  — start a FaceTime call to a contact, number, or email
  get_directions — open Apple Maps with driving directions
  empty_trash    — empty the Trash
"""
from __future__ import annotations

import os
import subprocess
import urllib.parse


def _osa(script: str, timeout: int = 12):
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        return (r.stdout.strip(), r.returncode)
    except Exception as e:
        return (str(e), 1)


def read_document(path: str) -> str:
    """Extract text from a PDF, Word doc, RTF, HTML, or text file so JARVIS can summarize it."""
    p = os.path.expanduser((path or "").strip())
    if not p or not os.path.exists(p):
        return f"Can't find a file at {path!r}, sir."
    ext = os.path.splitext(p)[1].lower()
    name = os.path.basename(p)
    try:
        if ext == ".pdf":
            from Foundation import NSURL
            import Quartz
            doc = Quartz.PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(p))
            if not doc:
                return f"Couldn't open the PDF {name}, sir."
            text = (doc.string() or "").strip()
            if not text:
                return f"{name} has no selectable text — it's probably scanned images, sir."
            return f"[{name} — {doc.pageCount()} pages]\n\n{text[:8000]}"
        if ext in (".doc", ".docx", ".rtf", ".rtfd", ".html", ".htm", ".odt", ".webarchive", ".wordml"):
            r = subprocess.run(["textutil", "-convert", "txt", "-stdout", p],
                               capture_output=True, text=True, timeout=15)
            t = (r.stdout or "").strip()
            return f"[{name}]\n\n{t[:8000]}" if t else f"Couldn't read {name}, sir."
        with open(p, "r", errors="replace") as f:
            return f"[{name}]\n\n{f.read()[:8000]}"
    except Exception as e:
        return f"Couldn't read {name}: {type(e).__name__}, sir."


def convert_image(path: str, to_format: str = "png") -> str:
    """Convert an image to another format (png, jpeg, heic, gif, tiff, bmp, pdf) via sips."""
    p = os.path.expanduser((path or "").strip())
    if not p or not os.path.exists(p):
        return f"Can't find an image at {path!r}, sir."
    fmt = (to_format or "png").lower().lstrip(".")
    fmt = {"jpg": "jpeg"}.get(fmt, fmt)
    if fmt not in ("png", "jpeg", "heic", "gif", "tiff", "bmp", "pdf"):
        return f"Can't convert to {to_format}, sir — try png, jpeg, heic, gif, tiff, or pdf."
    out = os.path.splitext(p)[0] + "." + ("jpg" if fmt == "jpeg" else fmt)
    try:
        r = subprocess.run(["sips", "-s", "format", fmt, p, "--out", out],
                           capture_output=True, text=True, timeout=25)
        if r.returncode == 0 and os.path.exists(out):
            return f"Converted to {os.path.basename(out)}, sir."
        return f"Conversion failed, sir: {r.stderr.strip()[:80]}"
    except Exception as e:
        return f"Conversion failed: {type(e).__name__}, sir."


def facetime_call(target: str) -> str:
    """Start a FaceTime call to a contact name, phone number, or email."""
    t = (target or "").strip()
    if not t:
        return "Who should I call, sir?"
    handle = t
    if not any(c.isdigit() for c in t) and "@" not in t:
        out, rc = _osa(
            'tell application "Contacts"\n'
            f'  set m to (every person whose name contains "{t}")\n'
            '  if m is {} then return ""\n'
            '  set p to item 1 of m\n'
            '  if (count of phones of p) > 0 then return value of first phone of p\n'
            '  if (count of emails of p) > 0 then return value of first email of p\n'
            '  return ""\n'
            'end tell')
        if rc == 0 and out:
            handle = out
        else:
            return f"Couldn't find {t} in your contacts, sir — give me a number?"
    try:
        subprocess.Popen(["open", f"facetime://{urllib.parse.quote(handle)}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return "Couldn't start FaceTime, sir."
    return f"Calling {t} on FaceTime, sir."


def get_directions(destination: str, origin: str = "") -> str:
    """Open Apple Maps with driving directions to a destination."""
    d = (destination or "").strip()
    if not d:
        return "Where to, sir?"
    url = f"https://maps.apple.com/?daddr={urllib.parse.quote(d)}&dirflg=d"
    if (origin or "").strip():
        url += f"&saddr={urllib.parse.quote(origin.strip())}"
    try:
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return "Couldn't open Maps, sir."
    return f"Pulling up directions to {d}, sir."


def empty_trash() -> str:
    """Empty the macOS Trash."""
    _out, rc = _osa('tell application "Finder" to empty trash')
    return "Trash emptied, sir." if rc == 0 else "Couldn't empty the Trash, sir."
