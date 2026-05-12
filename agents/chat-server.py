"""Direct chat UI for the OctoGent swarm — the control center.

Replaces fighting OctoGent's dashboard. One big input, auto-routes to the
right agent, all conversations visible, recent done-work shown alongside.

  Run:    python3 chat-server.py
  Open:   http://localhost:8766
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from typing import Optional, Tuple

import websockets
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

JARVIS = Path.home() / "JARVIS"
OCTOGENT_BIN = str(Path.home() / ".npm-global/bin/octogent")
OCTOGENT_URL = "http://127.0.0.1:8787"
WS_BASE = "ws://127.0.0.1:8787/api/terminals"
TENT_DIR = JARVIS / ".octogent" / "tentacles"

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

EMOJIS = {
    "jarvis": "🤖",
    "personal": "👤",
    "web-leads": "🌐",
    "web-build": "🎨",
    "car-flips": "🚗",
}

# ---------- helpers ----------


def list_main_terminals() -> list[dict[str, str]]:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{OCTOGENT_URL}/api/terminal-snapshots", timeout=3) as r:
            data = json.load(r)
    except Exception:
        return []
    by_tent: dict[str, list[dict]] = {}
    for t in data:
        if t.get("state") != "live":
            continue
        tid = t.get("terminalId", "")
        if not tid.startswith("terminal-"):
            continue
        by_tent.setdefault(t.get("tentacleId", ""), []).append(t)
    out: list[dict[str, str]] = []
    for tentacle, terms in sorted(by_tent.items()):
        terms.sort(key=lambda t: t.get("createdAt", ""))
        m = terms[0]
        out.append({
            "tentacle": tentacle,
            "terminalId": m["terminalId"],
            "emoji": EMOJIS.get(tentacle, "💬"),
            "runtimeState": m.get("agentRuntimeState", "?"),
        })
    return out


def clean_scrollback(raw: str) -> list[str]:
    s = ANSI.sub("", raw).replace("\r", "\n").replace("\x07", "")
    out: list[str] = []
    last = ""
    for line in s.split("\n"):
        line = line.rstrip()
        st = line.strip()
        if not st:
            continue
        if len(st) <= 2 and st in ("✶", "✳", "✻", "✽", "✢", "⏺", "·", "│", "—"):
            continue
        low = st.lower()
        if any(k in low for k in [
            "shift+tab to cycle", "ctrl+o to expand", "esc to interrupt",
            "for shortcuts", "/effort", "thinking…", "thinking",
            "flummoxing", "clauding", "brewed for", "architecting",
            "list files in", "single-task focus", "resume web-leads",
        ]):
            continue
        if line == last:
            continue
        last = line
        out.append(line)
    return out


async def fetch_history(terminal_id: str, lines: int = 80) -> list[str]:
    url = f"{WS_BASE}/{terminal_id}/ws"
    try:
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=3)
            except asyncio.TimeoutError:
                return []
            d = json.loads(msg)
            if d.get("type") != "history":
                return []
            cleaned = clean_scrollback(d.get("data", ""))
            return cleaned[-lines:]
    except Exception:
        return []


async def submit_enter(terminal_id: str) -> None:
    url = f"{WS_BASE}/{terminal_id}/ws"
    try:
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                pass
            await ws.send(json.dumps({"type": "input", "data": "\r"}))
            await asyncio.sleep(0.5)
    except Exception:
        pass


def channel_send(terminal_id: str, message: str) -> bool:
    try:
        r = subprocess.run(
            [OCTOGENT_BIN, "channel", "send", terminal_id, message],
            capture_output=True, text=True, timeout=10, cwd=str(JARVIS),
        )
        return r.returncode == 0
    except Exception:
        return False


# ---------- auto-router ----------


KEYWORDS: dict[str, list[str]] = {
    "jarvis":   ["jarvis", "voice", "wake word", "stt", "tts", "server.py",
                 "app.py", "brain/", "tools/", "electron"],
    "personal": ["text ", "imessage", "calendar", "remind", "gym", "workout",
                 "max ", "bennett", "neil", "andrew", "holden", "friend",
                 "morning brief", "schedule"],
    "web-leads":["lead", "prospect", "cold call", "cold email", "no website",
                 "outreach", "pitch", "phone number", "call list"],
    "web-build":["build a site", "build site", "scaffold", "landing", "deploy",
                 "astro", "tailwind", "jarvis sales", "client site",
                 "website for"],
    "car-flips":["flip", "marketplace", "craigslist", "car ", " car?",
                 "convertible", "miata", "wrx", "bmw", "subaru", "civic",
                 "mustang", "porsche", "911", "g37", "honda", "manual"],
}


def heuristic_route(task: str, available: set[str]) -> tuple[str, str]:
    s = task.lower()
    score: dict[str, int] = {k: 0 for k in KEYWORDS if k in available}
    for k in score:
        score[k] = sum(1 for kw in KEYWORDS[k] if kw in s)
    if not score:
        return next(iter(available), ""), "no agents available"
    best = max(score.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        # default to jarvis if nothing matches and jarvis exists
        if "jarvis" in available:
            return "jarvis", "no clear match — defaulted to jarvis"
        first = next(iter(available))
        return first, f"no clear match — defaulted to {first}"
    return best[0], f"keyword match (+{best[1]})"


def claude_route(task: str, available: list) -> Optional[Tuple[str, str]]:
    """Use the user's claude CLI to route. Falls back to None on any failure."""
    cbin = str(Path.home() / ".npm-global/bin/claude")
    if not Path(cbin).exists():
        return None
    role_lines = "\n".join(
        f"- {tent}: {EMOJIS.get(tent, '')} ({KEYWORDS.get(tent, [''])[0]}-related)"
        for tent in available
    )
    prompt = (
        "You are a router for a small swarm of specialized agents. "
        "Pick the single best agent for this task.\n\n"
        f"Available:\n{role_lines}\n\n"
        f"Task:\n{task}\n\n"
        "Reply with ONLY strict JSON: "
        '{"agent_id": "<id>", "reason": "<one short sentence>"}. '
        f"agent_id must be one of: {', '.join(available)}."
    )
    try:
        env = dict(os.environ)
        env["PATH"] = f"{Path.home()}/.npm-global/bin:" + env.get("PATH", "")
        r = subprocess.run(
            [cbin, "--print", "--output-format", "json",
             "--no-session-persistence", prompt],
            capture_output=True, text=True, timeout=30,
            cwd=str(Path.home()), env=env,
        )
        if r.returncode != 0:
            return None
        wrap = json.loads(r.stdout)
        text = (wrap.get("result") or "").strip()
        first = text.find("{"); last = text.rfind("}")
        if first != -1 and last > first:
            text = text[first:last+1]
        data = json.loads(text)
        agent = data.get("agent_id")
        if agent in available:
            return agent, str(data.get("reason", ""))[:160]
    except Exception:
        return None
    return None


