#!/bin/bash
# ── Launch JARVIS (Electron UI + Python server) ───────────────────────────────

JARVIS_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$JARVIS_DIR"

if [ ! -d ".venv" ]; then
  echo "Virtual environment not found. Run bash install.sh first."
  exit 1
fi

if [ ! -d "jarvis-app/node_modules" ]; then
  echo "Node modules not found. Running npm install..."
  cd jarvis-app && npm install && cd ..
fi

source .venv/bin/activate

# Kill any leftover processes from a previous run
pkill -f "python.*server.py" 2>/dev/null
pkill -f "Electron.*JARVIS"  2>/dev/null
sleep 0.5

echo "Starting JARVIS server..."
python server.py &
SERVER_PID=$!

# Wait for server to be ready (polls port 8765)
echo "Waiting for server..."
for i in $(seq 1 20); do
  if python -c "import socket; s=socket.socket(); s.connect(('127.0.0.1',8765)); s.close()" 2>/dev/null; then
    echo "Server ready."
    break
  fi
  sleep 0.5
done

echo "Starting Electron..."
cd jarvis-app
npm start

# When Electron exits, kill the server
kill $SERVER_PID 2>/dev/null
