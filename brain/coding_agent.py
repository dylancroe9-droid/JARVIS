"""
JARVIS Self-Coding Agent
========================
Autonomous agentic loop that reads, edits, and tests JARVIS's own code.
Runs in a background thread — completely separate from the main conversation.
Reports progress via a callback so the frontend can show a live coding panel.
"""

from __future__ import annotations

import os
import subprocess
import threading
from typing import Callable

from config import JARVIS_DIR  # canonical location of this install

# ── Codebase knowledge ────────────────────────────────────────────────────────
# Path is interpolated at runtime so the LLM sees the current install location,
# not the developer's machine.
_CODEBASE_MAP = f"""
JARVIS CODEBASE — {JARVIS_DIR}/
═══════════════════════════════════════════════════════════
server.py               FastAPI WebSocket server. Handles voice I/O, blueprint image
                        analysis, Wikipedia fetches. broadcast() sends to frontend.
                        Restart: python server.py

brain/
  jarvis.py             Core Jarvis class. LLM routing (Claude/Groq), conversation
                        loop, tool detection, blueprint triggers.
  personality.py        ALL system prompts. Edit to change JARVIS's behaviour/knowledge.
                        Contains get_system_prompt(), get_fast_prompt().
  tools.py              Tool DEFINITIONS (JSON schemas for the Claude API).
                        TOOL_DEFINITIONS list. Also _CORE_TOOL_NAMES set.
                        Edit here to expose a new tool to Claude.
  _executor.py          Tool DISPATCH. Routes tool names to Python implementations.
                        Add new elif name == "tool_name" blocks here.
  memory.py             Persistent memory. load_memory() / save_memory(key, val).

tools/                  Individual tool implementations (Python functions).
  file_tools.py         read_file, write_file, list_directory, search_files
  shell_tool.py         run_command (subprocess wrapper with timeout)
  system_tools.py       open_application, get_system_info, set_dark_mode
  browser_tool.py       Playwright browser automation
  calendar_tool.py      Apple Calendar via AppleScript
  music_tools.py        Apple Music / Spotify control
  extras_tool.py        Weather, news, battery, etc.

jarvis-app/             Electron app
  main.js               Electron main. Window management, IPC, Python spawn.
  renderer/
    index.html          UI structure + all CSS (inline <style> block).
    app.js              ALL frontend logic. ~5200 lines.
                        WebSocket handler, AR overlays, Three.js blueprints,
                        camera feed, voice animations, typing input.
                        Restart: npm start (in jarvis-app/)

HOW TOOLS WORK — adding a new tool requires THREE changes:
  1. brain/tools.py       → add to TOOL_DEFINITIONS list (JSON schema)
  2. brain/_executor.py   → add elif block in _dispatch()
  3. tools/[name].py      → implement the Python function (if new file needed)

RESTART COMMANDS:
  Server:   cd ~/JARVIS && source .venv/bin/activate && python server.py
  Frontend: cd ~/JARVIS/jarvis-app && npm start
═══════════════════════════════════════════════════════════
"""

_AGENT_SYSTEM_PROMPT = f"""You are JARVIS's autonomous self-coding agent.
Your job: implement the requested feature in the JARVIS codebase, correctly, then verify it works.

{_CODEBASE_MAP}

METHODOLOGY — follow this exactly:

STEP 1: UNDERSTAND
  grep_code to find relevant existing code.
  read_file with offset/limit to read the specific section — never load whole files.
  Understand the pattern before writing anything.

STEP 2: PLAN
  Before writing code, think through: what files change, what new code is needed,
  what existing code needs updating, what could break.

STEP 3: IMPLEMENT
  edit_file for surgical changes to existing files. old_string must be unique.
  write_file only for entirely new files.
  Follow the existing code style exactly — same variable names, same patterns.

STEP 4: VERIFY
  After every code change:
  - JS files:  run_command("node --check /path/to/file.js")
  - Python:    run_command("python -c \\"import ast; ast.parse(open('file.py').read()); print('OK')\\"")
  Fix any errors before continuing.

STEP 5: DONE
  Call done() with a clear summary and which components need restarting.

RULES:
- ALWAYS read before editing. No exceptions.
- Keep old_string short but unique — include 2-3 lines of context.
- One change at a time. Check syntax after each.
- If something fails, diagnose it, fix it. Don't give up.
- Write clean code that matches the existing style.
- Don't over-engineer. Minimal working solution first.
"""

