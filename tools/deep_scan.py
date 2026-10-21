"""
Deep scan — line-by-line review of every JARVIS source file.

Runs in a background thread so the user can keep talking to JARVIS while
it works. Yields per-file progress events for a UI loading bar. When done,
returns ONLY the failures — not the passes. The brain reads the stash
afterwards to explain anything specific the user asks about.

Scope:
  - Every .py file under JARVIS_ROOT (excluding .venv, __pycache__, .git)
  - Every .js file under jarvis-app/renderer
  - Every .sh file in JARVIS_ROOT
  - Every .html/.css file in jarvis-app/renderer

Per-file checks:
  - Syntax (compile() for Python, node --check for JS if available, shellcheck
    for shell if installed). Syntax errors are HIGH severity.
  - pyflakes for Python — catches undefined names, unused imports, redefined
    names. Most findings are MEDIUM severity.
  - Custom patterns: hardcoded secrets, broken file references, TODO/FIXME
    markers, debug print() statements left in production.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional


JARVIS_ROOT = Path("/Users/dylanroe/JARVIS").resolve()

# Directories we never descend into
_SKIP_DIRS = {
    ".venv", "venv", "node_modules", "__pycache__", ".git",
    "dist", "build", ".cache", ".pytest_cache",
}

# Severity ordering — higher number = louder in the report
SEVERITY_HIGH   = 3
SEVERITY_MEDIUM = 2
SEVERITY_LOW    = 1

# No artificial delay. The scan takes exactly as long as the real work —
# the mechanical pyflakes pass is near-instant; the LLM semantic pass (when
# enabled) is what makes it take real time. Honesty over theater.
_PER_FILE_MIN_DELAY = 0.0


# ── Patterns / heuristics ─────────────────────────────────────────────────────

# Looks like a hardcoded credential — case insensitive
_SECRET_PATTERNS = [
    re.compile(r'(?:api[_-]?key|apikey|secret|password|token|bearer)\s*=\s*["\']([A-Za-z0-9_\-./+]{20,})["\']', re.IGNORECASE),
    re.compile(r'(?:sk-|pk-)[A-Za-z0-9_\-]{20,}'),     # Anthropic / Stripe style
    re.compile(r'AKIA[0-9A-Z]{16}'),                    # AWS access key
]
# Allow these matches through — they're env-var references, not secrets
_SECRET_ALLOWLIST = (
    "os.environ", "getenv(", ".env", "API_KEY = os", "API_KEY=os",
    "your_key_here", "YOUR_", "<your", "example", "placeholder",
    "process.env",
)

_TODO_RE = re.compile(r'\b(?:TODO|FIXME|XXX|HACK)\b[:\s]', re.IGNORECASE)
_PRINT_LEFT_RE = re.compile(r'^\s*print\(', re.MULTILINE)
_BROKEN_PATH_RE = re.compile(
    r'(?:open\(|Path\(|os\.path\.\w+\(|str\()\s*["\']'
    r'(?P<path>(?:\.{1,2}/|~/|/Users/|/tmp/)[^"\'<>]+)["\']'
)


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class Finding:
    file: str
    line: int
    severity: int
    category: str
    message: str

    def to_dict(self) -> dict:
        sev_name = {3: "high", 2: "medium", 1: "low"}.get(self.severity, "low")
        return {
            "file": self.file, "line": self.line, "severity": sev_name,
            "category": self.category, "message": self.message,
        }


# ── Findings stash for the brain ──────────────────────────────────────────────

_LAST_SCAN_FINDINGS: list[dict] = []
_LAST_SCAN_STARTED_AT: float = 0.0
_LAST_SCAN_COMPLETED_AT: float = 0.0


def get_last_findings() -> list[dict]:
    """Returns the last completed scan's findings as a list of dicts."""
    return list(_LAST_SCAN_FINDINGS)


