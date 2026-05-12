"""
Persistent memory for JARVIS — things he's learned about Dylan
that survive between sessions.
Stored as plain text in ~/JARVIS/memory.txt (one fact per line).
"""

from pathlib import Path

MEMORY_FILE = Path(__file__).parent.parent / "memory.txt"
_MAX_FACTS   = 100   # prune oldest when we exceed this


def load_memory() -> str:
    """Return all saved facts as a formatted string, or empty string if none."""
    try:
        if not MEMORY_FILE.exists():
            return ""
        text = MEMORY_FILE.read_text(encoding="utf-8", errors="ignore").strip()
        return text if text else ""
    except Exception:
        return ""


def _parse_facts() -> list[str]:
    """Load facts as a clean list (strips leading '- ')."""
    try:
        if not MEMORY_FILE.exists():
            return []
        return [
            l.strip().lstrip("- ")
            for l in MEMORY_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()
            if l.strip()
        ]
    except Exception:
        return []


def _facts_match(a: str, b: str) -> bool:
    """
    True if two facts are about the same subject and should be merged.
    Uses first-5-words match OR substring containment — catches both
    "Dylan likes X" → "Dylan likes Y" and shorter fact being a subset of longer.
    """
    a_l, b_l = a.lower(), b.lower()
    # Substring: one fact is already contained in the other
    if a_l in b_l or b_l in a_l:
        return True
    # Same opening subject (first 5 meaningful words)
    a_words = a_l.split()[:5]
    b_words = b_l.split()[:5]
    return a_words == b_words


def save_fact(fact: str) -> str:
    """
    Save a fact, replacing any near-duplicate.
    Prunes to _MAX_FACTS most recent entries to prevent unbounded growth.
    """
    fact = fact.strip().lstrip("- ")
    if not fact:
        return "Nothing to save."
    try:
        existing = _parse_facts()

        # Replace near-duplicate if found, otherwise append
        updated = False
        for i, e in enumerate(existing):
            if _facts_match(fact, e):
                existing[i] = fact
                updated = True
                break
        if not updated:
            existing.append(fact)

        # Prune oldest if over the cap
        if len(existing) > _MAX_FACTS:
            existing = existing[-_MAX_FACTS:]

        MEMORY_FILE.write_text(
            "\n".join(f"- {e}" for e in existing) + "\n",
            encoding="utf-8",
        )
        return f"Noted: {fact}"
    except OSError as exc:
        return f"Couldn't save to memory: {exc}"


def clear_memory() -> str:
    """Wipe all saved facts."""
    try:
        MEMORY_FILE.write_text("", encoding="utf-8")
        return "Memory cleared."
    except OSError as exc:
        return f"Couldn't clear memory: {exc}"
