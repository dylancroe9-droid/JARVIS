#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Alex — start script (the homework product)
# Usage: ./start-alex.sh
#
# This is JARVIS's start.sh with the product flag flipped to Alex. Same engine,
# same server — the JARVIS_PRODUCT=alex flag is what selects Alex's persona,
# tool whitelist, and branding. See ALEX.md.
# ─────────────────────────────────────────────────────────────────────────────

export JARVIS_PRODUCT=alex
JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$JARVIS_DIR/start.sh"
