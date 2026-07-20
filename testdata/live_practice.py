"""The real user path: say it over the websocket, watch what happens."""
import asyncio, json, re, sys, time, urllib.request
from pathlib import Path
import websockets
LOG = Path("/tmp/jarvis_server.log")

def logsize():
    try: return LOG.stat().st_size
    except OSError: return 0

def tools_since(off):
    with LOG.open(errors="ignore") as f:
        f.seek(off); return re.findall(r"⚙\s+(\w+)\(", f.read())

async def say(ws, text, timeout=180):
    off = logsize()
    await ws.send(json.dumps({"type": "message", "text": text}))
    loop = asyncio.get_event_loop(); end = loop.time() + timeout
    while loop.time() < end:
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        except asyncio.TimeoutError:
            break
        if m.get("type") == "done":
            return m.get("full_text",""), tools_since(off)
    return "", tools_since(off)

async def main():
    async with websockets.connect("ws://127.0.0.1:8765/ws", max_size=None) as ws:
        # drain the startup greeting
        try:
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=40))
                if m.get("type") == "done": break
        except asyncio.TimeoutError: pass

        for prompt in ["quiz me on the quadratic formula, 4 questions",
                       "show me the answers"]:
            print(f"\n  > {prompt}")
            t0 = time.time()
            reply, tools = await say(ws, prompt)
            print(f"    tools : {tools or '(none)'}   {time.time()-t0:.0f}s")
            print(f"    JARVIS: {reply.strip()[:190]}")
            try:
                html = urllib.request.urlopen("http://127.0.0.1:8787/test", timeout=4).read().decode()
                qs = html.count('class="q"')
                print(f"    page  : {qs} questions served, "
                      f"answers {'REVEALED' if '__AUTO__=true' in html else 'hidden'}")
            except Exception as e:
                print(f"    page  : not reachable ({type(e).__name__})")
asyncio.run(main())
