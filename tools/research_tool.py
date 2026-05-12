"""
Research tool — opens study resources in Google Chrome.

Opens a smart mix of YouTube videos, Khan Academy, and Google searches
for any subject. JARVIS calls research_topic() with the subject and any
specific areas to focus on.
"""

from __future__ import annotations

import subprocess
import time
import urllib.parse
from typing import Optional


# ── Curated resource map for common subjects ──────────────────────────────────
# Maps subject keywords → list of (label, URL) tuples for high-quality sources.
# YouTube channels known to be great for each subject.

CURATED = {
    "ap gov": [
        ("AP Gov — Adam Norris playlist",
         "https://www.youtube.com/results?search_query=adam+norris+AP+Government"),
        ("AP Gov — Heimler's History",
         "https://www.youtube.com/results?search_query=heimler+AP+Government"),
        ("Khan Academy AP Government",
         "https://www.khanacademy.org/humanities/ap-government-and-politics"),
        ("AP Gov crash course",
         "https://www.youtube.com/results?search_query=AP+Government+crash+course"),
        ("AP Gov study guide",
         "https://www.google.com/search?q=AP+Government+2024+study+guide+key+concepts"),
    ],
    "ap history": [
        ("APUSH — Heimler's History",
         "https://www.youtube.com/results?search_query=heimler+APUSH+review"),
        ("APUSH Crash Course",
         "https://www.youtube.com/results?search_query=APUSH+crash+course"),
        ("Khan Academy APUSH",
         "https://www.khanacademy.org/humanities/us-history"),
        ("APUSH study guide",
         "https://www.google.com/search?q=APUSH+2024+study+guide"),
    ],
    "ap bio": [
        ("AP Bio — Bozeman Science",
         "https://www.youtube.com/results?search_query=bozeman+science+AP+Biology"),
        ("AP Bio Crash Course",
         "https://www.youtube.com/results?search_query=AP+Biology+crash+course"),
        ("Khan Academy AP Biology",
         "https://www.khanacademy.org/science/ap-biology"),
        ("AP Bio study guide",
         "https://www.google.com/search?q=AP+Biology+2024+review+guide"),
    ],
    "ap chem": [
        ("AP Chemistry — Tyler DeWitt",
         "https://www.youtube.com/results?search_query=tyler+dewitt+AP+Chemistry"),
        ("AP Chem — Chad's Chemistry",
         "https://www.youtube.com/results?search_query=AP+Chemistry+review+chad"),
        ("Khan Academy AP Chemistry",
         "https://www.khanacademy.org/science/ap-chemistry-beta"),
        ("AP Chem study guide",
         "https://www.google.com/search?q=AP+Chemistry+2024+study+guide"),
    ],
    "ap calc": [
        ("AP Calc — Professor Leonard",
         "https://www.youtube.com/results?search_query=professor+leonard+calculus"),
        ("AP Calc — The Organic Chemistry Tutor",
         "https://www.youtube.com/results?search_query=AP+Calculus+review+organic+chemistry+tutor"),
        ("Khan Academy AP Calculus",
         "https://www.khanacademy.org/math/ap-calculus-ab"),
        ("AP Calc study guide",
         "https://www.google.com/search?q=AP+Calculus+2024+review+key+topics"),
    ],
    "ap english": [
        ("AP Lang essays",
         "https://www.youtube.com/results?search_query=AP+English+Language+essay+tips"),
        ("AP Lit analysis",
         "https://www.youtube.com/results?search_query=AP+Literature+analysis+guide"),
        ("Khan Academy AP English",
         "https://www.khanacademy.org/humanities/reading-and-language-arts"),
        ("AP English study guide",
         "https://www.google.com/search?q=AP+English+Language+2024+study+guide"),
    ],
    "ap physics": [
        ("AP Physics — Flipping Physics",
         "https://www.youtube.com/results?search_query=flipping+physics+AP+Physics"),
        ("AP Physics crash course",
         "https://www.youtube.com/results?search_query=AP+Physics+1+crash+course"),
        ("Khan Academy AP Physics",
         "https://www.khanacademy.org/science/ap-physics-1"),
        ("AP Physics study guide",
         "https://www.google.com/search?q=AP+Physics+2024+review+guide"),
    ],
    "ap stats": [
        ("AP Stats — Josh Starmer",
         "https://www.youtube.com/results?search_query=AP+Statistics+review+Josh+Starmer"),
        ("AP Stats crash course",
         "https://www.youtube.com/results?search_query=AP+Statistics+crash+course"),
        ("Khan Academy AP Statistics",
         "https://www.khanacademy.org/math/ap-statistics"),
        ("AP Stats study guide",
         "https://www.google.com/search?q=AP+Statistics+2024+study+guide"),
    ],
    "ap macro": [
        ("AP Macro — Jacob Clifford",
         "https://www.youtube.com/results?search_query=jacob+clifford+AP+Macroeconomics"),
        ("AP Macro crash course",
         "https://www.youtube.com/results?search_query=AP+Macroeconomics+review"),
        ("Khan Academy Macroeconomics",
         "https://www.khanacademy.org/economics-finance-domain/ap-macroeconomics"),
        ("AP Macro study guide",
         "https://www.google.com/search?q=AP+Macroeconomics+2024+study+guide"),
    ],
}


