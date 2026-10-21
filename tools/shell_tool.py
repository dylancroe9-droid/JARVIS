"""
Shell command execution with output capture and safety guardrails.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

from config import USER_HOME

# Commands that require explicit confirmation — JARVIS will not auto-run these.
# (The personality prompt instructs JARVIS to ask the user first.)
_DANGEROUS_PATTERNS = [
    "rm -rf", "rmdir", "mkfs", "dd if=", "chmod 777", ":(){:|:&};:",
    "sudo rm", "> /dev/", "format ", "fdisk",
]


def run_command(
    command: str,
    working_directory: Optional[str] = None,
    timeout: int = 30,
) -> str:
    timeout = min(max(timeout, 1), 300)
    cwd = str(Path(working_directory).expanduser().resolve()) if working_directory else USER_HOME

    if not os.path.isdir(cwd):
        return f"Working directory not found: {cwd}"

    # Ask permission before running any shell command
    from tools.permissions import request
    if not request(f"Run command: {command}"):
        return "Permission denied."

    # Hard block on truly destructive patterns regardless
    lower_cmd = command.lower()
    for pattern in _DANGEROUS_PATTERNS:
        if pattern in lower_cmd:
            return (
                f"Blocked: command contains destructive pattern '{pattern}'."
            )

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s: {command}"
    except FileNotFoundError as e:
        # shell=True routes through /bin/sh so this is unlikely, but guard anyway
        return f"Command not found: {command} ({e})"
    except PermissionError as e:
        return f"Permission denied running command: {e}"
    except Exception as e:
        return f"Failed to run command: {e}"

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    exit_code = result.returncode

    parts = []
    if stdout:
        # Cap output to avoid filling context
        lines = stdout.splitlines()
        if len(lines) > 100:
            shown = "\n".join(lines[:100])
            parts.append(f"{shown}\n… ({len(lines) - 100} more lines truncated)")
        else:
            parts.append(stdout)

    if stderr:
        parts.append(f"[stderr]\n{stderr[:2000]}")

    parts.append(f"[exit code: {exit_code}]")
    return "\n".join(parts) if parts else f"[exit code: {exit_code}]"
