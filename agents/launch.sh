#!/usr/bin/env bash
# JARVIS Agent Swarm launcher.
# Reads agents/roles.json and spawns one specialized `claude` per role
# in its own tmux pane. Each pane runs in workspaces/<role-id>/ with a
# generated CLAUDE.md that scopes the agent to its specialty.
#
# Usage:
#   ./launch.sh           # spawn all roles from roles.json
#   ./launch.sh attach    # reattach to running session
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="jarvis-swarm"
WORKSPACES="$ROOT/workspaces"
TASKS="$ROOT/tasks"
ROLES="$ROOT/roles.json"

export PATH="$HOME/.npm-global/bin:$PATH"

if [[ "${1:-}" == "attach" ]]; then
  exec tmux attach -t "$SESSION"
fi

command -v tmux >/dev/null || { echo "tmux not found"; exit 1; }
command -v claude >/dev/null || { echo "claude not found on PATH"; exit 1; }
command -v jq >/dev/null || command -v python3 >/dev/null || { echo "need jq or python3"; exit 1; }
[[ -f "$ROLES" ]] || { echo "missing $ROLES"; exit 1; }

# Read role IDs into an array (works on bash 3.2)
ROLE_IDS=()
while IFS= read -r line; do
  [[ -n "$line" ]] && ROLE_IDS+=("$line")
done < <(python3 -c 'import json,sys;print("\n".join(r["id"] for r in json.load(open(sys.argv[1]))))' "$ROLES")

if (( ${#ROLE_IDS[@]} == 0 )); then echo "no roles defined"; exit 1; fi

# Helper: extract one field for a role id
role_field() {
  local id="$1" field="$2"
  python3 - "$ROLES" "$id" "$field" <<'PY'
import json, sys
roles = json.load(open(sys.argv[1]))
rid, field = sys.argv[2], sys.argv[3]
for r in roles:
    if r["id"] == rid:
        print(r.get(field, ""))
        break
PY
}

# Where each agent runs — by default, the JARVIS repo root so they can edit
# real project files (not an isolated workspace). Override per-agent later if
# you want sandboxing.
JARVIS_ROOT="$(cd "$ROOT/.." && pwd)"

# Per-agent CLAUDE.md goes into agents/configs/<id>.md so it doesn't pollute
# the JARVIS repo's own CLAUDE.md if one exists.
CONFIGS="$ROOT/configs"
mkdir -p "$CONFIGS" "$WORKSPACES"

for id in "${ROLE_IDS[@]}"; do
  title="$(role_field "$id" title)"
  emoji="$(role_field "$id" emoji)"
  specialty="$(role_field "$id" specialty)"
  system="$(role_field "$id" system)"

  cat > "$CONFIGS/$id.md" <<EOF
# Role: $emoji $title

$system

## Specialty
$specialty

## Where you work
Your working directory is the JARVIS repo at \`$JARVIS_ROOT\`. You can read
and edit any file in that tree directly. Be careful with destructive
operations (rm, force-push, schema drops) — confirm with the user first.

## Receiving tasks
Tasks arrive as prompts in this terminal — typed by the user or sent from
the JARVIS dashboard at http://localhost:8765. Acknowledge in one line,
then do the work.

## Coordinating with siblings
There are sibling agents working on other parts of JARVIS in parallel. If
your change touches a file outside your specialty, mention it briefly so
the user can coordinate.
EOF

  if [[ ! -f "$TASKS/$id.md" ]]; then
    cat > "$TASKS/$id.md" <<EOF
# $emoji $title — current task

(empty — send a task from the dashboard at http://localhost:8765
 or edit this file before launching)
EOF
  fi
done

# Tear down any existing session so we start clean
tmux kill-session -t "$SESSION" 2>/dev/null || true

# Write a small runner script per agent to avoid quote-escaping hell.
# The runner is what tmux actually executes in each pane.
RUN="$ROOT/.run"
mkdir -p "$RUN"

for id in "${ROLE_IDS[@]}"; do
  ws="$WORKSPACES/$id"
  cfg="$CONFIGS/$id.md"
  task="$TASKS/$id.md"
  runner="$RUN/$id.sh"
  cat > "$runner" <<EOF
#!/usr/bin/env bash
export PATH="\$HOME/.npm-global/bin:\$PATH"
clear
echo '=== $id ==='
echo
cat '$task'
echo
echo '--- starting claude (role: $id, workspace: $ws, +access: $JARVIS_ROOT) ---'
cd '$ws'
exec claude --add-dir '$JARVIS_ROOT' --append-system-prompt "\$(cat '$cfg')"
EOF
  chmod +x "$runner"
done

first="${ROLE_IDS[0]}"
tmux new-session -d -s "$SESSION" -n agents -c "$WORKSPACES/$first" \
  "$RUN/$first.sh"
tmux select-pane -t "$SESSION:agents.0" -T "$first"

idx=1
for id in "${ROLE_IDS[@]:1}"; do
  tmux split-window -t "$SESSION:agents" -c "$WORKSPACES/$id" \
    "$RUN/$id.sh"
  tmux select-layout -t "$SESSION:agents" tiled >/dev/null
  tmux select-pane -t "$SESSION:agents.$idx" -T "$id"
  idx=$((idx+1))
done

tmux select-layout -t "$SESSION:agents" tiled >/dev/null
tmux set-option -t "$SESSION" mouse on >/dev/null
tmux set-option -t "$SESSION" pane-border-status top >/dev/null
tmux set-option -t "$SESSION" pane-border-format ' #T ' >/dev/null

echo "Spawned ${#ROLE_IDS[@]} agents in tmux session '$SESSION'."
echo "Roles: ${ROLE_IDS[*]}"
echo "Attach: tmux attach -t $SESSION   |   Stop: ./stop.sh"

if [[ "${NO_ATTACH:-}" != "1" ]]; then
  exec tmux attach -t "$SESSION"
fi
