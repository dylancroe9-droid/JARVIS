#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Build Alex.app — the homework product, from the shared codebase.
#
# Same Electron shell and Python engine as JARVIS; the JARVIS_PRODUCT=alex flag
# (baked in via the product.build marker) is what makes it Alex. Unsigned build
# for local testing / cash-at-school distribution — see distribution notes for
# how a student opens an unsigned app. Signing/notarization needs an 18+ Apple
# Developer account (out of scope for now).
# ─────────────────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

# Marker so the packaged app self-identifies as Alex (no env var at launch).
echo alex > product.build
cleanup () { rm -f product.build; }
trap cleanup EXIT

echo "🧹  Cleaning previous build…"
rm -rf dist

echo "🔵  Building Alex.app (unsigned, arm64)…"
# CSC_IDENTITY_AUTO_DISCOVERY=false → do not try to sign with a Developer ID.
CSC_IDENTITY_AUTO_DISCOVERY=false \
  npx electron-builder --mac --arm64 \
  --config electron-builder-alex.json \
  --publish never

echo "✅  Done. Output in dist/:"
ls -1 dist/*.dmg 2>/dev/null || true
