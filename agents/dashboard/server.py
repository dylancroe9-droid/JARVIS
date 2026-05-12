"""
JARVIS Agent Swarm dashboard.

- Reads agents/roles.json for the agent roster (specialized agents).
- Watches each agent's Claude Code session JSONL for live status.
- Lets you type a task and dispatch it to a specific agent's tmux pane
  via `tmux send-keys`. Includes an "auto" option that routes the task to
  the best-fit agent using a quick Claude Haiku call.
- Lets you press Enter on a stuck pane to advance Claude Code's first-run
  onboarding screens.

Run:
    python3 server.py
Then open http://localhost:8765
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

HOME = Path.home()
JARVIS = HOME / "JARVIS"
AGENTS = JARVIS / "agents"
WORKSPACES = AGENTS / "workspaces"
TASKS = AGENTS / "tasks"
ROLES_FILE = AGENTS / "roles.json"
PROJECTS = HOME / ".claude" / "projects"
TMUX_SESSION = "jarvis-swarm"

# Load JARVIS .env so ANTHROPIC_API_KEY is available to the router
_ENV_PATH = JARVIS / ".env"
if _ENV_PATH.exists():
    for _line in _ENV_PATH.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


def load_roles() -> list[dict[str, Any]]:
    if not ROLES_FILE.exists():
        return []
    try:
        return json.loads(ROLES_FILE.read_text())
    except json.JSONDecodeError:
        return []


def project_dir_for(workspace: Path) -> Path:
    slug = str(workspace.resolve()).replace("/", "-")
    return PROJECTS / slug


def latest_session_file(workspace: Path) -> Path | None:
    pdir = project_dir_for(workspace)
    if not pdir.exists():
        return None
    sessions = sorted(pdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return sessions[0] if sessions else None


def read_last_messages(path: Path, n: int = 15) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 200_000)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return []
    lines = [ln for ln in data.splitlines() if ln.strip()]
    out: list[dict[str, Any]] = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def summarize_message(msg: dict[str, Any]) -> str:
    m = msg.get("message") or {}
    role = m.get("role") or msg.get("type") or "?"
    content = m.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            t = block.get("type")
            if t == "text":
                parts.append(block.get("text", ""))
            elif t == "tool_use":
                parts.append(f"[{block.get('name', 'tool')}]")
            elif t == "tool_result":
                txt = block.get("content")
                if isinstance(txt, list):
                    txt = " ".join(b.get("text", "") for b in txt if isinstance(b, dict))
                parts.append(f"(result) {str(txt)[:120]}")
    text = " ".join(p for p in parts if p).strip().replace("\n", " ")
    if len(text) > 220:
        text = text[:217] + "..."
    return f"{role}: {text}" if text else role


# ---------- tmux helpers ----------

def tmux_pane_for(role_index: int) -> str:
    """tmux target for an agent. Panes are created in roles.json order; index 0-based."""
    return f"{TMUX_SESSION}:agents.{role_index}"


def tmux_session_alive() -> bool:
    r = subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION],
        capture_output=True,
    )
    return r.returncode == 0


def tmux_send(target: str, text: str) -> tuple[bool, str]:
    """Send `text` to a tmux pane and press Enter to submit it to claude."""
    if not tmux_session_alive():
        return False, "tmux session 'jarvis-swarm' is not running. Start it with ./launch.sh."
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", text],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "Enter"],
            check=True, capture_output=True, text=True,
        )
        return True, "ok"
    except subprocess.CalledProcessError as e:
        return False, e.stderr or str(e)


def tmux_keypress(target: str, key: str) -> tuple[bool, str]:
    """Send a single named key (Enter, Escape, etc.) to a pane."""
    if not tmux_session_alive():
        return False, "tmux session 'jarvis-swarm' is not running."
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, key],
            check=True, capture_output=True, text=True,
        )
        return True, "ok"
    except subprocess.CalledProcessError as e:
        return False, e.stderr or str(e)


def tmux_capture(target: str, lines: int = 6) -> str:
    """Return the last N visible lines of a pane as a string."""
    if not tmux_session_alive():
        return ""
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", target, "-p"],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError:
        return ""
    out_lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    return "\n".join(out_lines[-lines:])


def pane_ready(preview: str) -> bool:
    """Heuristic: does the pane look like Claude Code is at its prompt?"""
    if not preview:
        return False
    low = preview.lower()
    setup_signals = (
        "select login method",
        "syntax theme",
        "press enter to continue",
        "trust this folder",
        "yes, i trust",
        "browser didn",
        "paste code here",
    )
    if any(s in low for s in setup_signals):
        return False
    # Heuristic for the actual prompt: the placeholder "Try \"...\"" line
    # is shown when Claude Code's input box is empty and ready.
    return ("❯" in preview) or ("> " in preview) or ("try \"" in low)


# ---------- auto-router ----------

ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "claude-haiku-4-5")

def heuristic_route(roles: list[dict[str, Any]], task: str) -> tuple[str, str]:
    """Cheap keyword fallback when the API isn't available."""
    t = task.lower()
    keywords = {
        "frontend": ["css", "html", "react", "ui", "ux", "component", "tailwind",
                     "button", "layout", "animation", "browser", "dom", "page",
                     "design", "style", "form"],
        "backend":  ["api", "endpoint", "database", "db", "sql", "schema",
                     "fastapi", "flask", "server", "python", "queue", "auth",
                     "model", "migration", "server.py"],
        "researcher": ["research", "compare", "summarize", "summary", "look up",
                       "investigate", "find out", "what is", "docs", "article"],
        "devops":   ["script", "shell", "bash", "deploy", "ci", "github action",
                     "brew", "npm", "pip", "env", "launchd", "cron", "docker"],
    }
    scores: dict[str, int] = {r["id"]: 0 for r in roles}
    for rid, kws in keywords.items():
        if rid in scores:
            scores[rid] += sum(1 for k in kws if k in t)
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        # No keyword hits — default to backend (broadest catch-all in this swarm)
        for fallback in ("backend", "researcher"):
            if fallback in scores:
                return fallback, "no clear keyword match — fell back to default"
        return roles[0]["id"], "no clear match — picked first agent"
    return best[0], f"keyword match (+{best[1]})"