def _match_subject(subject: str) -> Optional[list]:
    """Find curated resources for a subject using fuzzy keyword matching."""
    sl = subject.lower()
    for key, resources in CURATED.items():
        keywords = key.split()
        if any(kw in sl for kw in keywords):
            return resources
    return None


def open_chrome_tabs(urls: list[str]) -> str:
    """
    Open a list of URLs as tabs in Google Chrome.
    Uses 'open' command — no AppleScript, no Automation permissions needed.
    """
    if not urls:
        return "No URLs provided."

    import os

    # Find Chrome — check common locations
    chrome_apps = [
        "Google Chrome",
        "Google Chrome Canary",
        "Chromium",
    ]
    chrome_name = None
    for name in chrome_apps:
        paths = [
            f"/Applications/{name}.app",
            os.path.expanduser(f"~/Applications/{name}.app"),
        ]
        if any(os.path.exists(p) for p in paths):
            chrome_name = name
            break

    if not chrome_name:
        # Chrome not found — open in default browser
        failed = 0
        for url in urls:
            try:
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                failed += 1
        if failed:
            return f"Chrome not found — opened {len(urls) - failed} of {len(urls)} tabs in default browser ({failed} failed)."
        return f"Chrome not found — opened {len(urls)} tabs in default browser."

    # Open all URLs at once; macOS opens each as a new tab in the existing window
    try:
        subprocess.Popen(
            ["open", "-a", chrome_name] + urls,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        # Chrome open failed — fall back to default browser one by one
        for url in urls:
            try:
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        return f"Chrome failed ({exc}) — opened {len(urls)} tabs in default browser instead."

    return f"Opened {len(urls)} tabs in Chrome."


def research_topic(
    subject: str,
    subtopics: Optional[list[str]] = None,
    include_videos: bool = True,
    include_guides: bool = True,
) -> str:
    """
    Open study resources for `subject` in Chrome.
    Uses curated sources for known AP/school subjects,
    falls back to YouTube + Khan Academy searches for anything else.
    """
    urls: list[str] = []
    labels: list[str] = []

    # Always open Google Classroom first so you can grab any class-specific materials
    urls.append("https://classroom.google.com")
    labels.append("Google Classroom")

    # Try curated list first
    curated = _match_subject(subject)
    if curated:
        for label, url in curated:
            labels.append(label)
            urls.append(url)
    else:
        # Generic fallback
        q = urllib.parse.quote(subject)
        if include_videos:
            urls.append(
                f"https://www.youtube.com/results?search_query={q}+explained+study+guide"
            )
            urls.append(
                f"https://www.youtube.com/results?search_query={q}+crash+course"
            )
        if include_guides:
            urls.append(
                f"https://www.khanacademy.org/search?search_term={urllib.parse.quote(subject)}"
            )
            urls.append(
                f"https://www.google.com/search?q={q}+study+guide+notes+2024"
            )

    # Add any extra subtopic searches
    if subtopics:
        for st in subtopics[:3]:
            q2 = urllib.parse.quote(f"{subject} {st}")
            urls.append(
                f"https://www.youtube.com/results?search_query={q2}+explained"
            )

    open_chrome_tabs(urls)
    return (
        f"Opened {len(urls)} tabs for '{subject}': Google Classroom, "
        f"YouTube, Khan Academy, and study guides."
    )
