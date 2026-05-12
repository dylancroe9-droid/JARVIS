"""
System information and macOS application control.
open_application correctly handles app names, file paths, and URLs.
"""

import os
import subprocess

# ── Common name → real app name mapping ───────────────────────────────────────
# Lets JARVIS say "open spotify" and have it work.

APP_ALIASES: dict[str, str] = {
    "spotify":          "Spotify",
    "chrome":           "Google Chrome",
    "google chrome":    "Google Chrome",
    "safari":           "Safari",
    "firefox":          "Firefox",
    "arc":              "Arc",
    "notes":            "Notes",
    "calendar":         "Calendar",
    "reminders":        "Reminders",
    "music":            "Music",
    "apple music":      "Music",
    "photos":           "Photos",
    "mail":             "Mail",
    "messages":         "Messages",
    "facetime":         "FaceTime",
    "settings":         "System Settings",
    "system settings":  "System Settings",
    "system preferences": "System Preferences",
    "finder":           "Finder",
    "terminal":         "Terminal",
    "iterm":            "iTerm",
    "iterm2":           "iTerm",
    "vscode":           "Visual Studio Code",
    "vs code":          "Visual Studio Code",
    "visual studio code": "Visual Studio Code",
    "cursor":           "Cursor",
    "xcode":            "Xcode",
    "discord":          "Discord",
    "slack":            "Slack",
    "zoom":             "Zoom",
    "figma":            "Figma",
    "notion":           "Notion",
    "1password":        "1Password 7",
    "obsidian":         "Obsidian",
    "word":             "Microsoft Word",
    "excel":            "Microsoft Excel",
    "powerpoint":       "Microsoft PowerPoint",
    "preview":          "Preview",
    "quicktime":        "QuickTime Player",
    "vlc":              "VLC",
    "calculator":       "Calculator",
    "maps":             "Maps",
    "clock":            "Clock",
    "shortcuts":        "Shortcuts",
    "automator":        "Automator",
    "activity monitor": "Activity Monitor",
}


def open_application(target: str) -> str:
    """
    Open an app by name, a file by path, or a URL.
    Handles common aliases (e.g. 'spotify' → 'Spotify').
    """
    # Security check
    tl = target.lower()
    if any(x in tl for x in ["javascript:", "data:", "file:///etc", "file:///sys"]):
        return f"Refused to open suspicious target: {target}"

    # URL — open in default browser
    if tl.startswith(("http://", "https://", "ftp://")):
        try:
            subprocess.Popen(["open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            return f"Couldn't open URL: {exc}"
        return f"Opened URL: {target}"

    # .app bundle path — extract the name and use `open -a` so macOS resolves
    # the correct location (System/Applications, /Applications, Spotlight, etc.)
    # Never use the raw .app path: it may be a broken alias or wrong location.
    if tl.endswith(".app") or "/.app" in tl:
        import re as _re
        name_match = _re.search(r'([^/]+)\.app', target)
        app_name = name_match.group(1) if name_match else target
        try:
            result = subprocess.run(
                ["open", "-a", app_name],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return f"Opened {app_name}."
        except Exception:
            pass
        return f"Couldn't open {app_name}."

    # File path — open with default app for that file type
    expanded = os.path.expanduser(target)
    if os.path.exists(expanded):
        try:
            subprocess.Popen(["open", expanded], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            return f"Couldn't open file: {exc}"
        return f"Opened file: {expanded}"

    # App name — resolve alias, then use -a flag
    app_name = APP_ALIASES.get(tl, target)
    try:
        result = subprocess.run(
            ["open", "-a", app_name],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return f"Timed out trying to open '{app_name}'."
    except Exception as exc:
        return f"Couldn't open '{app_name}': {exc}"

    if result.returncode == 0:
        return f"Opened {app_name}."

    # If alias failed and the alias differs from the original, try raw target name
    if app_name != target:
        try:
            result2 = subprocess.run(
                ["open", "-a", target],
                capture_output=True, text=True, timeout=10,
            )
            if result2.returncode == 0:
                return f"Opened {target}."
        except Exception:
            pass

    return f"Couldn't open '{app_name}': {result.stderr.strip() or 'app not found'}"


def app_control(app: str, action: str) -> str:
    """
    Control a running macOS application.
    action: quit | hide | show | focus | restart
    """
    app_name = APP_ALIASES.get(app.lower(), app)

    try:
        if action == "quit":
            out = _osascript(f'tell application "{app_name}" to quit')
            return f"Quit {app_name}." if not out.startswith("error") else out

        elif action in ("focus", "show"):
            try:
                result = subprocess.run(
                    ["open", "-a", app_name],
                    capture_output=True, text=True, timeout=10,
                )
                return f"Switched to {app_name}." if result.returncode == 0 else f"Couldn't focus {app_name}."
            except subprocess.TimeoutExpired:
                return f"Timed out trying to focus '{app_name}'."
            except Exception as exc:
                return f"Couldn't focus '{app_name}': {exc}"

        elif action == "hide":
            out = _osascript(f'tell application "System Events" to set visible of process "{app_name}" to false')
            return f"Hid {app_name}."

        elif action == "restart":
            _osascript(f'tell application "{app_name}" to quit')
            import time; time.sleep(1)
            try:
                subprocess.Popen(["open", "-a", app_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:
                return f"Quit {app_name} but couldn't relaunch: {exc}"
            return f"Restarted {app_name}."

        return f"Unknown action: {action}. Use: quit, focus, hide, restart."
    except Exception as exc:
        return f"app_control failed for '{app_name}' ({action}): {exc}"


def list_running_apps() -> str:
    """List all currently running user applications."""
    out = _osascript('''
tell application "System Events"
    set appList to name of every process where background only is false
    return appList as string
end tell
''')
    if not out:
        return "Couldn't retrieve running apps."
    apps = [a.strip() for a in out.split(",") if a.strip()]
    return "Running apps: " + ", ".join(sorted(apps))


def get_system_info() -> str:
    """Return a human-readable snapshot of system status."""
    lines = []

    dt = _run("date '+%A, %B %d, %Y at %I:%M %p'")
    lines.append(f"Date/time: {dt}")

    uptime = _run("uptime | sed 's/.*up /up /' | sed 's/, [0-9]* user.*//'")
    lines.append(f"Uptime: {uptime}")

    battery = _run("pmset -g batt | grep -Eo '[0-9]+%; [a-z]+' | head -1")
    if battery:
        lines.append(f"Battery: {battery}")

    cpu = _run("top -l 1 -s 0 | grep 'CPU usage' | head -1")
    if cpu:
        lines.append(f"CPU: {cpu.replace('CPU usage: ', '')}")

    mem = _run("vm_stat | awk '/free/ {free=$3} /active/ {act=$3} END {printf \"%.1f GB free\", (free+0)*4096/1073741824}'")
    if mem:
        lines.append(f"Memory: {mem}")

    disk = _run("df -h / | tail -1 | awk '{print $4\" available of \"$2}'")
    if disk:
        lines.append(f"Disk: {disk}")

    return "\n".join(lines) if lines else "Could not retrieve system info."


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: str) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return ""


def _osascript(script: str) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else f"error: {r.stderr.strip()}"
    except Exception as e:
        return f"error: {e}"
