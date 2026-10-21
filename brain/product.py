"""
Product profile — one codebase, two shippable apps.

JARVIS and Alex are the SAME engine with a different face. A product flag decides
the name, the personality, and how much of the toolset is exposed:

  jarvis (default) — Dylan's full always-on assistant. Every mode, every tool.
  alex             — the homework assistant that gets sold. Study-only: it reads
                     the screen for problems and builds practice tests, and it
                     does NOT drive music, movies, the smart home, or ad-skipping.

This is deliberately ONE codebase rather than a fork. Every bug fixed tonight —
the mic gate, the click path, the ad-skip window probe — would otherwise have to
be fixed twice, forever. Two .app bundles are built from this same source by
setting JARVIS_PRODUCT at package time; "merge them back into one app later" is
then just deleting the flag.

Set the active product with the JARVIS_PRODUCT environment variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Product:
    key: str
    name: str                    # what it calls itself
    tagline: str                 # one line, for the window / store listing
    study_only: bool             # True → locked to schoolwork, no ambient assistant
    accent: str                  # brand colour (hex) for the app shell
    # Tools available AT ALL. Empty tuple means "no restriction — the full set".
    # For a study-only product this is the whitelist; everything else is hidden.
    allowed_tools: tuple = field(default_factory=tuple)


# The homework product is a tight whitelist, not "JARVIS minus a few things".
# Selling a narrow tool means a student never trips over movie concierge or smart
# home; it also means the schoolwork build can't be pointed at anything sketchy.
_ALEX_TOOLS = (
    # schoolwork core
    "take_screenshot", "create_practice_test", "show_test_answers",
    "read_chrome_tab", "read_document",
    # light general helpers a student legitimately wants
    "web_search", "get_wikipedia_summary", "define_word", "calculate",
    "convert_units", "translate_text",
    # self-management
    "set_mode",
)

_PRODUCTS = {
    "jarvis": Product(
        key="jarvis",
        name="JARVIS",
        tagline="Dylan's assistant.",
        study_only=False,
        accent="#00b4d8",
        allowed_tools=(),          # full toolset
    ),
    "alex": Product(
        key="alex",
        name="Alex",
        tagline="Your study partner.",
        study_only=True,
        accent="#5b8dee",
        allowed_tools=_ALEX_TOOLS,
    ),
}


def _active_key() -> str:
    key = (os.getenv("JARVIS_PRODUCT") or "jarvis").strip().lower()
    return key if key in _PRODUCTS else "jarvis"


def current() -> Product:
    """The product this process is running as."""
    return _PRODUCTS[_active_key()]


def is_alex() -> bool:
    return current().key == "alex"


def tool_is_allowed(name: str) -> bool:
    """
    True if `name` may be exposed to the model for the active product.

    A study-only product shows only its whitelist plus the always-loaded search
    tool; the full assistant allows everything. The tool_search server tool is
    never filtered — it is how the model reaches anything deferred.
    """
    p = current()
    if not p.allowed_tools:
        return True
    if name.startswith("tool_search_tool"):
        return True
    return name in p.allowed_tools
