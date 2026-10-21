"""
AP exam harness — puts a question on screen and says only "answer this".

The question text NEVER goes through the API. If JARVIS answers it, he read it
off the screen with his own eyes. Lives in the repo rather than /tmp because a
reboot already ate this once.

Usage:  python testdata/ap_exam.py testdata/ap_bank.json out.json ["prompt"]
"""

import asyncio
import html
import json
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import websockets

PORT = 8799
URL = f"http://127.0.0.1:{PORT}/q"
WS = "ws://127.0.0.1:8765/ws"
LOG = Path("/tmp/jarvis_server.log")

# Selection is reported by the page itself. Reading the DOM over AppleScript needs
# Chrome's "Allow JavaScript from Apple Events" enabled by hand, and grading a
# click on JARVIS's own say-so proves nothing.
_STATE = {"selected": "NONE", "html": "<h1>no question</h1>"}

# Sized to fit one viewport. An earlier version pushed choices C and D below the
# fold, and JARVIS correctly refused to answer what he couldn't see — so the exam
# was grading the CSS, not him.
TPL = """<!doctype html><meta charset="utf-8"><style>
*{{box-sizing:border-box}}
body{{margin:0;padding:20px 36px;font:400 19px/1.38 Georgia,serif;background:#fff;color:#111}}
.bar{{display:flex;justify-content:space-between;border-bottom:2px solid #1a4480;
     padding-bottom:7px;margin-bottom:14px}}
.s{{font:600 19px Georgia;color:#1a4480}}.m{{font:400 14px Georgia;color:#666}}
.n{{font:700 16px Georgia;margin-bottom:8px}}
.stem{{margin-bottom:18px;white-space:pre-wrap}}
pre{{font:15px Menlo,monospace;background:#f4f4f4;padding:9px;white-space:pre;
    border-left:4px solid #1a4480}}
ol{{list-style:none;padding:0;margin:0}}li{{margin-bottom:9px;font:400 18px/1.32 Georgia,serif}}
label{{display:flex;gap:16px;align-items:flex-start;cursor:pointer;padding:5px 9px;border-radius:10px}}
label:hover{{background:#f2f6fb}}
.L{{display:inline-flex;align-items:center;justify-content:center;min-width:34px;height:34px;
   border:2px solid #333;border-radius:50%;font-weight:700;flex:0 0 auto}}
input{{position:absolute;opacity:0}}
input:checked+.L{{background:#1a4480;color:#fff;border-color:#1a4480}}
</style><div class="bar"><div class="s">{subject}</div>
<div class="m">Section I &middot; Multiple Choice</div></div>
<div class="n">Question {n}</div><div class="stem">{stem}</div><ol>{choices}</ol>
<script>
document.addEventListener("change", function(e){{
  if (e.target && e.target.name === "ans") fetch("/select?v=" + e.target.value);
}});
</script>"""


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/select"):
            _STATE["selected"] = self.path.split("=", 1)[1] if "=" in self.path else "NONE"
            self.send_response(204); self.end_headers(); return
        body = _STATE["html"].encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _Srv(HTTPServer):
    allow_reuse_address = True


def start_server():
    srv = _Srv(("127.0.0.1", PORT), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def render(q, n):
    stem = html.escape(q["question"])
    if any(t in q["question"] for t in ("public ", "int ", "System.out", "class ")):
        stem = f"<pre>{stem}</pre>"
    choices = "".join(
        f'<li><label><input type="radio" name="ans" value="{L}">'
        f'<span class="L">{L}</span><span>{html.escape(q["choices"][L])}</span></label></li>'
        for L in "ABCD")
    _STATE["html"] = TPL.format(subject=html.escape(q["subject"]), n=n,
                                stem=stem, choices=choices)


def show(first):
    if first:
        script = f'''tell application "Google Chrome"
  activate
  set w to make new window
  set URL of active tab of w to "{URL}"
  set bounds of w to {{0, 0, 1440, 900}}
end tell'''
    else:
        script = f'''tell application "Google Chrome"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      if URL of t starts with "{URL}" then
        tell t to reload
        set index of w to 1
        return
      end if
    end repeat
  end repeat
end tell'''
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=25)


def _logsize():
    try:
        return LOG.stat().st_size
    except OSError:
        return 0


def _tools_since(off):
    try:
        with LOG.open(errors="ignore") as f:
            f.seek(off)
            return re.findall(r"⚙\s+(\w+)\(", f.read())
    except OSError:
        return []


async def ask(ws, prompt, timeout=150):
    off = _logsize()
    await ws.send(json.dumps({"type": "message", "text": prompt}))
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        except asyncio.TimeoutError:
            break
        if m.get("type") == "done":
            return m.get("full_text", ""), _tools_since(off)
    return "", _tools_since(off)


async def swallow_greeting(ws, timeout=45):
    """The greeting is Claude-generated and arrives seconds late; draining for a
    fixed moment races it and grades the greeting as the answer to question 1."""
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        except asyncio.TimeoutError:
            return
        if m.get("type") == "done":
            return


def extract(reply, q):
    if not reply:
        return None
    m = re.search(r"(?:answer|choice|option|it'?s|pick|go with)\D{0,12}\b([ABCD])\b", reply, re.I)
    if m:
        return m.group(1).upper()
    m = re.match(r"\s*\(?([ABCD])[).:\s]", reply)
    if m:
        return m.group(1).upper()
    for L in "ABCD":
        c = q["choices"][L].strip().rstrip(".")
        if len(c) > 12 and c.lower() in reply.lower():
            return L
    m = re.search(r"\b([ABCD])\b", reply)
    return m.group(1).upper() if m else None


async def main():
    qs = json.loads(Path(sys.argv[1]).read_text())["questions"]
    prompt = sys.argv[3] if len(sys.argv) > 3 else "answer this"
    start_server()
    results = []
    ws = await websockets.connect(WS, max_size=None)
    await swallow_greeting(ws)
    for i, q in enumerate(qs, 1):
        _STATE["selected"] = "NONE"
        render(q, i)
        show(first=(i == 1))
        await asyncio.sleep(2.5)
        reply, tools = await ask(ws, prompt)
        got = extract(reply, q)
        ok = got == q["answer"]
        head = reply.strip()[:60].lower()
        upfront = bool(re.search(r"answer is\s*\(?[abcd]\b", head))
        results.append({"subject": q["subject"], "expected": q["answer"], "got": got,
                        "ok": ok, "selected": _STATE["selected"], "upfront": upfront,
                        "words": len(reply.split()), "reply": reply, "tools": tools})
        sel = "OK" if _STATE["selected"] == q["answer"] else f"no({_STATE['selected']})"
        print(f"  [{'PASS' if ok else 'FAIL'}] {q['subject'][:26]:28} key={q['answer']} "
              f"said={got} sel={sel} first={'Y' if upfront else 'N'} w={len(reply.split())}")
        print(f"         \"{reply.strip()[:100]}\"")
    await ws.close()
    n = len(results)
    print(f"\n  CORRECT   : {sum(r['ok'] for r in results)}/{n}")
    print(f"  SELECTED  : {sum(r['selected'] == r['expected'] for r in results)}/{n}")
    print(f"  ANSWER 1st: {sum(r['upfront'] for r in results)}/{n}")
    print(f"  avg words : {sum(r['words'] for r in results)/max(n,1):.0f}")
    Path(sys.argv[2]).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