def route_task(task: str) -> tuple[str, str, str]:
    mains = list_main_terminals()
    available = [m["tentacle"] for m in mains]
    if not available:
        raise RuntimeError("no live agents")
    ai = claude_route(task, available)
    if ai:
        return ai[0], "claude", ai[1]
    rid, reason = heuristic_route(task, set(available))
    return rid, "heuristic", reason


# ---------- recent done items ----------


def recent_done(per_agent: int = 3) -> list[dict[str, Any]]:
    pat = re.compile(r"^\s*-\s*\[x\]\s+(.+?)(?:\s+\*\((.*?)\)\*)?\s*$", re.IGNORECASE)
    out: list[dict[str, Any]] = []
    for d in sorted(TENT_DIR.iterdir() if TENT_DIR.exists() else []):
        if not d.is_dir():
            continue
        f = d / "todo.md"
        if not f.exists():
            continue
        rows = []
        for line in f.read_text().splitlines():
            m = pat.match(line)
            if m:
                rows.append({
                    "item": m.group(1).strip().split("—")[0].strip(),
                    "note": (m.group(2) or "").strip(),
                })
        for r in rows[-per_agent:]:
            out.append({
                "agent": d.name,
                "emoji": EMOJIS.get(d.name, "💬"),
                "item": r["item"],
                "note": r["note"],
            })
    return out


