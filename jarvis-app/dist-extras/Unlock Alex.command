#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Unlock Alex — double-click this if macOS says Alex "can't be opened".
#
# Alex isn't signed by Apple yet (that needs a paid developer account), so when
# you download or AirDrop it, macOS quarantines it. This clears that flag so Alex
# opens normally. It does NOT touch anything else on your Mac.
# ─────────────────────────────────────────────────────────────────────────────

echo "Looking for Alex…"

# Check the usual spots: /Applications, ~/Downloads, ~/Desktop, and next to this file.
CANDIDATES=(
  "/Applications/Alex.app"
  "$HOME/Downloads/Alex.app"
  "$HOME/Desktop/Alex.app"
  "$(cd "$(dirname "$0")" && pwd)/Alex.app"
)

FOUND=""
for c in "${CANDIDATES[@]}"; do
  if [ -d "$c" ]; then FOUND="$c"; break; fi
done

if [ -z "$FOUND" ]; then
  echo
  echo "❌  Couldn't find Alex.app."
  echo "    Drag Alex into your Applications folder first, then run this again."
  echo
  read -n 1 -s -r -p "Press any key to close…"
  exit 1
fi

echo "Found: $FOUND"
echo "Unlocking…"
xattr -dr com.apple.quarantine "$FOUND" 2>/dev/null

echo
echo "✅  Alex is unlocked. You can open it now from Applications."
echo
read -n 1 -s -r -p "Press any key to close…"
