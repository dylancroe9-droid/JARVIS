#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# JARVIS — start script
# Usage: ./start.sh
# ─────────────────────────────────────────────────────────────────────────────

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$JARVIS_DIR"

# ── Kill anything already on port 8765 ───────────────────────────────────────
EXISTING=$(lsof -ti:8765 2>/dev/null)
if [ -n "$EXISTING" ]; then
  echo "🔄  Killing old server (PID $EXISTING)..."
  kill $EXISTING 2>/dev/null
  sleep 1
fi

# ── Sanity checks ─────────────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "❌  Virtual environment not found. Run ./setup.sh first."
  exit 1
fi

if [ ! -d "jarvis-app/node_modules" ]; then
  echo "❌  Node modules missing. Run: cd jarvis-app && npm install"
  exit 1
fi

# ── Start Python brain server in background ───────────────────────────────────
echo "🧠  Starting JARVIS brain..."
source .venv/bin/activate
# Tee server output to /tmp/jarvis_server.log so JARVIS can read his own
# runtime errors via tools/self_inspect.py (read_jarvis_log). Truncated on
# each start so the log only ever covers the current session.
JARVIS_LOG=/tmp/jarvis_server.log
: > "$JARVIS_LOG"
# Use process substitution (not a pipe) so $! is the SERVER's PID, not tee's.
# With `python ... | tee &`, $! captured tee — so the crash check watched the
# wrong process and the shutdown `kill` left the real server orphaned on 8765.
python server.py > >(tee "$JARVIS_LOG") 2>&1 &
SERVER_PID=$!

# ── Wait for server to be healthy (max 20s) ───────────────────────────────────
echo "⏳  Waiting for server..."
for i in $(seq 1 40); do
  if curl -s http://127.0.0.1:8765/health > /dev/null 2>&1; then
    echo "✅  Server ready."
    break
  fi
  if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo "❌  Server crashed on startup. Check above for errors."
    exit 1
  fi
  sleep 0.5
done

# ── Launch Electron app ───────────────────────────────────────────────────────
echo "🚀  Launching JARVIS..."
cd "$JARVIS_DIR/jarvis-app"
npm start

# ── When Electron closes, shut down the server ────────────────────────────────
echo "🛑  Shutting down brain server..."
kill $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null
echo "👋  Done."