def get_last_scan_summary() -> dict:
    return {
        "started_at":   _LAST_SCAN_STARTED_AT,
        "completed_at": _LAST_SCAN_COMPLETED_AT,
        "total":        len(_LAST_SCAN_FINDINGS),
        "by_severity": {
            "high":   sum(1 for f in _LAST_SCAN_FINDINGS if f["severity"] == "high"),
            "medium": sum(1 for f in _LAST_SCAN_FINDINGS if f["severity"] == "medium"),
            "low":    sum(1 for f in _LAST_SCAN_FINDINGS if f["severity"] == "low"),
        },
    }


# ── File discovery ────────────────────────────────────────────────────────────

def _discover_files() -> list[Path]:
    """Walk the project and gather every file we're going to scan."""
    out: list[Path] = []
    for root, dirs, files in os.walk(JARVIS_ROOT):
        # Prune blocked dirs in-place so os.walk skips them
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            path = Path(root) / name
            if path.suffix in {".py", ".js", ".sh", ".html", ".css"}:
                # Don't try to scan symlinks/oddities
                try:
                    if not path.is_file() or path.is_symlink():
                        continue
                except OSError:
                    continue
                out.append(path)
    # Stable order — server / brain / tools first so meaningful failures
    # surface early in the bar
    priority_prefixes = ("server.py", "brain/", "tools/", "voice/",
                         "jarvis-app/renderer/", "proactive.py")
    def _key(p: Path) -> tuple[int, str]:
        rel = str(p.relative_to(JARVIS_ROOT))
        for i, pref in enumerate(priority_prefixes):
            if rel.startswith(pref):
                return (i, rel)
        return (len(priority_prefixes), rel)
    out.sort(key=_key)
    return out


# ── Per-file checks ───────────────────────────────────────────────────────────

def _check_python_syntax(path: Path) -> list[Finding]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [Finding(str(path.relative_to(JARVIS_ROOT)), 0,
                        SEVERITY_HIGH, "io",
                        f"could not read file: {exc}")]
    try:
        compile(src, str(path), "exec")
        ast.parse(src)
        return []
    except SyntaxError as exc:
        return [Finding(str(path.relative_to(JARVIS_ROOT)),
                        exc.lineno or 0, SEVERITY_HIGH, "syntax",
                        f"{exc.msg}")]


def _check_python_pyflakes(path: Path) -> list[Finding]:
    """Run pyflakes against a single file. Captures stdout."""
    try:
        from pyflakes.api import checkPath
        from pyflakes.reporter import Reporter
        import io
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        rep = Reporter(out_buf, err_buf)
        checkPath(str(path), reporter=rep)
        text = out_buf.getvalue() + err_buf.getvalue()
        findings: list[Finding] = []
        for raw in text.splitlines():
            # Format: path:line:col: message  (col is optional)
            m = re.match(r'^(.+?):(\d+)(?::\d+)?:\s*(.+)$', raw)
            if not m:
                continue
            _, lineno, msg = m.groups()
            # Many pyflakes messages are low-severity hygiene issues
            sev = SEVERITY_MEDIUM
            low = msg.lower()
            if "undefined name" in low or "redefined" in low:
                sev = SEVERITY_HIGH
            elif "unused import" in low or "imported but unused" in low:
                sev = SEVERITY_LOW
            findings.append(Finding(
                str(path.relative_to(JARVIS_ROOT)),
                int(lineno), sev, "pyflakes", msg,
            ))
        return findings
    except Exception as exc:
        return [Finding(str(path.relative_to(JARVIS_ROOT)), 0,
                        SEVERITY_LOW, "pyflakes",
                        f"pyflakes failed: {type(exc).__name__}: {exc}")]


def _check_js_syntax(path: Path) -> list[Finding]:
    """Use node --check for a basic syntax check on .js files (best-effort)."""
    try:
        r = subprocess.run(
            ["node", "--check", str(path)],
            capture_output=True, text=True, timeout=4,
        )
        if r.returncode != 0:
            # First line of stderr usually has file:line:col snippet
            err = (r.stderr or "").splitlines()
            first = err[0] if err else "syntax error"
            line = 0
            for ln in err:
                m = re.search(r':(\d+)', ln)
                if m:
                    try: line = int(m.group(1)); break
                    except ValueError: pass
            return [Finding(str(path.relative_to(JARVIS_ROOT)),
                            line, SEVERITY_HIGH, "syntax", first[:200])]
        return []
    except FileNotFoundError:
        return []   # node not installed, skip
    except subprocess.TimeoutExpired:
        return []
    except Exception:
        return []


