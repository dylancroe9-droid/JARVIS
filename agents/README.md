# JARVIS Agent Swarm

Multiple **specialized** Claude Code agents running in parallel, with a web
dashboard you can use to type tasks and dispatch them to whichever agent
should handle them.

## What's specialized

`agents/roles.json` defines each agent's identity. Defaults:

| id           | role               | specialty |
|--------------|--------------------|-----------|
| `frontend`   | 🎨 Frontend         | React, CSS, UI/UX, browser bugs |
| `backend`    | ⚙️ Backend          | Python, APIs, DB, JARVIS server.py |
| `researcher` | 🔎 Researcher       | Web research, summarizing docs |
| `devops`     | 🛠️ DevOps           | Shell, deploy, CI, env setup |

Each agent gets its own `workspaces/<id>/CLAUDE.md` with a system prompt that
locks it to its specialty. Edit `roles.json` and rerun `launch.sh` to add
roles or change specialties — the workspaces and CLAUDE.md files regenerate.

## Run it

```bash
cd ~/JARVIS/agents
./start.sh
```

That:

1. Starts the dashboard at <http://localhost:8765> (auto-opens browser).
2. Spawns the tmux session `jarvis-swarm`, one tiled pane per role, each
   running `claude` in its specialty workspace with the role's CLAUDE.md.

### First-time setup of each pane (one-time, ~30 seconds each)

The very first time you launch the swarm, every Claude Code pane goes through
its setup wizard before reaching the prompt:

1. **Login screen** ("Select login method" → 1. Claude account with subscription)
2. **Theme picker** (just press Enter)
3. **Permissions/trust dialog** (Yes)

Click into each pane (mouse works inside tmux) and finish all three. Until a
pane reaches the actual `>` prompt, dispatched tasks will land but Claude
won't process them — the dashboard now shows a purple **setup** pill on any
pane that's still in the wizard, and each card has a **⏎ Press Enter**
button you can use to advance simple "press Enter to continue" screens
without leaving the dashboard.

After setup, panes start straight into the prompt and the pill turns
**active** / **idle**.

Tmux quick keys: `Ctrl-b d` detach · `Ctrl-b o` cycle pane · `Ctrl-b z` zoom.
Reattach: `./launch.sh attach`. Stop everything: `./stop.sh`.

## Sending tasks from the dashboard

The top of the dashboard has a **DISPATCH TASK** panel:

1. Leave the dropdown on **🤖 Auto-pick** (default) — the router picks the
   best-fit agent for you. Or pick a specific agent yourself.
2. Type the task in the textarea.
3. Hit **SEND** (or `Cmd/Ctrl+Enter`).

The flash message shows which agent received the task and why
(e.g. *"sent to frontend (auto via claude: dark-mode toggle is a UI
change)"*).

### How auto-routing works

1. **Primary:** the router shells out to your already-authenticated `claude`
   CLI in headless mode (`claude --print --output-format json`) with a tiny
   prompt listing the available roles + the task. No separate API key
   needed — it uses your existing Claude subscription.
2. **Fallback:** if `claude` isn't on PATH or the call fails, a built-in
   keyword heuristic picks an agent (e.g. "react" → frontend,
   "endpoint" → backend, "compare X vs Y" → researcher,
   "launchd / deploy" → devops).

The status bar at the top shows **router READY** when the LLM router can
run, and **router OFF** when only the heuristic is available.

### What "send" actually does

Behind the scenes the server runs
`tmux send-keys -t jarvis-swarm:agents.<pane> -l "<task>"` and presses
Enter — exactly the same as typing the task yourself in that pane. The
task is also saved to `tasks/<agent>.md` so it survives restarts and shows
on the agent's card.

### "I sent a task and nothing happened"

99% of the time this is the pane being stuck in Claude Code's first-run
wizard (login / theme / trust prompts). Look for a purple **setup** pill on
the agent's card and the **PANE PREVIEW** panel showing setup screen text
instead of a Claude prompt. Either:

- click into the tmux pane and finish the wizard manually, or
- use the **⏎ Press Enter** button on the agent card for screens that
  just want Enter, then resend the task.

Once a pane is at the actual Claude `>` prompt, the **active** / **idle**
pill replaces **setup** and dispatches will work.

## Files

```
agents/
  roles.json              # ← define specialties here
  launch.sh               # tmux session builder; reads roles.json
  start.sh                # dashboard + launch.sh
  stop.sh                 # tear it all down
  tasks/<role>.md         # last task per agent (auto-updated)
  workspaces/<role>/
    CLAUDE.md             # generated system prompt per role
  dashboard/
    server.py             # FastAPI: monitor + /api/dispatch
    requirements.txt
```

## Adding or changing roles

Edit `roles.json`:

```json
{
  "id": "qa",
  "title": "QA Engineer",
  "emoji": "🧪",
  "specialty": "Writing tests, finding bugs, edge cases.",
  "system": "You are the QA specialist..."
}
```

Save, then `./stop.sh && ./start.sh`. The new agent appears in the tmux
layout and the dashboard dropdown.

## Tips

- Put the dashboard on a vertical second monitor in full-screen Chrome to
  match the look from the inspiration video.
- Status pills: **active** = updated in the last 30s, **idle** = older,
  **unstarted** = no Claude session yet, **offline** = tmux not running.
- Want a fifth, sixth, seventh agent? Just add entries to `roles.json`.
  The tmux layout retiles automatically.
