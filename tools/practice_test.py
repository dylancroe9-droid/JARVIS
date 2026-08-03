"""
Practice tests — generate, take, and reveal worked answers.

Generates real exam-style questions on a topic, then VERIFIES each answer key
before showing it: the question is re-solved from scratch, blind to the stated
key, and anything the solver disagrees with is thrown away. AI-generated practice
tests routinely ship with wrong keys, and a wrong key is worse than no practice —
you learn the wrong thing and trust it.

The test renders as a real page in the browser with clickable choices, so it can
be taken normally. "Show the answers" reveals the worked solution for each one.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

PORT = 8787
URL = f"http://127.0.0.1:{PORT}/test"

# Current test lives in memory: the page is served, not written to disk, so a
# stale file can never be shown after a new test is generated.
_STATE: dict = {"questions": [], "answers": {}, "revealed": False, "topic": ""}
_server: Optional[HTTPServer] = None
_lock = threading.Lock()


# ── Generation ────────────────────────────────────────────────────────────────

_GEN_PROMPT = """Write {n} multiple-choice questions on: {topic}

Difficulty: {level}

Rules:
- Real exam rigor, not definitions or trivia. Test understanding, not recall.
- Answerable from text alone — NO images, NO "refer to the diagram".
- Exactly one correct choice. The three distractors must be mistakes a student
  actually makes, not obvious throwaways.
- Keep each stem under 120 words.
- Vary what is tested across the set; do not ask the same idea four ways.

Keep "why" to ONE sentence — the key step, not a full derivation.

Return ONLY a JSON array, no prose:
[{{"question": "...", "choices": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
   "answer": "A", "why": "one sentence of worked reasoning"}}]"""

_VERIFY_PROMPT = """Solve this multiple-choice question independently.

{stem}

A) {A}
B) {B}
C) {C}
D) {D}

Work it out yourself. Reply with ONLY the single letter of the best answer."""


def _claude():
    from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
    import anthropic
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY), ANTHROPIC_MODEL


def _valid(q) -> bool:
    return (isinstance(q, dict)
            and {"question", "choices", "answer"} <= q.keys()
            and q.get("answer") in ("A", "B", "C", "D")
            and isinstance(q.get("choices"), dict)
            and all(k in q["choices"] for k in "ABCD"))


def _parse_questions(txt: str) -> list:
    """
    Pull questions out of the model's reply, tolerating a truncated array.

    A whole-array json.loads is all-or-nothing: one run hit the output cap
    mid-array and every question was thrown away, costing $0.12 and 79 seconds to
    return nothing. Falling back to decoding object-by-object keeps the questions
    that did come through complete.
    """
    start = txt.find("[")
    if start >= 0:
        end = txt.rfind("]") + 1
        if end > start:
            try:
                whole = json.loads(txt[start:end])
                if isinstance(whole, list):
                    return [q for q in whole if _valid(q)]
            except json.JSONDecodeError:
                pass          # truncated or malformed — salvage below
    out, dec, i = [], json.JSONDecoder(), max(start, 0)
    while True:
        i = txt.find("{", i)
        if i < 0:
            break
        try:
            obj, endpos = dec.raw_decode(txt, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if _valid(obj):
            out.append(obj)
        i = endpos
    return out


def _generate(topic: str, n: int, level: str) -> list:
    client, model = _claude()
    # Headroom so a full set isn't cut off mid-array; _parse_questions salvages
    # whatever completed if it still is.
    r = client.messages.create(
        model=model, max_tokens=min(2000 + 900 * n, 16000),
        messages=[{"role": "user",
                   "content": _GEN_PROMPT.format(n=n, topic=topic, level=level)}],
    )
    txt = "".join(b.text for b in r.content if b.type == "text")
    return _parse_questions(txt)


def _extract_answer_letter(said: str):
    """
    Pull the verifier's CHOSEN letter out of its reply.

    Taking the first standalone A/B/C/D is wrong: a verifier that reasons aloud
    ("A is tempting, but the answer is B") gets read as A, so a wrong key is kept or
    a right one dropped — which silently breaks the whole "answer keys verified"
    guarantee. An explicit declaration wins; otherwise the LAST standalone letter,
    since the conclusion comes at the end.
    """
    if not said:
        return None
    decl = re.findall(
        r"(?:answer|correct|it'?s|choose|choice|pick|go(?:ing)? with)\D{0,15}\b([ABCD])\b",
        said, re.I)
    if decl:
        return decl[-1].upper()
    letters = re.findall(r"\b([ABCD])\b", said)
    return letters[-1].upper() if letters else None


def _verify(questions: list) -> tuple:
    """Re-solve each question blind and drop any whose key doesn't hold up."""
    client, model = _claude()
    kept, dropped = [], 0
    for q in questions:
        try:
            # Thinking off + tiny cap so it emits just the letter — no reasoning to
            # misparse. The extractor is a backstop for models that add a word anyway.
            r = client.messages.create(
                model=model, max_tokens=16,
                thinking={"type": "disabled"},
                messages=[{"role": "user", "content": _VERIFY_PROMPT.format(
                    stem=q["question"], **q["choices"])}],
            )
            said = "".join(b.text for b in r.content if b.type == "text")
            if _extract_answer_letter(said) == q["answer"]:
                kept.append(q)
            else:
                dropped += 1
        except Exception:
            dropped += 1
    return kept, dropped