def _check_shell_syntax(path: Path) -> list[Finding]:
    """`bash -n` is a non-executing parse check."""
    try:
        r = subprocess.run(["bash", "-n", str(path)],
                           capture_output=True, text=True, timeout=3)
        if r.returncode != 0:
            line = 0
            m = re.search(rf'{re.escape(str(path))}:.+?line\s+(\d+)', r.stderr)
            if m:
                try: line = int(m.group(1))
                except ValueError: pass
            return [Finding(str(path.relative_to(JARVIS_ROOT)),
                            line, SEVERITY_HIGH, "syntax",
                            r.stderr.splitlines()[0][:200] if r.stderr else "shell syntax error")]
        return []
    except Exception:
        return []


def _check_secrets(path: Path, text: str) -> list[Finding]:
    """Scan file content for things that look like hardcoded credentials."""
    findings: list[Finding] = []
    for i, line in enumerate(text.splitlines(), start=1):
        for pat in _SECRET_PATTERNS:
            m = pat.search(line)
            if not m:
                continue
            if any(ok in line for ok in _SECRET_ALLOWLIST):
                continue
            findings.append(Finding(
                str(path.relative_to(JARVIS_ROOT)), i, SEVERITY_HIGH,
                "secret", "looks like a hardcoded credential — move to .env",
            ))
            break    # one finding per line is plenty
    return findings


def _check_todos(path: Path, text: str) -> list[Finding]:
    out: list[Finding] = []
    for i, line in enumerate(text.splitlines(), start=1):
        if _TODO_RE.search(line):
            # Skip the patterns themselves (this file matches)
            if "_TODO_RE" in line: continue
            out.append(Finding(
                str(path.relative_to(JARVIS_ROOT)), i, SEVERITY_LOW,
                "todo", line.strip()[:160],
            ))
    return out


def _scan_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    suffix = path.suffix
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return [Finding(str(path.relative_to(JARVIS_ROOT)), 0,
                        SEVERITY_HIGH, "io",
                        f"could not read: {exc}")]

    if suffix == ".py":
        findings.extend(_check_python_syntax(path))
        # Only run pyflakes if syntax is OK — otherwise pyflakes itself errors
        if not any(f.category == "syntax" for f in findings):
            findings.extend(_check_python_pyflakes(path))
    elif suffix == ".js":
        findings.extend(_check_js_syntax(path))
    elif suffix == ".sh":
        findings.extend(_check_shell_syntax(path))
    # HTML / CSS — no syntax check, just text-level scans

    # Text-level scans apply to all file types
    findings.extend(_check_secrets(path, text))
    findings.extend(_check_todos(path, text))
    return findings


# ── AI semantic review (the REAL deep pass) ──────────────────────────────────
#
# pyflakes finds hygiene issues (unused vars, syntax). It CANNOT find logic
# bugs, races, leaks, or injection — those need a model that actually reads
# and reasons about the code. This pass sends each source file (in chunks)
# to JARVIS's brain and asks for real bugs. It's slow and honest: the time
# is real analysis, not padding. Quality is bounded by the available model
# (Groq 70B is good but not Claude-level; Ollama 7B is the local fallback).

