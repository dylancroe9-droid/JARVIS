"""
Email integration — reads and drafts via macOS Mail.app (AppleScript).
No extra permissions beyond what Mail.app normally has.
"""

import subprocess


def _run(script: str, timeout: int = 15) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return False, r.stderr.strip()
        return True, r.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as exc:
        return False, str(exc)


_PERM_MSG = (
    "Mail access not granted. "
    "System Settings → Privacy & Security → Automation → "
    "allow Terminal (or JARVIS) to control Mail."
)


def get_unread_emails(count: int = 10) -> str:
    """Get unread emails from Mail.app — subject, sender, preview."""
    count = min(count, 25)
    script = f"""
tell application "Mail"
    set unread to (messages of inbox whose read status is false)
    set output to ""
    set counter to 0
    repeat with msg in unread
        if counter >= {count} then exit repeat
        set s to subject of msg
        set sndr to sender of msg
        set preview to (content of msg)
        if length of preview > 200 then
            set preview to (characters 1 through 200 of preview as string) & "..."
        end if
        set output to output & "FROM: " & sndr & "\\nSUBJECT: " & s & "\\nPREVIEW: " & preview & "\\n---\\n"
        set counter to counter + 1
    end repeat
    if output is "" then return "no_unread"
    return output
end tell
"""
    ok, out = _run(script)
    if not ok:
        return _PERM_MSG if any(k in out.lower() for k in ["not allowed", "access", "1743"]) else f"Mail error: {out}"
    if out == "no_unread":
        return "No unread emails."
    return out.strip()


def get_recent_emails(count: int = 10, mailbox: str = "inbox") -> str:
    """Get the most recent emails regardless of read status."""
    count = min(count, 25)
    script = f"""
tell application "Mail"
    set mbox to mailbox "{mailbox}"
    set msgs to (messages of mbox)
    set output to ""
    set counter to 0
    repeat with msg in msgs
        if counter >= {count} then exit repeat
        set s to subject of msg
        set sndr to sender of msg
        set isRead to read status of msg
        set readStr to ""
        if isRead is false then set readStr to " [UNREAD]"
        set output to output & "FROM: " & sndr & "\\nSUBJECT: " & s & readStr & "\\n---\\n"
        set counter to counter + 1
    end repeat
    if output is "" then return "No emails found."
    return output
end tell
"""
    ok, out = _run(script)
    if not ok:
        return _PERM_MSG if any(k in out.lower() for k in ["not allowed", "access", "1743"]) else f"Mail error: {out}"
    return out.strip()


def read_email(subject_keyword: str) -> str:
    """Read the full content of an email matching a subject keyword."""
    safe = subject_keyword.replace('"', "'")
    script = f"""
tell application "Mail"
    set matched to (messages of inbox whose subject contains "{safe}")
    if (count of matched) = 0 then return "No email found with that subject."
    set msg to item 1 of matched
    set s to subject of msg
    set sndr to sender of msg
    set dt to date received of msg as string
    set body to content of msg
    if length of body > 3000 then
        set body to (characters 1 through 3000 of body as string) & "\\n[truncated]"
    end if
    return "FROM: " & sndr & "\\nSUBJECT: " & s & "\\nDATE: " & dt & "\\n\\n" & body
end tell
"""
    ok, out = _run(script)
    if not ok:
        return _PERM_MSG if any(k in out.lower() for k in ["not allowed", "access", "1743"]) else f"Mail error: {out}"
    return out.strip()


def send_email(to: str, subject: str, body: str) -> str:
    """Send an email via Mail.app."""
    safe_to      = to.replace('"', "'")
    safe_subject = subject.replace('"', "'")
    safe_body    = body.replace("\\", "\\\\").replace('"', '\\"')
    script = f"""
tell application "Mail"
    set newMsg to make new outgoing message with properties {{subject:"{safe_subject}", content:"{safe_body}", visible:true}}
    tell newMsg
        make new to recipient with properties {{address:"{safe_to}"}}
    end tell
    send newMsg
end tell
return "sent"
"""
    ok, out = _run(script)
    if not ok:
        return _PERM_MSG if any(k in out.lower() for k in ["not allowed", "access", "1743"]) else f"Couldn't send: {out}"
    return f"Email sent to {to}."


def draft_email(to: str, subject: str, body: str) -> str:
    """Open a draft email in Mail.app without sending — user reviews before sending."""
    safe_to      = to.replace('"', "'")
    safe_subject = subject.replace('"', "'")
    safe_body    = body.replace("\\", "\\\\").replace('"', '\\"')
    script = f"""
tell application "Mail"
    activate
    set newMsg to make new outgoing message with properties {{subject:"{safe_subject}", content:"{safe_body}", visible:true}}
    tell newMsg
        make new to recipient with properties {{address:"{safe_to}"}}
    end tell
end tell
return "drafted"
"""
    ok, out = _run(script)
    if not ok:
        return _PERM_MSG if any(k in out.lower() for k in ["not allowed", "access", "1743"]) else f"Couldn't draft: {out}"
    return f"Draft opened in Mail — review and send when ready."