# ── Page ──────────────────────────────────────────────────────────────────────

_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:28px 44px;background:#0f1115;color:#e8eaed;
     font:400 17px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:1000px}
h1{font:600 24px/1.3 inherit;margin:0 0 4px}
.sub{color:#8b929e;font-size:14px;margin-bottom:26px}
.q{border-top:1px solid #232733;padding:22px 0}
.stem{margin-bottom:14px;white-space:pre-wrap}
.n{color:#5b8dee;font-weight:600;margin-right:8px}
label{display:flex;gap:13px;align-items:flex-start;padding:8px 11px;border-radius:9px;
      cursor:pointer;margin-bottom:5px}
label:hover{background:#181c25}
input{position:absolute;opacity:0}
.L{display:inline-flex;align-items:center;justify-content:center;min-width:29px;height:29px;
   border:2px solid #3a4150;border-radius:50%;font-weight:600;font-size:14px;flex:0 0 auto}
input:checked+.L{background:#5b8dee;border-color:#5b8dee;color:#fff}
.key{display:none;margin-top:12px;padding:13px 16px;background:#131a26;
     border-left:3px solid #3fb950;border-radius:0 8px 8px 0;font-size:15px}
.key b{color:#3fb950}
body.revealed .key{display:block}
body.revealed .right .L{background:#3fb950;border-color:#3fb950;color:#fff}
.bar{position:sticky;top:0;background:#0f1115;padding:14px 0;border-bottom:1px solid #232733;
     display:flex;gap:12px;align-items:center;z-index:5}
button{background:#5b8dee;color:#fff;border:0;padding:9px 18px;border-radius:8px;
       font:600 15px inherit;cursor:pointer}
button.ghost{background:#232733}
#score{color:#8b929e;font-size:15px}
"""

_JS = """
function reveal(){
  document.body.classList.add('revealed');
  var right=0, total=0;
  document.querySelectorAll('.q').forEach(function(q){
    total++;
    var key=q.dataset.key;
    q.querySelector('label[data-c="'+key+'"]').classList.add('right');
    var pick=q.querySelector('input:checked');
    if(pick && pick.value===key) right++;
  });
  document.getElementById('score').textContent = right+' / '+total+' correct';
}
document.getElementById('reveal').addEventListener('click', reveal);
if (window.__AUTO__) {
  document.querySelectorAll('.q').forEach(function(q){
    q.querySelector('input[value="'+q.dataset.key+'"]').checked = true;
  });
  reveal();
}
"""


def _render() -> str:
    qs = _STATE["questions"]
    rows = []
    for i, q in enumerate(qs, 1):
        choices = "".join(
            f'<label data-c="{L}"><input type="radio" name="q{i}" value="{L}">'
            f'<span class="L">{L}</span><span>{html.escape(q["choices"][L])}</span></label>'
            for L in "ABCD")
        why = html.escape(q.get("why", "") or "")
        rows.append(
            f'<div class="q" data-key="{q["answer"]}">'
            f'<div class="stem"><span class="n">{i}.</span>{html.escape(q["question"])}</div>'
            f'{choices}<div class="key"><b>Answer: {q["answer"]}</b> — {why}</div></div>')
    auto = "window.__AUTO__=true;" if _STATE["revealed"] else ""
    return (f'<!doctype html><meta charset="utf-8"><title>Practice — '
            f'{html.escape(_STATE["topic"])}</title><style>{_CSS}</style>'
            f'<h1>{html.escape(_STATE["topic"])}</h1>'
            f'<div class="sub">{len(qs)} questions &middot; answer keys verified</div>'
            f'<div class="bar"><button id="reveal">Show answers</button>'
            f'<span id="score"></span></div>'
            f'{"".join(rows)}<script>{auto}{_JS}</script>')


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = _render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _Server(HTTPServer):
    # Without this, a server torn down seconds ago leaves the port in TIME_WAIT and
    # the next test generation dies on "Address already in use".
    allow_reuse_address = True


def _serve() -> bool:
    """Start the page server if it isn't already up. Returns False if the port is
    held by something else — the caller must not claim the test is on screen."""
    global _server
    with _lock:
        if _server is not None:
            return True
        try:
            _server = _Server(("127.0.0.1", PORT), _H)
        except OSError:
            _server = None
            # Something already holds the port. If it's a stale copy of us it will
            # serve the current _STATE anyway, since the page is rendered on each
            # request; anything else and we genuinely cannot show the test.
            try:
                import urllib.request
                urllib.request.urlopen(URL, timeout=2).read(64)
                return True
            except Exception:
                return False
        threading.Thread(target=_server.serve_forever, daemon=True).start()
        return True


def _open_or_reload():
    script = f'''
tell application "Google Chrome"
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
  make new window
  set URL of active tab of front window to "{URL}"
end tell'''
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=25)


# ── Public tools ──────────────────────────────────────────────────────────────

def create_practice_test(topic: str, count: int = 8, difficulty: str = "AP level") -> str:
    """Generate a verified practice test on `topic` and open it in the browser."""
    count = max(1, min(int(count or 8), 25))
    qs = _generate(topic, count, difficulty or "AP level")
    if not qs:
        return f"Couldn't generate a test on '{topic}' — try naming the topic more specifically."
    kept, dropped = _verify(qs)
    if not kept:
        return (f"Generated {len(qs)} questions on '{topic}' but none passed the answer-key "
                f"check, so I'm not showing you a test I can't stand behind. Try again?")
    _STATE.update(questions=kept, topic=topic, revealed=False, answers={})
    if not _serve():
        return (f"Made {len(kept)} questions on {topic}, but port {PORT} is taken by "
                f"something else so I can't put the test on screen.")
    _open_or_reload()
    note = f" ({dropped} thrown out — the key didn't hold up)" if dropped else ""
    return (f"{len(kept)}-question test on {topic} is up{note}. "
            f"Answer keys verified. Say 'show the answers' when you want them.")


def show_test_answers() -> str:
    """Reveal the worked answer key for the current practice test."""
    if not _STATE["questions"]:
        return "No practice test open — ask me to make one first."
    _STATE["revealed"] = True
    _open_or_reload()
    return (f"Answers up, with the working for each. "
            f"{len(_STATE['questions'])} questions on {_STATE['topic']}.")