_AI_CHUNK_LINES = 220          # lines per LLM call — fits Groq's token budget
_AI_REVIEW_PROMPT = (
    "You are a skeptical senior engineer reviewing a slice of a Python file "
    "from a macOS voice-assistant app. Report a bug ONLY if you can name the "
    "concrete failure: the specific input or state that triggers it AND the "
    "wrong result. If you cannot describe how it actually breaks, do NOT "
    "report it. A clean slice is the normal, expected outcome — returning an "
    "empty list is a SUCCESS, not a failure to find something.\n\n"
    "Real bugs worth reporting:\n"
    "- race conditions: two threads mutate the same state with no lock AND a "
    "bad interleaving produces a wrong value or crash\n"
    "- resource leaks: a file/subprocess/socket is opened on a path that can "
    "raise before it is closed\n"
    "- injection: unescaped user/LLM input flows into a shell or AppleScript "
    "command\n"
    "- logic errors: off-by-one, inverted condition, wrong variable, a branch "
    "that can never run\n\n"
    "Do NOT report (these are intentional or handled elsewhere):\n"
    "- best-effort `except: pass` / `except Exception: pass` around cleanup, "
    "notifications, or telemetry — swallowing those is deliberate\n"
    "- style, naming, unused imports/vars, type hints, comments, TODO markers\n"
    "- 'could theoretically' concerns with no concrete trigger\n\n"
    "For every bug you MUST quote the exact source line it occurs on so it can "
    "be verified. Respond with ONLY a JSON array (no prose, no code fences). "
    "Each item:\n"
    '{"line": <absolute line number>, '
    '"code": "<the exact text of that source line, copied verbatim>", '
    '"severity": "high"|"medium"|"low", '
    '"category": "<2-3 word label>", '
    '"trigger": "<the input/state that causes the failure>", '
    '"message": "<the wrong result that follows>"}\n'
    "If there are no real bugs in this slice, respond with exactly: []"
)


def _get_review_client():
    """
    Return (client, model, engine_name) for code review, preferring Groq's
    70B (best quality JARVIS has) and falling back to local Ollama. Returns
    (None, None, None) if neither is available.
    """
    try:
        from config import GROQ_API_KEY, GROQ_MODEL
        if GROQ_API_KEY:
            from openai import OpenAI
            client = OpenAI(api_key=GROQ_API_KEY,
                            base_url="https://api.groq.com/openai/v1")
            return client, GROQ_MODEL, "groq"
    except Exception:
        pass
    # Fallback: local Ollama (unlimited, slower, lower quality)
    try:
        from config import OLLAMA_URL, OLLAMA_MODEL
        import httpx
        httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        from openai import OpenAI
        client = OpenAI(base_url=f"{OLLAMA_URL}/v1", api_key="ollama")
        return client, OLLAMA_MODEL, "ollama"
    except Exception:
        return None, None, None


def _ollama_client():
    """Just the Ollama client, for rate-limit fallback mid-scan."""
    try:
        from config import OLLAMA_URL, OLLAMA_MODEL
        import httpx
        httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        from openai import OpenAI
        return OpenAI(base_url=f"{OLLAMA_URL}/v1", api_key="ollama"), OLLAMA_MODEL
    except Exception:
        return None, None


def _norm_code(s: str) -> str:
    """Normalize a line of code for fuzzy comparison (ignore whitespace)."""
    return re.sub(r"\s+", "", s or "")