def _find_claude_bin() -> str | None:
    """Find the claude CLI binary so we can shell out from the dashboard."""
    candidates = [
        os.environ.get("CLAUDE_BIN"),
        str(Path.home() / ".npm-global/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    # Last resort: PATH lookup
    r = subprocess.run(["which", "claude"], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def claude_route(roles: list[dict[str, Any]], task: str) -> tuple[str, str] | None:
    """Ask the user's already-authenticated `claude` CLI which agent should handle this.
    Uses `claude --print --output-format json` so no separate API key is needed.
    Returns (id, reason) or None on failure.
    """
    cbin = _find_claude_bin()
    if cbin is None:
        return None

    role_lines = "\n".join(
        f"- {r['id']}: {r.get('title','')} — {r.get('specialty','')}" for r in roles
    )
    valid_ids = [r["id"] for r in roles]
    prompt = (
        "You are a router for a small swarm of specialized coding agents. "
        "Pick the single best-fit agent for the task.\n\n"
        f"Available agents:\n{role_lines}\n\n"
        f"Task:\n{task}\n\n"
        "Reply with ONLY strict JSON, no prose, no code fences, of the form:\n"
        '{"agent_id": "<id>", "reason": "<one short sentence>"}\n'
        f"agent_id must be exactly one of: {', '.join(valid_ids)}."
    )
    try:
        # Run in a neutral cwd so it doesn't pick up any project's CLAUDE.md.
        env = dict(os.environ)
        env["PATH"] = f"{Path.home()}/.npm-global/bin:" + env.get("PATH", "")
        r = subprocess.run(
            [cbin, "--print", "--output-format", "json", "--no-session-persistence", prompt],
            capture_output=True, text=True, timeout=45, cwd=str(Path.home()), env=env,
        )
        if r.returncode != 0:
            return None
        wrap = json.loads(r.stdout)
        # `--output-format json` returns {"result": "...", ...}
        text = (wrap.get("result") or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        # Tolerate models that wrap JSON in extra text
        first = text.find("{"); last = text.rfind("}")
        if first != -1 and last > first:
            text = text[first:last+1]
        data = json.loads(text)
        agent_id = data.get("agent_id")
        if agent_id in valid_ids:
            return agent_id, str(data.get("reason", ""))[:160]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None
    return None


def route_task(task: str, only_ready: bool = True) -> tuple[str, str, str]:
    """Pick an agent for the task. Returns (agent_id, source, reason).

    If `only_ready` is True, restrict to panes whose Claude prompt is reachable
    (i.e. not stuck in the first-run wizard). Falls back to all roles only if
    none are ready.
    """
    all_roles = load_roles()
    if not all_roles:
        raise RuntimeError("no roles defined")

    candidates = all_roles
    if only_ready and tmux_session_alive():
        ready = []
        for i, r in enumerate(all_roles):
            preview = tmux_capture(tmux_pane_for(i), lines=6)
            if pane_ready(preview):
                ready.append(r)
        if ready:
            candidates = ready

    ai = claude_route(candidates, task)
    if ai is not None:
        return ai[0], "claude", ai[1]
    rid, reason = heuristic_route(candidates, task)
    if candidates is not all_roles:
        reason += " (only-ready)"
    return rid, "heuristic", reason


def router_available() -> bool:
    return _find_claude_bin() is not None


# ---------- agent state ----------

@dataclass
class AgentState:
    id: str
    title: str
    emoji: str
    specialty: str
    pane: int
    workspace: str
    task: str
    status: str            # active | idle | unstarted | offline | setup
    last_update: float | None
    last_line: str
    recent: list[str]
    pane_preview: str
    ready: bool


def list_agents() -> list[AgentState]:
    roles = load_roles()
    session_alive = tmux_session_alive()
    out: list[AgentState] = []
    for i, role in enumerate(roles):
        rid = role["id"]
        ws = WORKSPACES / rid
        task_file = TASKS / f"{rid}.md"
        task = task_file.read_text() if task_file.exists() else ""
        sess = latest_session_file(ws) if ws.exists() else None

        preview = tmux_capture(tmux_pane_for(i), lines=6) if session_alive else ""
        ready = pane_ready(preview) if session_alive else False

        if sess is None:
            if not session_alive:
                status = "offline"
            elif preview and not ready:
                status = "setup"
            else:
                status = "unstarted"
            out.append(AgentState(
                id=rid, title=role.get("title", rid), emoji=role.get("emoji", ""),
                specialty=role.get("specialty", ""), pane=i, workspace=str(ws),
                task=task.strip(), status=status, last_update=None,
                last_line=("finish Claude Code setup in the tmux pane"
                           if status == "setup"
                           else "(no session yet — start the swarm with ./launch.sh)"),
                recent=[], pane_preview=preview, ready=ready,
            ))
            continue

        msgs = read_last_messages(sess, n=15)
        lines = [summarize_message(m) for m in msgs]
        last_update = sess.stat().st_mtime
        age = time.time() - last_update
        status = "active" if age < 30 else "idle"
        out.append(AgentState(
            id=rid, title=role.get("title", rid), emoji=role.get("emoji", ""),
            specialty=role.get("specialty", ""), pane=i, workspace=str(ws),
            task=task.strip(), status=status, last_update=last_update,
            last_line=lines[-1] if lines else "(empty session)",
            recent=lines[-8:], pane_preview=preview, ready=ready,
        ))
    return out


# ---------- API ----------

app = FastAPI()


class DispatchBody(BaseModel):
    agent_id: str   # role id, or "auto" to let the router pick
    task: str
    force: bool = False  # if True, send even when the pane looks stuck


class KeypressBody(BaseModel):
    agent_id: str
    key: str = "Enter"  # tmux key name: Enter, Escape, Up, Down, etc.


class RouteBody(BaseModel):
    task: str


@app.get("/api/agents")
def api_agents() -> JSONResponse:
    agents = [asdict(a) for a in list_agents()]
    return JSONResponse({
        "agents": agents,
        "session_alive": tmux_session_alive(),
        "router_available": router_available(),
    })


@app.post("/api/route")
def api_route(body: RouteBody) -> JSONResponse:
    task = body.task.strip()
    if not task:
        return JSONResponse({"ok": False, "error": "empty task"}, status_code=400)
    try:
        agent_id, source, reason = route_task(task)
        return JSONResponse({"ok": True, "agent_id": agent_id, "source": source, "reason": reason})
    except RuntimeError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/dispatch")
def api_dispatch(body: DispatchBody) -> JSONResponse:
    task = body.task.strip()
    if not task:
        return JSONResponse({"ok": False, "error": "empty task"}, status_code=400)

    roles = load_roles()
    routing_source: str | None = None
    routing_reason: str | None = None
    target_id = body.agent_id

    if target_id == "auto":
        try:
            target_id, routing_source, routing_reason = route_task(task)
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    pane = next((i for i, r in enumerate(roles) if r["id"] == target_id), None)
    if pane is None:
        return JSONResponse({"ok": False, "error": f"unknown agent: {target_id}"}, status_code=404)

    target = tmux_pane_for(pane)
    preview = tmux_capture(target, lines=6)
    ready = pane_ready(preview)

    if not ready and not body.force:
        return JSONResponse({
            "ok": False,
            "agent_id": target_id,
            "routing_source": routing_source,
            "routing_reason": routing_reason,
            "ready": False,
            "needs_setup": True,
            "error": (f"{target_id} pane is still in Claude Code's first-run setup. "
                      f"Click into that tmux pane and finish onboarding, or use the "
                      f"'⏎ Press Enter' button on the agent card. Then resend."),
        })

    ok, msg = tmux_send(target, task)

    task_file = TASKS / f"{target_id}.md"
    try:
        TASKS.mkdir(parents=True, exist_ok=True)
        task_file.write_text(f"# {target_id} — last task\n\n{task}\n")
    except OSError:
        pass

    return JSONResponse({
        "ok": ok,
        "error": None if ok else msg,
        "agent_id": target_id,
        "routing_source": routing_source,
        "routing_reason": routing_reason,
        "ready": ready,
        "needs_setup": False,
    })


@app.post("/api/keypress")
def api_keypress(body: KeypressBody) -> JSONResponse:
    roles = load_roles()
    pane = next((i for i, r in enumerate(roles) if r["id"] == body.agent_id), None)
    if pane is None:
        return JSONResponse({"ok": False, "error": f"unknown agent: {body.agent_id}"}, status_code=404)
    # Allowlist of safe key names
    allowed = {"Enter", "Escape", "Space", "Tab", "Up", "Down", "Left", "Right",
               "BSpace", "C-c", "C-d"}
    if body.key not in allowed:
        return JSONResponse({"ok": False, "error": f"key not allowed: {body.key}"}, status_code=400)
    ok, msg = tmux_keypress(tmux_pane_for(pane), body.key)
    return JSONResponse({"ok": ok, "error": None if ok else msg})


# ---------- UI ----------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>JARVIS // Agent Swarm</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px;
    font: 14px/1.45 ui-monospace, "SF Mono", Menlo, monospace;
    background: #07090f; color: #d6e1ff;
  }
  header {
    display: flex; align-items: baseline; gap: 16px;
    border-bottom: 1px solid #1f2a44; padding-bottom: 10px; margin-bottom: 14px;
  }
  h1 { margin: 0; font-size: 16px; letter-spacing: 0.18em; color: #7ee0ff; }
  .meta { color: #6680b3; font-size: 12px; }

  .dispatch {
    border: 1px solid #1c2742; background: linear-gradient(180deg,#0a1226,#080d1d);
    border-radius: 10px; padding: 12px 14px; margin-bottom: 14px;
  }
  .dispatch h2 { margin: 0 0 8px; font-size: 12px; letter-spacing: 0.18em; color: #7ee0ff; }
  .dispatch .row { display: flex; gap: 8px; }
  .dispatch select, .dispatch textarea, .dispatch button {
    background: #0a1226; color: #d6e1ff; border: 1px solid #28406e;
    border-radius: 6px; padding: 8px 10px; font: inherit;
  }
  .dispatch select { min-width: 220px; }
  .dispatch textarea { flex: 1; min-height: 60px; resize: vertical; }
  .dispatch button {
    cursor: pointer; background: #133a5e; border-color: #2e6ea3; color: #cfe9ff;
    font-weight: 600; letter-spacing: 0.06em;
  }
  .dispatch button:hover { background: #1b4f80; }
  .dispatch button:disabled { opacity: 0.5; cursor: not-allowed; }
  .dispatch .hint { margin-top: 6px; color: #6680b3; font-size: 11px; }
  .dispatch .flash { margin-top: 6px; font-size: 12px; }
  .dispatch .flash.ok   { color: #79ffb0; }
  .dispatch .flash.warn { color: #ffd479; }
  .dispatch .flash.err  { color: #ff8c8c; }

  .pill.setup { color: #d6b3ff; border-color: #4d2a6e; background: #1c0e2a; }
  .preview {
    margin-top: 8px; padding: 6px 8px; border-radius: 4px;
    background: #04060c; border: 1px solid #15203a;
    color: #6680b3; font-size: 11px; white-space: pre-wrap;
    max-height: 110px; overflow: auto;
  }
  .actions { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
  .actions button {
    cursor: pointer; font: inherit; font-size: 11px; padding: 4px 8px;
    background: #0a1226; color: #a8d6ff; border: 1px solid #28406e;
    border-radius: 4px;
  }
  .actions button:hover { background: #14213d; }

  .grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); }
  .card {
    border: 1px solid #1c2742; background: linear-gradient(180deg,#0c1326,#080d1d);
    border-radius: 10px; padding: 12px 14px;
  }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  .name { color: #a8d6ff; font-weight: 600; letter-spacing: 0.12em; }
  .specialty { color: #6680b3; font-size: 11px; margin-top: 2px; }
  .pill { font-size: 10px; padding: 2px 8px; border-radius: 999px; border: 1px solid #28406e; color: #8fb6ff; }
  .pill.active    { color: #79ffb0; border-color: #2a6e44; background: #0e2a1c; }
  .pill.idle      { color: #ffd479; border-color: #6e5328; background: #2a210e; }
  .pill.unstarted { color: #ff8c8c; border-color: #6e2828; background: #2a0e0e; }
  .pill.offline   { color: #888; border-color: #444; background: #181818; }
  .pill.setup     { color: #d6b3ff; border-color: #4d2a6e; background: #1c0e2a; }
  .task { margin: 8px 0 6px; color: #93a7d6; font-size: 12px; white-space: pre-wrap; max-height: 4.5em; overflow: hidden; }
  .last { color: #d6e1ff; margin: 6px 0; }
  .log { margin-top: 8px; padding-top: 8px; border-top: 1px dashed #1c2742; color: #5e76a6; font-size: 12px; max-height: 160px; overflow: auto; }
  .log div { margin: 2px 0; }
  .log .user { color: #c8a8ff; }
  .log .assistant { color: #b0e0ff; }
  footer { margin-top: 18px; color: #45567e; font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>JARVIS // AGENT SWARM</h1>
  <span class="meta" id="meta">loading…</span>
</header>

<section class="dispatch">
  <h2>DISPATCH TASK</h2>
  <div class="row">
    <select id="agentSel"></select>
    <textarea id="taskTxt" placeholder="Describe the task — leave Auto picked and the router will choose the best agent. (Cmd/Ctrl+Enter to send)"></textarea>
    <button id="sendBtn">SEND ▸</button>
  </div>
  <div class="hint" id="dispatchHint">Auto-routing uses Claude Haiku to pick the best agent for the task.</div>
  <div class="flash" id="flash"></div>
</section>

<div class="grid" id="grid"></div>
<footer>auto-refresh every 2s · sessions read from ~/.claude/projects · tmux session: jarvis-swarm</footer>

<script>
  const grid = document.getElementById('grid');
  const meta = document.getElementById('meta');
  const sel  = document.getElementById('agentSel');
  const txt  = document.getElementById('taskTxt');
  const btn  = document.getElementById('sendBtn');
  const flash= document.getElementById('flash');

  function esc(s){ return (s||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
  function roleClass(line){
    if (line.startsWith('user:')) return 'user';
    if (line.startsWith('assistant:')) return 'assistant';
    return '';
  }

  async function tick(){
    try {
      const r = await fetch('/api/agents');
      const data = await r.json();
      meta.textContent = data.agents.length + ' agents · ' +
        (data.session_alive ? 'tmux UP' : 'tmux DOWN — run ./launch.sh') +
        (data.router_available ? ' · router READY' : ' · router OFF (no API key)') +
        ' · ' + new Date().toLocaleTimeString();

      // Dropdown: Auto first, then each role
      const prev = sel.value || 'auto';
      const opts = ['<option value="auto">🤖 Auto-pick (recommended)</option>']
        .concat(data.agents.map(a =>
          `<option value="${esc(a.id)}">${esc(a.emoji)} ${esc(a.title)} (${esc(a.id)})</option>`
        ));
      sel.innerHTML = opts.join('');
      sel.value = (prev === 'auto' || data.agents.some(a => a.id === prev)) ? prev : 'auto';

      grid.innerHTML = data.agents.map(a => `
        <div class="card">
          <div class="row">
            <div>
              <div class="name">${esc(a.emoji)} ${esc(a.title)}</div>
              <div class="specialty">${esc(a.specialty)}</div>
            </div>
            <span class="pill ${a.status}">${a.status}</span>
          </div>
          <div class="task">${esc(a.task) || '(no task yet)'}</div>
          <div class="last">${esc(a.last_line)}</div>
          ${a.pane_preview ? `<div class="preview">${esc(a.pane_preview)}</div>` : ''}
          <div class="actions">
            <button data-action="enter"  data-id="${esc(a.id)}">⏎ Press Enter</button>
            <button data-action="escape" data-id="${esc(a.id)}">⎋ Escape</button>
            <button data-action="ctrlc"  data-id="${esc(a.id)}">^C Cancel</button>
          </div>
          <div class="log">
            ${a.recent.map(l => `<div class="${roleClass(l)}">${esc(l)}</div>`).join('')}
          </div>
        </div>
      `).join('');

      grid.querySelectorAll('button[data-action]').forEach(b => {
        b.addEventListener('click', () => keypress(b.dataset.id, b.dataset.action));
      });
    } catch (e) {
      meta.textContent = 'error: ' + e;
    }
  }

  async function keypress(agent_id, action){
    const keyMap = { enter: 'Enter', escape: 'Escape', ctrlc: 'C-c' };
    const key = keyMap[action];
    if (!key) return;
    try {
      await fetch('/api/keypress', {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({ agent_id, key }),
      });
      tick();
    } catch (e) { /* ignore */ }
  }

  async function send(opts = {}) {
    const agent_id = opts.agent_id || sel.value || 'auto';
    const task = txt.value.trim();
    const force = !!opts.force;
    if (!task) { flash.textContent = 'enter a task first'; flash.className = 'flash err'; return; }
    btn.disabled = true;
    flash.textContent = (agent_id === 'auto' ? 'routing…' : 'dispatching…');
    flash.className = 'flash';
    try {
      const r = await fetch('/api/dispatch', {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({ agent_id, task, force }),
      });
      const data = await r.json();
      if (data.ok) {
        let msg = '✓ sent to ' + data.agent_id;
        if (data.routing_source) {
          msg += ' (auto via ' + data.routing_source +
                 (data.routing_reason ? ': ' + data.routing_reason : '') + ')';
        }
        flash.textContent = msg;
        flash.className = 'flash ok';
        txt.value = '';
        tick();
      } else if (data.needs_setup) {
        // Stuck pane — offer a one-click fix
        flash.innerHTML = '⚠ ' + esc(data.error) + ' ';
        const b = document.createElement('button');
        b.textContent = '⏎ Press Enter & retry';
        b.style.marginLeft = '6px';
        b.onclick = async () => {
          await fetch('/api/keypress', {
            method: 'POST', headers: {'content-type': 'application/json'},
            body: JSON.stringify({ agent_id: data.agent_id, key: 'Enter' }),
          });
          // Brief pause for the pane to redraw, then resend
          setTimeout(() => send({ agent_id: data.agent_id }), 600);
        };
        flash.appendChild(b);
        flash.className = 'flash warn';
      } else {
        flash.textContent = '✗ ' + (data.error || 'failed');
        flash.className = 'flash err';
      }
    } catch (e) {
      flash.textContent = 'error: ' + e;
      flash.className = 'flash err';
    } finally {
      btn.disabled = false;
    }
  }
  btn.addEventListener('click', () => send());
  txt.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); send(); }
  });

  tick();
  setInterval(tick, 2000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8765"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