# ---------- API ----------

app = FastAPI()


class SendBody(BaseModel):
    text: str
    agent: Optional[str] = None  # None = auto


@app.get("/api/agents")
def api_agents() -> JSONResponse:
    return JSONResponse({"agents": list_main_terminals()})


@app.get("/api/messages")
async def api_messages(agent: str) -> JSONResponse:
    mains = list_main_terminals()
    match = next((m for m in mains if m["tentacle"] == agent), None)
    if not match:
        return JSONResponse({"lines": [], "error": f"unknown agent: {agent}"})
    lines = await fetch_history(match["terminalId"], lines=120)
    return JSONResponse({"lines": lines, "terminalId": match["terminalId"]})


@app.get("/api/done")
def api_done() -> JSONResponse:
    return JSONResponse({"items": recent_done(per_agent=4)})


@app.post("/api/send")
async def api_send(body: SendBody) -> JSONResponse:
    text = body.text.strip()
    if not text:
        return JSONResponse({"ok": False, "error": "empty"})
    routed_via = None
    routed_reason = None
    agent = body.agent
    if not agent or agent == "auto":
        try:
            agent, routed_via, routed_reason = route_task(text)
        except RuntimeError as e:
            return JSONResponse({"ok": False, "error": str(e)})
    mains = list_main_terminals()
    match = next((m for m in mains if m["tentacle"] == agent), None)
    if not match:
        return JSONResponse({"ok": False, "error": f"agent not running: {agent}"})
    tid = match["terminalId"]
    ok = channel_send(tid, text)
    if ok:
        await submit_enter(tid)
    return JSONResponse({
        "ok": ok,
        "agent": agent,
        "terminalId": tid,
        "routed_via": routed_via,
        "routed_reason": routed_reason,
    })


# ---------- HTML ----------