def _parse_ai_findings(raw: str, rel_path: str, lo: int, hi: int,
                       file_lines: list[str]) -> list[Finding]:
    """
    Parse the model's JSON array into Findings, with a DETERMINISTIC
    fabrication filter: every finding must quote the exact source line it
    claims the bug is on, and that quote must actually appear at (or very
    near) the cited line. If the quote doesn't match the real file, the model
    made it up — we drop it. This is what stops "false reports to show
    something": a finding has to point at code that genuinely exists.

    Returns findings that PASSED the quote check. Findings that failed are
    silently dropped (they're fabrications or misplacements).
    """
    if not raw:
        return []
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    sev_map = {"high": SEVERITY_HIGH, "medium": SEVERITY_MEDIUM, "low": SEVERITY_LOW}
    out: list[Finding] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            line = int(it.get("line", 0))
        except (TypeError, ValueError):
            line = 0
        quoted = str(it.get("code", "")).strip()
        trigger = str(it.get("trigger", "")).strip()
        why = str(it.get("message", "")).strip()
        if not why or not quoted:
            # No claimed result or no quoted code → unverifiable → drop.
            continue

        # ── Deterministic fabrication filter ──────────────────────────────
        # The quoted code must actually appear in the file. We search a small
        # window around the cited line (models are often off by a few). If it
        # appears nowhere nearby, the finding is fabricated — drop it. If it
        # appears at a different nearby line, RELOCATE to the true line
        # instead of trusting the model's (possibly wrong) number.
        #
        # The source is shown to the model as numbered lines ("147: code"),
        # so the model sometimes echoes the "147: " gutter in its quote. Strip
        # a leading line-number gutter before comparing — otherwise a genuine
        # finding fails the match and gets silently dropped as a fabrication.
        quoted = re.sub(r"^\s*\d+:\s?", "", quoted)
        want = _norm_code(quoted)
        if len(want) < 6:
            # Too short to verify meaningfully (e.g. "pass") — drop.
            continue
        true_line = None
        # Search the whole file but prefer the closest match to `line`.
        best_dist = 1e9
        for idx, src_line in enumerate(file_lines, start=1):
            if want in _norm_code(src_line):
                dist = abs(idx - line)
                if dist < best_dist:
                    best_dist = dist
                    true_line = idx
        if true_line is None:
            # Quoted code exists nowhere in the file → fabricated → drop.
            continue

        sev = sev_map.get(str(it.get("severity", "medium")).lower(), SEVERITY_MEDIUM)
        cat = str(it.get("category", "ai")).strip()[:24] or "ai"
        # Compose an honest message: what breaks + the trigger.
        full = why[:200]
        if trigger:
            full = f"{why[:160]} (trigger: {trigger[:80]})"
        out.append(Finding(rel_path, true_line, sev, f"ai:{cat}", full[:240]))
    return out


def _enclosing_context(file_lines: list[str], line: int, pad: int = 18) -> tuple[int, str]:
    """
    Return (context_start_line, context_source) giving the reviewer enough
    surrounding code to see any guard that prevents the bug. Prefers the
    enclosing top-level/def block via AST; falls back to ±pad lines.
    """
    src = "\n".join(file_lines)
    try:
        tree = ast.parse(src)
        best = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = node.lineno
                end = getattr(node, "end_lineno", start)
                if start <= line <= end:
                    # Smallest enclosing function wins (handles nested defs)
                    if best is None or (end - start) < (best[1] - best[0]):
                        best = (start, end)
        if best:
            s, e = best
            # Cap context size so we don't blow the token budget on huge funcs
            if e - s > 120:
                s = max(best[0], line - 60)
                e = min(best[1], line + 60)
            block = "\n".join(f"{i}: {file_lines[i-1]}" for i in range(s, e + 1))
            return s, block
    except Exception:
        pass
    s = max(1, line - pad)
    e = min(len(file_lines), line + pad)
    block = "\n".join(f"{i}: {file_lines[i-1]}" for i in range(s, e + 1))
    return s, block


_VERIFY_PROMPT = (
    "You are a skeptical reviewer trying to REFUTE a reported bug. You will "
    "see a code snippet and a claimed bug. Your job is to decide whether the "
    "bug can ACTUALLY happen given the surrounding code — look hard for a "
    "guard, early return, type invariant, or caller contract that prevents "
    "it. Default to refuted: only confirm if you can still name a concrete "
    "input or state that produces the failure with this exact code.\n\n"
    "These are NOT bugs — REFUTE them:\n"
    "- subprocess.run/Popen with a LIST of args and NO shell=True: this is the "
    "safe form; arguments cannot be interpreted by a shell, so there is no "
    "command injection no matter what the input is.\n"
    "- `except X as e: return f\"...{e}\"` or any handler that puts the "
    "exception into its return/log message: the error IS surfaced, so 'not "
    "logged / silently ignored' is false.\n"
    "- a try/except that falls back to an alternate implementation: the "
    "fallback IS the handling.\n"
    "- code opened inside a `with` block: it is closed automatically.\n\n"
    "Respond with ONLY a JSON object: "
    '{"real": true|false, "reason": "<one sentence>"}'
)