# ── Tool definitions for the coding agent ────────────────────────────────────
_CODING_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file. Use offset+limit to read specific sections — never load big files whole.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":   {"type": "string"},
                "offset": {"type": "integer", "description": "Start line (0-based)"},
                "limit":  {"type": "integer", "description": "Number of lines"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file. old_string must appear exactly once. Read first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":       {"type": "string"},
                "old_string": {"type": "string", "description": "Exact text to replace"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "write_file",
        "description": "Write a complete file. Use for new files only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "grep_code",
        "description": "Search for a pattern in the codebase. Returns matching lines with file:line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern":   {"type": "string"},
                "path":      {"type": "string", "description": "File or directory to search"},
                "max_lines": {"type": "integer", "default": 30},
            },
            "required": ["pattern", "path"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command. Use for syntax checks, installs, tests.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command":           {"type": "string"},
                "working_directory": {"type": "string"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files in a directory.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "done",
        "description": "Signal the task is complete.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary":         {"type": "string", "description": "What was built or changed"},
                "restart_needed":  {
                    "type": "array",
                    "items": {"type": "string", "enum": ["server", "electron"]},
                    "description": "Which services need restarting",
                },
            },
            "required": ["summary", "restart_needed"],
        },
    },
]

# ── Tool execution ────────────────────────────────────────────────────────────

def _exec_tool(name: str, args: dict) -> str:
    if name == "read_file":
        path   = args["path"]
        offset = int(args.get("offset") or 0)
        limit  = args.get("limit")
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            end = offset + int(limit) if limit else len(lines)
            selected = lines[offset:end]
            total = len(lines)
            header = f"[{path}  lines {offset+1}–{min(end,total)} of {total}]\n"
            return header + "".join(f"{offset+i+1}\t{l}" for i, l in enumerate(selected))
        except Exception as e:
            return f"read_file error: {e}"

    elif name == "edit_file":
        path = args["path"]
        old  = args["old_string"]
        new  = args["new_string"]
        try:
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            if old not in src:
                return (
                    f"FAILED — old_string not found in {path}.\n"
                    "Read the file first to get the exact text, then retry."
                )
            count = src.count(old)
            if count > 1:
                return (
                    f"FAILED — old_string appears {count} times in {path}. "
                    "Include more surrounding context to make it unique."
                )
            with open(path, "w", encoding="utf-8") as f:
                f.write(src.replace(old, new, 1))
            delta = new.count("\n") - old.count("\n")
            return f"OK — replaced in {path}  (line delta: {delta:+d})"
        except Exception as e:
            return f"edit_file error: {e}"

    elif name == "write_file":
        path    = args["path"]
        content = args["content"]
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"OK — wrote {len(content.splitlines())} lines to {path}"
        except Exception as e:
            return f"write_file error: {e}"

    elif name == "grep_code":
        pattern   = args["pattern"]
        path      = args.get("path", JARVIS_DIR)
        max_lines = int(args.get("max_lines", 30))
        try:
            result = subprocess.run(
                ["grep", "-rn",
                 "--include=*.py", "--include=*.js",
                 "--include=*.html", "--include=*.css",
                 "-m", str(max_lines),
                 pattern, path],
                capture_output=True, text=True, timeout=10,
            )
            out = result.stdout.strip()
            if not out:
                return f"No matches for '{pattern}' in {path}"
            lines = out.splitlines()
            if len(lines) >= max_lines:
                lines.append(f"… (capped at {max_lines} lines)")
            return "\n".join(lines)
        except Exception as e:
            return f"grep_code error: {e}"

    elif name == "run_command":
        cmd = args["command"]
        cwd = args.get("working_directory", JARVIS_DIR)
        try:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=30, cwd=cwd,
            )
            out = (r.stdout + r.stderr).strip()
            return out[:3000] if out else "(no output — exit code " + str(r.returncode) + ")"
        except subprocess.TimeoutExpired:
            return "Timed out after 30s"
        except Exception as e:
            return f"run_command error: {e}"

    elif name == "list_directory":
        path = args.get("path", JARVIS_DIR)
        try:
            entries = sorted(os.listdir(path))
            return "\n".join(entries)
        except Exception as e:
            return f"list_directory error: {e}"

    return f"Unknown tool: {name}"


