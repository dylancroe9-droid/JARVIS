"""
Self-fix — generate and safely apply patches for confirmed deep-scan bugs.

Safety model (a bad edit could brick JARVIS, so every fix must prove itself):
  1. The LLM produces a MINIMAL surgical patch: an exact old_string → new_string
     pair, not a rewrite.
  2. We back up the file to <file>.bak, then write the patch ATOMICALLY
     (temp file + os.replace) so the original is never truncated on a failed
     write.
  3. We run the FULL test suite + an import check.
  4. If green → keep the patch. If red → restore from backup automatically.

Backup semantics: <file>.bak holds the file's state IMMEDIATELY BEFORE THIS
fix — not necessarily the pristine session original. If two fixes touch the
same file in one run, the second fix's .bak is the "original + first fix"
state. This is intentional and correct: each fix reverts to exactly its own
pre-state, so a failed later fix keeps the earlier good fixes. Don't rely on
.bak to recover the very first original after multiple same-file fixes — use
git for that.

Nothing is ever applied without passing the tests. The scan finds bugs; this
module fixes them only when the fix is provably safe.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

JARVIS_ROOT = Path("/Users/dylanroe/JARVIS").resolve()


# ── Fix generation ────────────────────────────────────────────────────────────

_FIX_PROMPT = (
    "You are fixing ONE specific bug in a Python file. Produce the SMALLEST "
    "possible change — a surgical edit, not a rewrite. You will be given the "
    "bug and the exact source around it.\n\n"
    "Respond with ONLY a JSON object:\n"
    '{"old_string": "<exact text to replace, copied verbatim from the source, '
    'including indentation — must be unique in the file>", '
    '"new_string": "<the corrected replacement>", '
    '"explanation": "<one sentence: what the fix does>"}\n\n'
    "Rules:\n"
    "- old_string MUST appear verbatim in the source (exact whitespace).\n"
    "- Make old_string long enough to be unique (include surrounding lines).\n"
    "- Change ONLY what fixes this bug. Do not reformat or touch other code.\n"
    "- Preserve existing indentation and style exactly.\n"
    "- NEVER collapse statements onto one line. No semicolons. If you add an "
    "`if` guard, put its body on the next line, properly indented, and keep "
    "any following statements (like `return`) on their own separate lines at "
    "the correct indent level. A `raise`/`return` must never share a line with "
    "an `if`."
)


def _get_client():
    """Groq first (best quality), Ollama fallback."""
    try:
        from config import GROQ_API_KEY, GROQ_MODEL
        if GROQ_API_KEY:
            from openai import OpenAI
            return (OpenAI(api_key=GROQ_API_KEY,
                           base_url="https://api.groq.com/openai/v1"),
                    GROQ_MODEL)
    except Exception:
        pass
    try:
        from config import OLLAMA_URL, OLLAMA_MODEL
        import httpx
        httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        from openai import OpenAI
        return OpenAI(base_url=f"{OLLAMA_URL}/v1", api_key="ollama"), OLLAMA_MODEL
    except Exception:
        return None, None


def _context_around(file_lines: list[str], line: int, pad: int = 12) -> str:
    s = max(1, line - pad)
    e = min(len(file_lines), line + pad)
    return "\n".join(f"{i}: {file_lines[i-1]}" for i in range(s, e + 1))


def generate_fix(rel_path: str, finding: dict) -> Optional[dict]:
    """
    Ask the LLM for a surgical patch for one finding. Returns
    {old_string, new_string, explanation} or None.
    """
    client, model = _get_client()
    if client is None:
        return None
    path = JARVIS_ROOT / rel_path
    try:
        file_lines = path.read_text(encoding="utf-8").split("\n")
    except Exception:
        return None
    line = int(finding.get("line", 0) or 0)
    context = _context_around(file_lines, line)
    user = (
        f"File: {rel_path}\n"
        f"Bug at line {line}: {finding.get('message', '')}\n\n"
        f"Source around the bug:\n{context}\n\n"
        "Produce the minimal fix."
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _FIX_PROMPT},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content or ""
    except Exception:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    old = obj.get("old_string")
    new = obj.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or old == new:
        return None
    return {
        "old_string": old,
        "new_string": new,
        "explanation": str(obj.get("explanation", ""))[:200],
    }


# ── Safe application ──────────────────────────────────────────────────────────

def _run_tests() -> tuple[bool, str]:
    """Run the test suite + import check. Returns (passed, short_output)."""
    try:
        r = subprocess.run(
            [".venv/bin/python", "-m", "pytest", "tests/", "-q",
             "--no-header", "-x"],
            cwd=str(JARVIS_ROOT), capture_output=True, text=True, timeout=180,
        )
        tail = (r.stdout or "")[-400:] + (r.stderr or "")[-200:]
        return r.returncode == 0, tail.strip()
    except subprocess.TimeoutExpired:
        return False, "tests timed out"
    except Exception as exc:
        return False, f"test runner error: {exc}"


def apply_fix_safely(rel_path: str, old_string: str,
                     new_string: str) -> tuple[bool, str]:
    """
    Apply one patch, then prove it with the test suite. If tests fail, the
    original file is restored from the .bak automatically.

    Returns (applied_and_green, detail).
    """
    path = JARVIS_ROOT / rel_path
    if not path.exists():
        return False, "file not found"
    try:
        original = path.read_text(encoding="utf-8")
    except Exception as exc:
        return False, f"could not read file: {exc}"

    count = original.count(old_string)
    if count == 0:
        return False, "old_string not found in file (fix does not apply cleanly)"
    if count > 1:
        return False, f"old_string is ambiguous ({count} matches) — skipped for safety"

    patched = original.replace(old_string, new_string, 1)
    bak = path.with_suffix(path.suffix + ".bak")
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        shutil.copy2(path, bak)                 # backup the pristine original
        # ATOMIC write: build the new content in a temp file, then os.replace
        # it over the original in one atomic operation. This means the
        # original is NEVER truncated by a failed write — if writing the temp
        # file fails (disk full, I/O error), the real file is untouched and
        # the good backup is never overwritten by corrupted content.
        tmp.write_text(patched, encoding="utf-8")
        os.replace(tmp, path)                   # atomic on POSIX
    except Exception as exc:
        # Clean up any partial temp file; the original is intact (never
        # truncated), but restore from backup as belt-and-suspenders.
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        try:
            if bak.exists():
                shutil.copy2(bak, path)
        except Exception:
            pass
        return False, f"could not write patch: {exc} (file left intact)"

    # From here the file is PATCHED on disk. Every verification step below can
    # fail EITHER by returning a falsy result OR by raising. In both cases the
    # file must be restored — a crash in the test runner or reviewer must never
    # leave modified code behind. The try/except guarantees revert-on-anything.
    def _revert(reason: str) -> tuple[bool, str]:
        try:
            shutil.copy2(bak, path)
        except Exception as rexc:
            return False, f"{reason} — AND restore failed ({rexc}); .bak at {bak}"
        return False, reason

    try:
        # Syntax first (fast), then the full suite.
        if path.suffix == ".py":
            try:
                compile(patched, str(path), "exec")
            except SyntaxError as exc:
                return _revert(f"patch broke syntax ({exc.msg}) — reverted")

        passed, out = _run_tests()
        if not passed:
            return _revert(f"tests failed — reverted. {out[-200:]}")

        # Post-fix re-review: tests only protect COVERED code. A fix to
        # untested code can be syntactically valid yet semantically wrong
        # (e.g. a `return` collapsed into an `if` so it never runs). Confirm
        # the bug is gone AND no new bug was introduced. If not, revert.
        ok_review, why = _post_fix_review(rel_path, old_string, new_string, patched)
        if not ok_review:
            return _revert(f"post-fix review rejected it — reverted. {why}")
    except Exception as exc:
        # Any unexpected failure in verification → restore, never keep.
        return _revert(f"verification crashed ({type(exc).__name__}: {exc}) — reverted")

    return True, "applied, tests pass, and fix re-verified"


_POSTFIX_PROMPT = (
    "You are checking whether a code change is a CORRECT and COMPLETE fix. You "
    "see the old code, the new code, and the surrounding context. Confirm BOTH:\n"
    "1. the new code actually fixes the described problem, and\n"
    "2. the new code introduces NO new bug (unreachable code, wrong control "
    "flow, a statement that now never runs, a changed return contract, etc.).\n\n"
    "Be strict about control flow: an `if guard: raise` that swallows a "
    "following `return`, or any statement made unreachable, is a NEW bug.\n\n"
    "Respond with ONLY a JSON object: "
    '{"good": true|false, "reason": "<one sentence>"}'
)


def _post_fix_review(rel_path: str, old_string: str, new_string: str,
                     patched_source: str) -> tuple[bool, str]:
    """Second opinion that the patch is correct + introduces no new bug."""
    client, model = _get_client()
    if client is None:
        # Fail CLOSED: never keep a fix we couldn't independently re-review.
        # (Tests alone can't catch a semantically-wrong-but-valid edit in
        # untested code — that's exactly what this second opinion is for.)
        return False, "no model available to re-review — not applied"
    # Give context: the new code plus a window of the patched file around it
    idx = patched_source.find(new_string)
    ctx = new_string
    if idx >= 0:
        lo = max(0, idx - 300)
        hi = min(len(patched_source), idx + len(new_string) + 300)
        ctx = patched_source[lo:hi]
    user = (
        f"File: {rel_path}\n\nOLD code:\n{old_string}\n\n"
        f"NEW code:\n{new_string}\n\n"
        f"Patched context:\n{ctx}\n\n"
        "Is the new code a correct, complete fix that introduces no new bug?"
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _POSTFIX_PROMPT},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return False, "reviewer gave no parseable verdict — not applied"
        v = json.loads(m.group(0))
        return bool(v.get("good", True)), str(v.get("reason", ""))[:160]
    except Exception as exc:
        # A rate-limit (429) during a scan+fix run is the COMMON case here.
        # Fail closed: if we can't get the second opinion, don't apply — better
        # to skip a fix than silently keep an unverified one and claim it's safe.
        return False, f"re-review unavailable ({type(exc).__name__}) — not applied"


def fix_findings(findings: list[dict], max_fixes: int = 5) -> list[dict]:
    """
    For each confirmed finding, generate a fix and safely apply it. Returns a
    per-finding result report. Only AI-confirmed findings (category ai:*)
    with a real line number are attempted, most-severe first.
    """
    sev_rank = {"high": 3, "medium": 2, "low": 1}
    candidates = [f for f in findings
                  if str(f.get("category", "")).startswith("ai:")
                  and int(f.get("line", 0) or 0) > 0]
    candidates.sort(key=lambda f: -sev_rank.get(f.get("severity", "low"), 1))
    results = []
    for f in candidates[:max_fixes]:
        rel = f.get("file", "")
        fix = generate_fix(rel, f)
        if not fix:
            results.append({**f, "fix_status": "no_fix_generated"})
            continue
        ok, detail = apply_fix_safely(rel, fix["old_string"], fix["new_string"])
        results.append({
            **f,
            "fix_status": "applied" if ok else "rejected",
            "fix_explanation": fix["explanation"],
            "fix_detail": detail,
        })
    return results