INDEX = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>JARVIS Control</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; }
  body {
    background: #06080e; color: #d6e1ff;
    font: 15px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
    display: grid; grid-template-columns: 1fr 320px; height: 100vh;
  }
  .col { display: flex; flex-direction: column; min-height: 0; }

  header {
    flex: 0 0 auto; padding: 10px 16px;
    border-bottom: 1px solid #1f2a44;
    display: flex; align-items: center; gap: 14px;
  }
  header h1 {
    margin: 0; font-size: 13px; letter-spacing: 0.2em;
    color: #7ee0ff; font-weight: 600;
  }
  .tabs { display: flex; gap: 4px; flex-wrap: wrap; flex: 1; }
  .tab {
    background: #0a1226; color: #93a7d6;
    border: 1px solid #1c2742; border-radius: 999px;
    padding: 4px 12px; cursor: pointer; font-size: 12px;
    user-select: none;
  }
  .tab:hover { background: #122044; color: #d6e1ff; }
  .tab.active { background: #1b3b6e; color: #fff; border-color: #2e6ea3; }
  .tab .dot {
    display: inline-block; width: 7px; height: 7px; border-radius: 50%;
    background: #45567e; margin-right: 6px; vertical-align: middle;
  }
  .tab .dot.processing { background: #79ffb0; }
  .tab .dot.idle { background: #ffd479; }
  .tab .dot.waiting_for_permission { background: #ff8c8c; }

  main {
    flex: 1; overflow: auto;
    padding: 16px 20px 220px;
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    font-size: 13px; line-height: 1.55;
    white-space: pre-wrap; word-break: break-word;
    color: #c4d2ee;
  }
  main .line { margin: 1px 0; }
  main .line.user { color: #c8a8ff; }
  main .line.agent { color: #b0e0ff; }
  main .empty { color: #45567e; font-style: italic; padding: 20px 0; }

  footer {
    position: fixed; bottom: 0; left: 0; right: 320px;
    padding: 12px 20px;
    background: linear-gradient(180deg, rgba(6,8,14,0), rgba(6,8,14,0.95) 30%);
  }
  .composer {
    background: #0a1226; border: 1px solid #2e6ea3; border-radius: 14px;
    padding: 10px 14px; display: flex; gap: 10px; align-items: flex-end;
    box-shadow: 0 -4px 24px rgba(0,0,0,0.5);
  }
  .composer textarea {
    flex: 1; background: transparent; color: #d6e1ff;
    border: none; outline: none; resize: none;
    font: inherit; min-height: 28px; max-height: 200px;
    line-height: 1.4;
  }
  .composer button {
    background: #133a5e; border: 1px solid #2e6ea3;
    color: #cfe9ff; border-radius: 10px;
    padding: 8px 16px; font-weight: 600;
    cursor: pointer;
  }
  .composer button:hover { background: #1b4f80; }
  .composer button:disabled { opacity: 0.4; cursor: not-allowed; }
  .hint { color: #6680b3; font-size: 11px; margin-top: 6px; padding: 0 6px; min-height: 16px; }
  .hint.ok  { color: #79ffb0; }
  .hint.err { color: #ff8c8c; }

  aside {
    border-left: 1px solid #1f2a44;
    overflow: auto;
    background: #08101e;
  }
  aside h2 {
    margin: 0; padding: 14px 16px 8px;
    font-size: 11px; letter-spacing: 0.2em; color: #7ee0ff;
  }
  aside .row {
    padding: 10px 16px; border-top: 1px solid #131c34;
    font-size: 12px; line-height: 1.4;
  }
  aside .row .who { color: #a8d6ff; font-weight: 600; }
  aside .row .item { color: #d6e1ff; margin-top: 2px; }
  aside .row .note { color: #6680b3; margin-top: 3px; font-size: 11px; }
</style>
</head>
<body>

<div class="col">
  <header>
    <h1>JARVIS // CONTROL</h1>
    <div class="tabs" id="tabs"></div>
  </header>
  <main id="messages"><div class="empty">pick an agent or type below — auto-route will pick for you</div></main>
  <footer>
    <div class="composer">
      <textarea id="input" placeholder="type a message... (Enter sends · Shift+Enter newline)"></textarea>
      <button id="send">Send</button>
    </div>
    <div class="hint" id="hint">auto-route is on by default — pick a tab to lock to one agent</div>
  </footer>
</div>

<aside>
  <h2>RECENT DONE</h2>
  <div id="done"></div>
</aside>

<script>
  let activeAgent = localStorage.getItem("activeAgent") || "auto";
  const tabsEl = document.getElementById("tabs");
  const messagesEl = document.getElementById("messages");
  const inputEl = document.getElementById("input");
  const sendBtn = document.getElementById("send");
  const hintEl = document.getElementById("hint");
  const doneEl = document.getElementById("done");

  function esc(s){ return (s||"").replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
  function classify(line){
    if (line.startsWith("[Channel message from")) return "user";
    if (/^\\s*>\\s/.test(line) || /^❯\\s/.test(line)) return "agent";
    if (line.startsWith("⏺")) return "agent";
    return "";
  }

  async function loadAgents(){
    let data;
    try { data = await (await fetch("/api/agents")).json(); }
    catch(e) { tabsEl.innerHTML = '<span style="color:#ff8c8c">connect failed</span>'; return; }
    if (!data.agents.length) {
      tabsEl.innerHTML = '<span style="color:#ff8c8c">no agents — start octogent</span>';
      return;
    }
    let html = `<div class="tab ${activeAgent==='auto'?'active':''}" data-tentacle="auto">🪄 auto</div>`;
    for (const a of data.agents) {
      const cls = a.runtimeState || 'idle';
      const active = activeAgent === a.tentacle ? ' active' : '';
      html += `<div class="tab${active}" data-tentacle="${esc(a.tentacle)}">
        <span class="dot ${esc(cls)}"></span>${esc(a.emoji)} ${esc(a.tentacle)}
      </div>`;
    }
    tabsEl.innerHTML = html;
    tabsEl.querySelectorAll(".tab").forEach(t => {
      t.addEventListener("click", () => {
        activeAgent = t.dataset.tentacle;
        localStorage.setItem("activeAgent", activeAgent);
        loadAgents();
        loadMessages();
      });
    });
  }

  async function loadMessages(){
    if (activeAgent === "auto") {
      messagesEl.innerHTML = '<div class="empty">auto-route mode — type below and the right agent will be picked. switch to a specific agent tab to see its conversation.</div>';
      return;
    }
    try {
      const data = await (await fetch("/api/messages?agent="+encodeURIComponent(activeAgent))).json();
      const lines = data.lines || [];
      if (!lines.length) {
        messagesEl.innerHTML = '<div class="empty">no messages yet — say something</div>';
        return;
      }
      const wasAtBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
      messagesEl.innerHTML = lines.map(l =>
        `<div class="line ${classify(l)}">${esc(l)}</div>`
      ).join("");
      if (wasAtBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
    } catch (e) {}
  }

  async function loadDone(){
    try {
      const data = await (await fetch("/api/done")).json();
      const items = data.items || [];
      if (!items.length) { doneEl.innerHTML = '<div class="row" style="color:#45567e">(nothing finished yet)</div>'; return; }
      doneEl.innerHTML = items.reverse().slice(0, 14).map(i => `
        <div class="row">
          <div class="who">${esc(i.emoji)} ${esc(i.agent)}</div>
          <div class="item">${esc(i.item)}</div>
          ${i.note ? `<div class="note">→ ${esc(i.note)}</div>` : ''}
        </div>
      `).join("");
    } catch (e) {}
  }

  async function send(){
    const text = inputEl.value.trim();
    if (!text) return;
    sendBtn.disabled = true;
    hintEl.className = "hint";
    hintEl.textContent = activeAgent === "auto" ? "routing..." : "sending...";
    try {
      const r = await fetch("/api/send", {
        method: "POST",
        headers: {"content-type":"application/json"},
        body: JSON.stringify({ agent: activeAgent === "auto" ? null : activeAgent, text }),
      });
      const data = await r.json();
      if (data.ok) {
        inputEl.value = "";
        let msg = "✓ sent to " + data.agent;
        if (data.routed_via) msg += " (auto: " + data.routed_via + (data.routed_reason ? " — " + data.routed_reason : "") + ")";
        hintEl.textContent = msg;
        hintEl.className = "hint ok";
        // jump to that agent's chat to see the response
        if (data.agent && data.agent !== activeAgent && activeAgent !== data.agent) {
          activeAgent = data.agent;
          localStorage.setItem("activeAgent", activeAgent);
          loadAgents();
        }
        setTimeout(() => { loadMessages(); loadDone(); }, 1500);
        setTimeout(() => { loadMessages(); loadDone(); }, 5000);
      } else {
        hintEl.textContent = "✗ " + (data.error || "failed");
        hintEl.className = "hint err";
      }
    } catch (e) {
      hintEl.textContent = "send error: " + e;
      hintEl.className = "hint err";
    } finally {
      sendBtn.disabled = false;
    }
  }

  sendBtn.addEventListener("click", send);
  inputEl.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault(); send();
    }
  });

  function tick(){ loadAgents(); loadMessages(); loadDone(); }
  tick();
  setInterval(tick, 4000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8766"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