# ── Agent loop ────────────────────────────────────────────────────────────────

def run_coding_agent(
    request: str,
    api_key: str,
    progress_cb: Callable[[str, str], None] | None = None,
) -> dict:
    """
    Run the coding agent synchronously.
    progress_cb(phase, detail) is called at each step for live updates.
    Returns {"summary": str, "restart_needed": list[str], "success": bool}
    """
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    messages = [{"role": "user", "content": request}]
    MAX_ITERS = 25

    for iteration in range(MAX_ITERS):
        if progress_cb:
            progress_cb("thinking", f"Iteration {iteration + 1}/{MAX_ITERS}")

        try:
            resp = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=4096,
                system=_AGENT_SYSTEM_PROMPT,
                tools=_CODING_TOOLS,
                messages=messages,
            )
        except Exception as api_exc:
            if progress_cb:
                progress_cb("error", f"API error: {api_exc}")
            return {"summary": f"API error: {api_exc}", "restart_needed": [], "success": False}

        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            text = next(
                (b.text for b in resp.content if hasattr(b, "text")), "Done."
            )
            return {"summary": text, "restart_needed": [], "success": True}

        if resp.stop_reason != "tool_use":
            return {
                "summary": f"Unexpected stop: {resp.stop_reason}",
                "restart_needed": [],
                "success": False,
            }

        # ── Execute all tool calls in this turn ───────────────────────────
        tool_results = []
        done_payload = None

        for block in resp.content:
            if not hasattr(block, "type") or block.type != "tool_use":
                continue

            tname = block.name
            targs = block.input

            if tname == "done":
                done_payload = targs
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Acknowledged.",
                })
                break

            # Progress update
            if progress_cb:
                label = {
                    "read_file":      f"Reading  {targs.get('path','').replace(JARVIS_DIR+'/', '')}",
                    "edit_file":      f"Editing  {targs.get('path','').replace(JARVIS_DIR+'/', '')}",
                    "write_file":     f"Writing  {targs.get('path','').replace(JARVIS_DIR+'/', '')}",
                    "grep_code":      f"Searching '{targs.get('pattern','')}'",
                    "run_command":    f"$ {targs.get('command','')[:60]}",
                    "list_directory": f"Listing  {targs.get('path','') or JARVIS_DIR}",
                }.get(tname, tname)
                progress_cb(tname, label)

            result = _exec_tool(tname, targs)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        if done_payload:
            return {
                "summary":        done_payload.get("summary", "Complete."),
                "restart_needed": done_payload.get("restart_needed", []),
                "success":        True,
            }

        messages.append({"role": "user", "content": tool_results})

    return {
        "summary": "Reached max iterations without completing.",
        "restart_needed": [],
        "success": False,
    }


# ── Background runner ─────────────────────────────────────────────────────────

def run_coding_agent_background(
    request: str,
    api_key: str,
    progress_cb: Callable[[str, str], None] | None = None,
    done_cb: Callable[[dict], None] | None = None,
) -> threading.Thread:
    """
    Run the coding agent in a daemon thread.
    Returns the thread (already started).
    """
    def _run():
        try:
            result = run_coding_agent(request, api_key, progress_cb)
        except Exception as exc:
            if progress_cb:
                progress_cb("error", f"Fatal error: {exc}")
            result = {"summary": f"Fatal error: {exc}", "restart_needed": [], "success": False}
        if done_cb:
            done_cb(result)

    t = threading.Thread(target=_run, daemon=True, name="jarvis-coding-agent")
    t.start()
    return t
