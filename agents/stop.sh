#!/usr/bin/env bash
# Stop the dashboard and tear down the tmux swarm session.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

tmux kill-session -t jarvis-swarm 2>/dev/null && echo "killed tmux session" || echo "no tmux session"

if [[ -f "$ROOT/.run/dashboard.pid" ]]; then
  PID="$(cat "$ROOT/.run/dashboard.pid")"
  if kill "$PID" 2>/dev/null; then echo "stopped dashboard pid=$PID"; fi
  rm -f "$ROOT/.run/dashboard.pid"
fi
