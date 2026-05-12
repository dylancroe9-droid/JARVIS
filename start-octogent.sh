#!/usr/bin/env bash
# Start (or restart) Octogent for JARVIS.
# Usage: ./start-octogent.sh        # start in background, open browser
#        ./start-octogent.sh stop   # stop the running instance
set -euo pipefail

LOG=/tmp/octogent.log
PORT=8787

CHAT_PORT=8766
CHAT_LOG=/tmp/chat-server.log

stop_running() {
  pkill -f "/octogent$" 2>/dev/null || true
  pkill -f "octogent/bin" 2>/dev/null || true
  pkill -f chat-server.py 2>/dev/null || true
  for p in "$PORT" "$CHAT_PORT"; do
    if lsof -ti ":$p" >/dev/null 2>&1; then
      lsof -ti ":$p" | xargs kill -9 2>/dev/null || true
    fi
  done
}

if [[ "${1:-}" == "stop" ]]; then
  stop_running
  echo "octogent + chat stopped"
  exit 0
fi

stop_running
sleep 1

cd "$(dirname "${BASH_SOURCE[0]}")"

# Bump fd limit so node-pty can spawn lots of terminals
( ulimit -n 8192; \
  PATH="$HOME/.npm-global/bin:$PATH" \
  OCTOGENT_NO_OPEN=1 \
  nohup "$HOME/.npm-global/bin/octogent" >"$LOG" 2>&1 & echo $! >/tmp/octogent.pid )
echo "started octogent pid=$(cat /tmp/octogent.pid)"

# Also start the simple chat UI on $CHAT_PORT
nohup "$HOME/JARVIS/.venv/bin/python" "$HOME/JARVIS/agents/chat-server.py" \
  >"$CHAT_LOG" 2>&1 & echo $! >/tmp/chat-server.pid
echo "started chat-server pid=$(cat /tmp/chat-server.pid)"

sleep 4

if curl -sf "http://127.0.0.1:$PORT/" >/dev/null; then
  echo "✓ Octogent dashboard:  http://127.0.0.1:$PORT"
else
  echo "✗ Octogent didn't come up. Last log lines:"
  tail -20 "$LOG"; exit 1
fi
if curl -sf "http://127.0.0.1:$CHAT_PORT/" >/dev/null; then
  echo "✓ Simple chat UI:      http://127.0.0.1:$CHAT_PORT  ← talk to agents here"
else
  echo "(chat-server didn't bind port $CHAT_PORT, see $CHAT_LOG)"
fi
command -v open >/dev/null && open "http://127.0.0.1:$CHAT_PORT"
