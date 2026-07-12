"""
Self-inspect — safe read-only access to JARVIS's own code, dirs, and logs.

Lets the brain investigate its own state when something is broken:
  read_jarvis_file  — read a source file
  grep_jarvis       — regex search across the codebase
  list_jarvis_dir   — list a directory
  read_jarvis_log   — tail the latest captured stdout (if log file exists)

Hard safety rules:
  - All paths are resolved relative to the JARVIS root and rejected if they
    escape it (no `../../etc/passwd`).
  - Secrets are blocked: `.env`, anything ending in `.pem`, `.key`,
    `~/.jarvis_history.json` (privacy — contains conversation transcripts).
  - Binary / build dirs are blocked: `node_modules/`, `__pycache__/`,
    `.venv/`, `.git/objects/`.
  - File reads cap at 1 MB; grep returns at most 50 hits; log tail caps at
    500 lines. Designed for the brain — large dumps would blow the context.
"""

from __future__ import annotations

import re
from pathlib import Path


# ── Constants ─────────────────────────────────────────────────────────────────

JARVIS_ROOT = Path("/Users/dylanroe/JARVIS").resolve()

# Files we never let the brain read — secrets and privacy
_BLOCKED_FILE_NAMES = {
    ".env", ".env.local", ".env.production",
    "credentials.json", "credentials.yaml",
}
_BLOCKED_FILE_SUFFIXES = (".pem", ".key", ".crt", ".pyc", ".so", ".dylib")
# (Removed dead _BLOCKED_HOME_FILES set — it was never referenced. Files in the
# home dir are already outside JARVIS_ROOT and blocked by the containment check
# in _safe_path, so it added no protection while implying it did.)

# Directories we never descend into — too big or irrelevant
_BLOCKED_DIRS = {
    "node_modules", "__pycache__", ".venv", "venv",
    ".git", "dist", "build", ".cache",
}

# Caps to keep the brain from drowning in output
_MAX_FILE_BYTES   = 1_000_000     # 1 MB
_MAX_GREP_HITS    = 50
_MAX_LOG_LINES    = 500
_DEFAULT_LOG_LINES = 100

# Where the server log should be tee'd to (if start.sh is configured to)
_LOG_PATH_CANDIDATES = [
    Path("/tmp/jarvis_server.log"),
    Path("/tmp/jarvis.log"),
    JARVIS_ROOT / "jarvis.log",
]


# ── Path-safety helpers ───────────────────────────────────────────────────────

def _safe_path(path: str) -> Path | None:
    """
    Resolve `path` to an absolute Path under JARVIS_ROOT. Returns None if
    the resolved path escapes the root, hits a blocked file, or sits inside
    a blocked directory.
    """
    if not path:
        return None
    raw = path.strip()
    # Allow callers to pass either "server.py", "tools/diagnostics.py",
    # or an absolute path already under JARVIS_ROOT.
    candidate = Path(raw) if Path(raw).is_absolute() else (JARVIS_ROOT / raw)
    try:
        resolved = candidate.resolve()
    except Exception:
        return None
    # Containment check
    try:
        resolved.relative_to(JARVIS_ROOT)
    except ValueError:
        return None
    # Block by filename
    if resolved.name in _BLOCKED_FILE_NAMES:
        return None
    if resolved.suffix in _BLOCKED_FILE_SUFFIXES:
        return None
    # Block if any path component is in the blocked-dirs set
    parts = set(resolved.parts)
    if parts & _BLOCKED_DIRS:
        return None
    return resolved


def _is_text_file(path: Path) -> bool:
    """Quick heuristic to skip binaries — first chunk has no null bytes."""
    try:
        with path.open("rb") as f:
            chunk = f.read(2048)
        return b"\x00" not in chunk
    except Exception:
        return False


# ── Public tools ──────────────────────────────────────────────────────────────

def read_jarvis_file(path: str, max_bytes: int = _MAX_FILE_BYTES) -> str:
    """
    Read a JARVIS source/config file relative to the project root.
    Returns the file content, or a one-line error string starting with
    '__error__:' so the caller can detect failure without a try/except.
    """
    resolved = _safe_path(path)
    if resolved is None:
        return f"__error__: path '{path}' is blocked or outside the JARVIS directory"
    if not resolved.exists():
        return f"__error__: file '{path}' does not exist"
    if not resolved.is_file():
        return f"__error__: '{path}' is a directory — use list_jarvis_dir instead"
    if not _is_text_file(resolved):
        return f"__error__: '{path}' looks like a binary file"
    cap = min(max_bytes, _MAX_FILE_BYTES)
    try:
        with resolved.open("r", encoding="utf-8", errors="replace") as f:
            data = f.read(cap + 1)
    except Exception as exc:
        return f"__error__: read failed — {type(exc).__name__}: {exc}"
    truncated = len(data) > cap
    if truncated:
        data = data[:cap] + f"\n…[truncated at {cap} bytes]"
    rel = resolved.relative_to(JARVIS_ROOT)
    header = f"# {rel} ({resolved.stat().st_size} bytes)\n"
    return header + data


