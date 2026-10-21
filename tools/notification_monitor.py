"""
Notification monitor — listens to macOS's system log for incoming notifications
and feeds them to a callback (so JARVIS can announce them).

Approach: pipe `log stream` filtered to UserNotifications, parse each event for
the source app's bundle ID, debounce per-app, and call back with a friendly
name + optional content snippet (Messages content is pullable via AppleScript;
Snapchat etc. don't expose their notification body to us).

Doesn't need Full Disk Access — only reads system log events, not the
TCC-protected notification database.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from typing import Callable, Optional


# Map bundle IDs to friendly names. JARVIS announces these.
APP_NAMES = {
    "com.apple.MobileSMS":               "Messages",
    "com.apple.iChat":                   "Messages",
    "com.toyopagroup.picaboo":           "Snapchat",
    "com.snap.snapchat":                 "Snapchat",
    "com.snapchat.SnapchatWeb":          "Snapchat",
    "com.snap.web.snapchat":             "Snapchat",
    # iPhone-relayed (Continuity) variants seen with relayed notifications
    "com.toyopagroup.picaboo.iphoneos":  "Snapchat",
    "com.snap.SnapChat":                 "Snapchat",
    "com.snapchat":                      "Snapchat",
    "com.facebook.Messenger":            "Messenger",
    "com.facebook.MessengerMacOS":       "Messenger",
    "com.tinyspeck.slackmacgap":         "Slack",
    "com.hnc.Discord":                   "Discord",
    "com.apple.mail":                    "Mail",
    "com.google.Chrome":                 "Chrome",
    "com.spotify.client":                "Spotify",
    "com.apple.Music":                   "Apple Music",
    "com.apple.iCal":                    "Calendar",
    "com.apple.reminders":               "Reminders",
    "com.apple.MobileSMS.MessageNotification": "Messages",
    "com.burbn.instagram":               "Instagram",
    "com.atebits.Tweetie2":              "Twitter",
    "com.zhiliaoapp.musically":          "TikTok",
    "us.zoom.xos":                       "Zoom",
    "com.apple.FaceTime":                "FaceTime",
    "com.apple.facetime":                "FaceTime",
    "com.apple.mobilephone":             "Phone",
    "com.apple.CallHistoryPluginHelper": "Phone",
    "com.apple.iChat.FaceTime":          "FaceTime",
}

# Apps whose notifications RE-POST continuously while active (a ringing call
# re-fires its notification every couple seconds for the whole ring). A flat
# 8s cooldown announces these 4-7 times per call. Give them a long cooldown
# so an incoming call is announced at most once.
_LONG_COOLDOWN_APPS = {
    "com.apple.FaceTime": 120.0,
    "com.apple.facetime": 120.0,
    "com.apple.iChat.FaceTime": 120.0,
    "com.apple.mobilephone": 120.0,
    "com.apple.CallHistoryPluginHelper": 120.0,
}

# Apps to NEVER announce — these are system internals or background apps
# Dylan doesn't care about hearing announcements for.
DENYLIST = {
    "com.apple.ScriptEditor2",
    "com.apple.notificationcenter",
    "com.apple.notificationcenterui",
    "com.apple.controlcenter",
    "com.apple.systempreferences",
    "com.apple.dock",
    "com.apple.WindowManager",
    "com.apple.Spotlight",
    "com.anthropic.claudefordesktop",   # JARVIS's developer using Claude desktop
    "com.apple.iCal",                    # calendar reminders — proactive engine handles these
    "com.apple.UsageTrackingAgent",
}

# Patterns to extract the source app's bundle ID from log lines.
# usernoted's NotificationRecord format: <NotificationRecord app:"com.apple.X" ...>
_BUNDLE_PATTERNS = (
    re.compile(r'app:"([\w.\-]+)"'),                      # <NotificationRecord app:"...">
    re.compile(r'app\s+([\w.\-]+)'),                      # "for app com.foo"
    re.compile(r'bundleID[=:]?\s*"?([\w.\-]+)"?'),        # bundleID="..."
    re.compile(r'bundle[_ ]?identifier[=:]?\s*"?([\w.\-]+)"?'),
)
# Match "Delivering" / "Queuing action delivered" / etc.
_DELIVER_RE = re.compile(r'\b(deliver|present|banner|alert)', re.IGNORECASE)


def _friendly_name(bundle_id: str) -> str:
    if bundle_id in APP_NAMES:
        return APP_NAMES[bundle_id]
    # Auto-shorten unknown bundle IDs ("com.foo.bar" → "Bar")
    parts = bundle_id.split(".")
    if parts:
        return parts[-1].replace("-", " ").title()
    return bundle_id


def _read_latest_imessage() -> str:
    """Pull the most recent iMessage (sender + body) via AppleScript."""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "Messages"\n'
             '  set lastChat to first chat\n'
             '  set lastMsg to last message of lastChat\n'
             '  set msgText to (content of lastMsg) as string\n'
             '  set sender to (display name of (handle of lastMsg)) as string\n'
             '  return sender & "|" & msgText\n'
             'end tell'],
            capture_output=True, text=True, timeout=3,
        )
        out = (r.stdout or "").strip()
        if "|" in out:
            sender, body = out.split("|", 1)
            return f"from {sender}: {body[:140]}"
    except Exception:
        pass
    return ""


def _read_latest_mail() -> str:
    """Pull the most recent unread email (sender + subject) via AppleScript."""
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "Mail"\n'
             '  try\n'
             '    set unreadMsgs to (every message of inbox whose read status is false)\n'
             '    if (count of unreadMsgs) = 0 then return ""\n'
             '    set lastMsg to first item of unreadMsgs\n'
             '    set theSender to (sender of lastMsg) as string\n'
             '    set theSubject to (subject of lastMsg) as string\n'
             '    -- Clean up sender ("Name <email@example.com>" → "Name")\n'
             '    if theSender contains "<" then\n'
             '      set theSender to text 1 thru ((offset of "<" in theSender) - 1) of theSender\n'
             '      set theSender to text 1 thru -1 of theSender\n'
             '    end if\n'
             '    return theSender & "|" & theSubject\n'
             '  on error\n'
             '    return ""\n'
             '  end try\n'
             'end tell'],
            capture_output=True, text=True, timeout=4,
        )
        out = (r.stdout or "").strip()
        if "|" in out:
            sender, subject = out.split("|", 1)
            sender = sender.strip().strip('"')
            return f"from {sender}: {subject[:140]}"
    except Exception:
        pass
    return ""


# Patterns to extract title/body from notification log messages. macOS log
# format includes these in various shapes — we try multiple.
_NOTIF_TITLE_RE = re.compile(r'title\s*=\s*"([^"]+)"', re.IGNORECASE)
_NOTIF_BODY_RE  = re.compile(r'body\s*=\s*"([^"]+)"',  re.IGNORECASE)
_NOTIF_SUBTITLE_RE = re.compile(r'subtitle\s*=\s*"([^"]+)"', re.IGNORECASE)


def _extract_log_content(message: str) -> str:
    """
    Try to pull the notification's user-visible title + body from a log line.
    For apps without an AppleScript API (Snapchat, Discord, etc.) this is
    the only way to get the actual content.
    """
    title = ""; body = ""
    m = _NOTIF_TITLE_RE.search(message)
    if m: title = m.group(1)
    m = _NOTIF_BODY_RE.search(message)
    if m: body = m.group(1)
    m = _NOTIF_SUBTITLE_RE.search(message)
    if m and not body: body = m.group(1)
    # Drop common <private> redactions
    if title == "<private>": title = ""
    if body == "<private>":  body = ""
    out = ""
    if title and body:
        out = f"from {title}: {body}"
    elif title:
        out = f"from {title}"
    elif body:
        out = body
    return out[:200]


class NotificationMonitor:
    """
    Spawns `log stream` and dispatches notification events to a callback.
    callback(app_name: str, content_snippet: str) — content_snippet may be "".
    """

    def __init__(self,
                 on_notification: Callable[[str, str], None],
                 cooldown_sec: float = 8.0):
        self.on_notification = on_notification
        self.cooldown_sec = cooldown_sec
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_announce: dict[str, float] = {}   # app → last announce time

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="notif-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def _run(self) -> None:
        # Watch ALL notification daemons. Local Mac apps fire via "usernoted".
        # iPhone-relayed notifications (Continuity, like Snapchat from phone)
        # come through Apple Push Service "apsd" or NotificationCenter.
        try:
            self._proc = subprocess.Popen(
                ["/usr/bin/log", "stream",
                 "--predicate",
                 '(process == "usernoted") '
                 'OR (process == "apsd") '
                 'OR (process == "NotificationCenter") '
                 'OR (process == "NotificationCenterUI")',
                 "--info", "--style", "ndjson"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
        except Exception as exc:
            print(f"[notif] failed to start log stream: {exc}", flush=True)
            return

        # FIFO-evicting dedupe — earlier code used set + slice rebuild which
        # was O(n) per eviction AND incorrect (sets have no order so the
        # "last 100" was random). Python dicts preserve insertion order
        # since 3.7, so a plain dict-as-ordered-set gives O(1) add/lookup
        # and O(1) oldest-entry eviction.
        _DEDUPE_CAP = 200
        seen_idents: dict[str, None] = {}
        for line in iter(self._proc.stdout.readline, ""):
            if self._stop.is_set():
                break
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            msg = ev.get("eventMessage") or ""
            # Must look like a delivery event with an app reference
            if not _DELIVER_RE.search(msg):
                continue
            bundle = ""
            for pat in _BUNDLE_PATTERNS:
                m = pat.search(msg)
                if m:
                    bundle = m.group(1)
                    break
            if not bundle:
                continue
            # Valid bundle IDs always look like "com.X.Y" with at least one dot.
            # Reject bogus single-word matches like "notification" or "app".
            if "." not in bundle or len(bundle) < 5:
                continue
            # Drop system internals and JARVIS-induced AppleScript events
            if bundle in DENYLIST:
                continue
            if "usernoted" in bundle or "notificationcenter" in bundle.lower():
                continue
            # Dedupe: usernoted logs the same notification several times.
            ident_match = re.search(r'ident:"([\w\-]+)"', msg)
            if ident_match:
                ident = ident_match.group(1)
                if ident in seen_idents:
                    continue
                seen_idents[ident] = None
                # O(1) FIFO eviction — pop the oldest entry when over cap
                if len(seen_idents) > _DEDUPE_CAP:
                    # next(iter(dict)) returns the oldest key (insertion order)
                    oldest = next(iter(seen_idents))
                    del seen_idents[oldest]

            # Throttle per-app. Call-type apps (FaceTime/Phone) re-post their
            # notification for the entire ring, so they get a much longer
            # cooldown — announce an incoming call once, not every few seconds.
            now = time.time()
            _cooldown = _LONG_COOLDOWN_APPS.get(bundle, self.cooldown_sec)
            if now - self._last_announce.get(bundle, 0) < _cooldown:
                continue
            self._last_announce[bundle] = now
            # Log AFTER dedupe+throttle so we only see notifications that
            # actually become announcements. Was previously logged before
            # the filters and produced ~10 lines per real notification.
            print(f"[notif] announcing bundle={bundle!r}", flush=True)

            app_name = _friendly_name(bundle)
            # Try to pull body content. Priority:
            #  1. Apps with proper API → use it (Messages, Mail)
            #  2. Apps without API → parse the log message itself for title/body
            content = ""
            try:
                if bundle in ("com.apple.MobileSMS", "com.apple.iChat"):
                    content = _read_latest_imessage()
                elif bundle == "com.apple.mail":
                    content = _read_latest_mail()
                # Fallback: parse the log message for title/body (works for Snap,
                # Discord, Slack, anything that includes content in the log).
                if not content:
                    content = _extract_log_content(msg)
            except Exception as exc:
                # The content readers shell out (osascript) and CAN raise. An
                # unhandled throw here would kill the monitor thread, leaving it
                # "started but dead" — the exact state diagnostics can't detect
                # and (before its fix) falsely reported as repaired. Never let a
                # content-read failure take the thread down; just announce the
                # app with no body.
                print(f"[notif] content read error: {exc}", flush=True)
                content = ""

            try:
                self.on_notification(app_name, content)
            except Exception as exc:
                print(f"[notif] callback error: {exc}", flush=True)

        # Cleanup — explicitly close stdout PIPE and wait for the child.
        # The earlier code only called .wait(); if the loop exited via an
        # exception, the stdout file descriptor stayed open until garbage
        # collection. Over a multi-day session this would leak FDs.
        if self._proc:
            try:
                if self._proc.stdout:
                    self._proc.stdout.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            except Exception:
                pass
