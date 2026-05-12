#!/usr/bin/env bash
# Start the dashboard (background) and the tmux agent swarm (foreground).
#
# Usage:
#   ./start.sh        # 4 agents
#   ./start.sh 6      # 6 agents
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JARVIS="$(cd "$ROOT/.." && pwd)"
N="${1:-4}"
PORT="${PORT:-8765}"

# Kill any previous dashboard on this port
if lsof -ti :"$PORT" >/dev/null 2>&1; then
  lsof -ti :"$PORT" | xargs kill -9 2>/dev/null || true
fi

# Start dashboard in background, log to file
mkdir -p "$ROOT/.run"
LOG="$ROOT/.run/dashboard.log"
( cd "$JARVIS" && PORT="$PORT" nohup .venv/bin/python agents/dashboard/server.py >"$LOG" 2>&1 & echo $! > "$ROOT/.run/dashboard.pid" )

sleep 1
echo "Dashboard:  http://localhost:$PORT  (log: $LOG)"

# Open the dashboard in the default browser on first launch
if command -v open >/dev/null; then
  open "http://localhost:$PORT" || true
fi

# Hand off to the swarm launcher (this is what attaches the tmux session)
exec "$ROOT/launch.sh" "$N"