def grep_jarvis(pattern: str,
                file_glob: str = "**/*.py",
                max_results: int = _MAX_GREP_HITS,
                case_insensitive: bool = True) -> str:
    """
    Regex-search across files matching `file_glob` under JARVIS_ROOT.
    Returns up to max_results lines in `path:line:matched_text` format, or a
    one-line '__error__:' string on failure.
    """
    if not pattern:
        return "__error__: empty pattern"
    try:
        flags = re.IGNORECASE if case_insensitive else 0
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return f"__error__: bad regex — {exc}"
    cap = min(max_results, _MAX_GREP_HITS)
    out: list[str] = []
    try:
        for path in sorted(JARVIS_ROOT.glob(file_glob)):
            if len(out) >= cap:
                break
            safe = _safe_path(str(path))
            if safe is None or not safe.is_file() or not _is_text_file(safe):
                continue
            try:
                with safe.open("r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, start=1):
                        if rx.search(line):
                            rel = safe.relative_to(JARVIS_ROOT)
                            snippet = line.rstrip("\n")[:200]
                            out.append(f"{rel}:{lineno}: {snippet}")
                            if len(out) >= cap:
                                break
            except Exception:
                continue
    except Exception as exc:
        return f"__error__: grep failed — {type(exc).__name__}: {exc}"
    if not out:
        return f"No matches for {pattern!r} in {file_glob!r}."
    header = f"Found {len(out)} match{'es' if len(out) != 1 else ''} for {pattern!r}:\n"
    return header + "\n".join(out)


def list_jarvis_dir(path: str = ".") -> str:
    """
    List the immediate contents of a directory under JARVIS_ROOT. Marks
    files vs subdirectories and shows file sizes.
    """
    resolved = _safe_path(path)
    if resolved is None:
        return f"__error__: path '{path}' is blocked or outside the JARVIS directory"
    if not resolved.exists():
        return f"__error__: directory '{path}' does not exist"
    if not resolved.is_dir():
        return f"__error__: '{path}' is a file — use read_jarvis_file instead"
    entries = []
    try:
        for child in sorted(resolved.iterdir()):
            if child.name in _BLOCKED_DIRS or child.name in _BLOCKED_FILE_NAMES:
                continue
            if child.suffix in _BLOCKED_FILE_SUFFIXES:
                continue
            if child.is_dir():
                entries.append(f"  {child.name}/")
            else:
                try:
                    size = child.stat().st_size
                    entries.append(f"  {child.name}  ({size} bytes)")
                except Exception:
                    entries.append(f"  {child.name}")
    except Exception as exc:
        return f"__error__: list failed — {type(exc).__name__}: {exc}"
    rel = resolved.relative_to(JARVIS_ROOT) if resolved != JARVIS_ROOT else Path(".")
    if not entries:
        return f"# {rel}/ (empty)"
    return f"# {rel}/  ({len(entries)} entries)\n" + "\n".join(entries)


def read_jarvis_history(max_turns: int = 20) -> str:
    """
    Read the conversation history file (~/.jarvis_history.json) so JARVIS
    can answer "why did I just say that" questions by inspecting his own
    recent turns. Returns the last `max_turns` turns formatted as plain
    text.

    NOTE: explicit, separate function — not exposed through the generic
    read_jarvis_file path so the brain has to deliberately ask for it.
    """
    import json as _json
    p = Path.home() / ".jarvis_history.json"
    if not p.exists():
        return f"__error__: no history file at {p}"
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            data = _json.load(f)
    except Exception as exc:
        return f"__error__: couldn't parse history — {type(exc).__name__}: {exc}"
    if not isinstance(data, list):
        return f"__error__: unexpected history format (root is {type(data).__name__})"
    turns = data[-max_turns:] if len(data) > max_turns else data
    out = [f"# Last {len(turns)} of {len(data)} turns from ~/.jarvis_history.json"]
    for t in turns:
        if not isinstance(t, dict):
            continue
        role = t.get("role", "?")
        content = t.get("content", "")
        if isinstance(content, list):
            # Multi-part content (tool calls etc.) — flatten
            content = " ".join(str(c) for c in content)
        snippet = str(content).replace("\n", " ").strip()[:400]
        out.append(f"[{role}] {snippet}")
    return "\n".join(out)


def read_jarvis_log(lines: int = _DEFAULT_LOG_LINES) -> str:
    """
    Tail the captured JARVIS server stdout if a log file exists. Looks at:
      /tmp/jarvis_server.log
      /tmp/jarvis.log
      ~/JARVIS/jarvis.log
    Returns a clear error if no log file is configured — start.sh needs to
    tee stdout into one of these for this to work.

    Safety: for large log files, we seek to the last 2 MB rather than reading
    the entire file into memory. With ~120 chars/line, 2 MB covers ~17k lines
    which is way more than the 500-line cap on `lines`.
    """
    cap = max(1, min(lines, _MAX_LOG_LINES))
    log_file = None
    for candidate in _LOG_PATH_CANDIDATES:
        if candidate.exists() and candidate.is_file():
            log_file = candidate
            break
    if log_file is None:
        return ("__error__: no log file found. "
                "To enable, modify start.sh to tee server output to "
                "/tmp/jarvis_server.log (e.g. `python server.py 2>&1 | "
                "tee /tmp/jarvis_server.log`).")
    try:
        TAIL_BYTES = 2 * 1024 * 1024
        file_size = log_file.stat().st_size
        with log_file.open("rb") as f:
            if file_size > TAIL_BYTES:
                f.seek(file_size - TAIL_BYTES)
                # Discard the partial first line so we don't return half a record
                f.readline()
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        all_lines = text.splitlines(keepends=True)
        tail = all_lines[-cap:] if len(all_lines) > cap else all_lines
    except Exception as exc:
        return f"__error__: log read failed — {type(exc).__name__}: {exc}"
    note = " (tailed last 2 MB)" if file_size > TAIL_BYTES else ""
    header = f"# {log_file}{note} — last {len(tail)} line(s)\n"
    return header + "".join(tail)