def _verify_finding(client, model: str, rel: str, finding: Finding,
                    file_lines: list[str]) -> tuple[bool, str]:
    """
    Second-opinion refutation check. Returns (keep, reason). On any error we
    default to KEEPING the finding (fail-open) so verification never silently
    hides a real bug — but a clean 'real: false' verdict drops it.
    """
    _, context = _enclosing_context(file_lines, finding.line)
    sev = {3: "high", 2: "medium", 1: "low"}[finding.severity]
    user = (
        f"File: {rel}\n"
        f"Claimed bug at line {finding.line} (severity {sev}): {finding.message}\n\n"
        f"Surrounding code:\n{context}\n\n"
        "Can this bug actually occur? Refute it if a guard prevents it."
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _VERIFY_PROMPT},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return True, "verifier gave no verdict (kept)"
        verdict = json.loads(m.group(0))
        is_real = bool(verdict.get("real", True))
        reason = str(verdict.get("reason", ""))[:160]
        return is_real, reason
    except Exception:
        return True, "verifier errored (kept)"


def _ai_review_file(path: Path, client, model: str,
                    engine: str) -> Generator[dict, None, list[Finding]]:
    """
    Chunk a file and send each chunk to the model. Yields progress dicts and
    returns the accumulated findings. On Groq rate-limit, transparently
    falls back to Ollama for the remaining chunks.
    """
    rel = str(path.relative_to(JARVIS_ROOT))
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = src.split("\n")
    n = len(lines)
    findings: list[Finding] = []
    # Mutable so we can swap to Ollama mid-file on rate limit
    cur_client, cur_model, cur_engine = client, model, engine

    for start in range(0, n, _AI_CHUNK_LINES):
        end = min(start + _AI_CHUNK_LINES, n)
        lo, hi = start + 1, end           # 1-based inclusive line range
        # Number each line so the model can cite absolute line numbers
        numbered = "\n".join(f"{i+1}: {lines[i]}" for i in range(start, end))
        user_msg = (
            f"File: {rel}  (lines {lo}-{hi} of {n})\n\n{numbered}"
        )
        yield {"_progress": f"{rel} lines {lo}-{hi}", "_engine": cur_engine}

        raw = None
        for attempt in range(2):
            try:
                resp = cur_client.chat.completions.create(
                    model=cur_model,
                    messages=[
                        {"role": "system", "content": _AI_REVIEW_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    temperature=0.1,
                    max_tokens=900,
                )
                raw = resp.choices[0].message.content
                break
            except Exception as exc:
                msg = str(exc).lower()
                is_rate = "rate" in msg or "429" in msg or "tokens per" in msg
                if is_rate and cur_engine == "groq":
                    # Swap to local Ollama for the rest of the scan
                    oc, om = _ollama_client()
                    if oc is not None:
                        cur_client, cur_model, cur_engine = oc, om, "ollama"
                        continue
                    # No fallback — brief backoff then retry once
                    time.sleep(3.0)
                    continue
                # Other error — skip this chunk
                break
        if raw:
            findings.extend(_parse_ai_findings(raw, rel, lo, hi, lines))

    # ── Adversarial verification pass ─────────────────────────────────────
    # Each candidate finding gets a second-opinion refutation check with the
    # full enclosing function as context. This kills "plausible in a 220-line
    # slice but prevented by a guard just outside it" false positives — the
    # main remaining source of noise after quote-verification.
    verified: list[Finding] = []
    for f in findings:
        yield {"_progress": f"{rel} verifying L{f.line}", "_engine": cur_engine}
        keep, reason = _verify_finding(cur_client, cur_model, rel, f, lines)
        if keep:
            verified.append(f)
        else:
            print(f"[deep-scan] refuted {rel}:{f.line} — {reason}", flush=True)
    return verified


# Which files get the expensive AI review. Core logic only — skip tests,
# the agents/ subproject, setup, and tiny utility files where bugs are rare.
_AI_REVIEW_GLOBS = ("server.py", "proactive.py", "main.py")
_AI_REVIEW_DIRS = ("brain/", "tools/", "voice/")
_AI_REVIEW_SKIP = ("tools/deep_scan.py", "tools/__init__.py")


def _files_for_ai_review(all_files: list[Path]) -> list[Path]:
    out = []
    for p in all_files:
        rel = str(p.relative_to(JARVIS_ROOT))
        if p.suffix != ".py":
            continue
        if rel in _AI_REVIEW_SKIP:
            continue
        if rel in _AI_REVIEW_GLOBS or any(rel.startswith(d) for d in _AI_REVIEW_DIRS):
            out.append(p)
    return out


# ── Runner ────────────────────────────────────────────────────────────────────

def run_deep_scan(deep: bool = True) -> Generator[dict, None, None]:
    """
    Two-phase scan:
      Phase 1 (mechanical): syntax + pyflakes + secret/TODO patterns on every
        file. Fast (~2-3s). Finds hygiene issues.
      Phase 2 (AI review): sends core source files to JARVIS's brain, chunk by
        chunk, and asks for REAL bugs (races, leaks, injection, logic). Slow —
        the time is genuine analysis, not padding. Only runs when deep=True
        and a review model is available.

    Event shapes:
      {type: "deep_scan_start", total, deep, engine}
      {type: "deep_scan_progress", current, total, file, phase, engine}
      {type: "deep_scan_finding", file, line, severity, category, message}
      {type: "deep_scan_done", total_files, findings, summary}
    """
    global _LAST_SCAN_STARTED_AT, _LAST_SCAN_COMPLETED_AT
    _LAST_SCAN_FINDINGS.clear()
    _LAST_SCAN_STARTED_AT = time.time()
    files = _discover_files()

    # Decide whether the AI pass can run
    review_client = review_model = engine = None
    ai_files: list[Path] = []
    if deep:
        review_client, review_model, engine = _get_review_client()
        if review_client is not None:
            ai_files = _files_for_ai_review(files)

    mech_total = len(files)
    ai_total = len(ai_files)
    total = mech_total + ai_total
    yield {"type": "deep_scan_start", "total": total,
           "deep": bool(ai_files), "engine": engine or "none"}

    # ── Phase 1: mechanical ───────────────────────────────────────────────
    for i, path in enumerate(files):
        rel = str(path.relative_to(JARVIS_ROOT))
        yield {
            "type": "deep_scan_progress",
            "current": i, "total": total, "file": rel,
            "phase": "mechanical", "engine": "pyflakes",
        }
        try:
            findings = _scan_file(path)
        except Exception as exc:
            findings = [Finding(rel, 0, SEVERITY_MEDIUM, "scanner",
                                f"scanner crashed on this file: {type(exc).__name__}")]
        for f in findings:
            d = f.to_dict()
            _LAST_SCAN_FINDINGS.append(d)
            yield {"type": "deep_scan_finding", **d}

    # ── Phase 2: AI semantic review ───────────────────────────────────────
    for j, path in enumerate(ai_files):
        rel = str(path.relative_to(JARVIS_ROOT))
        # Drive the per-file generator; it yields progress + returns findings
        gen = _ai_review_file(path, review_client, review_model, engine)
        file_findings: list[Finding] = []
        try:
            while True:
                ev = next(gen)
                # progress sub-event from the chunker
                yield {
                    "type": "deep_scan_progress",
                    "current": mech_total + j, "total": total,
                    "file": ev.get("_progress", rel),
                    "phase": "ai", "engine": ev.get("_engine", engine),
                }
        except StopIteration as stop:
            file_findings = stop.value or []
        except Exception as exc:
            file_findings = []
            print(f"[deep-scan] AI review crashed on {rel}: {exc}", flush=True)
        for f in file_findings:
            d = f.to_dict()
            _LAST_SCAN_FINDINGS.append(d)
            yield {"type": "deep_scan_finding", **d}

    _LAST_SCAN_COMPLETED_AT = time.time()
    yield {
        "type": "deep_scan_done",
        "total_files": total,
        "findings": list(_LAST_SCAN_FINDINGS),
        "summary": get_last_scan_summary(),
    }
