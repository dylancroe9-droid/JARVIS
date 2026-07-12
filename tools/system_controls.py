"""macOS system controls — brightness, dark mode, lock screen, notifications."""
import subprocess

from rich.console import Console
console = Console()


def _osascript(script: str) -> str:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() or r.stderr.strip()
    except subprocess.TimeoutExpired:
        console.print("[yellow]osascript timed out[/yellow]")
        return "error: AppleScript timed out"
    except Exception as exc:
        console.print(f"[yellow]osascript error: {exc}[/yellow]")
        return f"error: {exc}"


def set_dark_mode(enabled: bool) -> str:
    """Toggle dark or light mode."""
    try:
        val = "true" if enabled else "false"
        result = _osascript(
            f'tell application "System Events" to tell appearance preferences to set dark mode to {val}'
        )
        if result.startswith("error"):
            return f"Couldn't set dark mode: {result}"
        return f"{'Dark' if enabled else 'Light'} mode enabled."
    except Exception as exc:
        return f"Couldn't set dark mode: {exc}"


def set_brightness(direction: str = "up", steps: int = 3) -> str:
    """Increase or decrease screen brightness. direction = 'up' or 'down', steps 1-16."""
    try:
        key_code = 144 if direction.lower() == "up" else 145
        steps = max(1, min(16, steps))
        script = f"repeat {steps} times\n    tell application \"System Events\" to key code {key_code}\nend repeat"
        result = _osascript(script)
        if result and ("not authorized" in result.lower() or "1002" in result or "privileges" in result.lower()):
            return (
                "Accessibility permission required for brightness control. "
                "Enable in System Settings > Privacy > Accessibility."
            )
        return f"Brightness {'increased' if direction == 'up' else 'decreased'}."
    except Exception as exc:
        return f"Couldn't adjust brightness: {exc}"


def lock_screen() -> str:
    """Lock the screen."""
    cgsession = (
        "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession"
    )
    try:
        r = subprocess.run(
            [cgsession, "-suspend"],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0:
            return "Screen locked."
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        pass
    except Exception as exc:
        console.print(f"[yellow]CGSession failed: {exc} — trying pmset[/yellow]")

    # Fallback: pmset sleepnow (available on all macOS versions)
    try:
        r2 = subprocess.run(["pmset", "sleepnow"], capture_output=True, timeout=5)
        if r2.returncode == 0:
            return "Screen locked (sleep)."
    except Exception:
        pass

    # Last resort: screensaver
    try:
        _osascript(
            'tell application "System Events" to start current screen saver'
        )
        return "Screen saver started."
    except Exception as exc:
        return f"Couldn't lock screen: {exc}"


def show_notification(title: str, message: str, sound: bool = True) -> str:
    """Show a macOS notification banner."""
    try:
        t = title.replace('"', '\\"')
        m = message.replace('"', '\\"')
        sound_str = ' sound name "Glass"' if sound else ""
        result = _osascript(f'display notification "{m}" with title "{t}"{sound_str}')
        if result.startswith("error"):
            return f"Couldn't show notification: {result}"
        return "Notification shown."
    except Exception as exc:
        return f"Couldn't show notification: {exc}"


def get_wifi_status() -> str:
    """Get current Wi-Fi network and signal info."""
    import os
    airport = (
        "/System/Library/PrivateFrameworks/Apple80211.framework"
        "/Versions/Current/Resources/airport"
    )
    if not os.path.exists(airport):
        # Fallback: use networksetup
        try:
            r = subprocess.run(
                ["networksetup", "-getairportnetwork", "en0"],
                capture_output=True, text=True, timeout=5,
            )
            out = r.stdout.strip()
            if "Current Wi-Fi Network:" in out:
                return out.replace("Current Wi-Fi Network:", "Connected to:").strip()
            return "Not connected to Wi-Fi."
        except Exception as exc:
            return f"Wi-Fi info unavailable: {exc}"

    try:
        r = subprocess.run(
            [airport, "-I"],
            capture_output=True, text=True, timeout=5,
        )
        lines = {}
        for line in r.stdout.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                lines[k.strip()] = v.strip()
        ssid = lines.get("SSID", "")
        rssi = lines.get("agrCtlRSSI", "")
        if ssid:
            try:
                strength = (
                    "excellent" if rssi and int(rssi) > -60
                    else "good" if rssi and int(rssi) > -75
                    else "weak"
                )
            except ValueError:
                strength = "unknown"
            return f"Connected to {ssid} ({strength} signal)."
        return "Not connected to Wi-Fi."
    except subprocess.TimeoutExpired:
        return "Wi-Fi info unavailable: timed out."
    except Exception as exc:
        return f"Wi-Fi info unavailable: {exc}"


def toggle_wifi(enabled: bool) -> str:
    """Turn Wi-Fi on or off."""
    try:
        # Try common interface names
        for iface in ["en0", "en1"]:
            try:
                r = subprocess.run(
                    ["networksetup", "-setairportpower", iface, "on" if enabled else "off"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    return f"Wi-Fi turned {'on' if enabled else 'off'}."
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                continue
        return "Could not toggle Wi-Fi — check interface name."
    except Exception as exc:
        return f"Couldn't toggle Wi-Fi: {exc}"


def read_url(url: str, max_chars: int = 4000) -> str:
    """Fetch a URL and return its text content (HTML stripped)."""
    import urllib.request, html, re
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        # Strip scripts/styles
        raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        # Strip tags
        raw = re.sub(r"<[^>]+>", " ", raw)
        # Decode entities, collapse whitespace
        raw = html.unescape(raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        if len(raw) > max_chars:
            raw = raw[:max_chars] + "…"
        return raw if raw else "No readable content found."
    except Exception as e:
        return f"Could not fetch URL: {e}"
