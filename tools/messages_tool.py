"""iMessage integration — send messages via macOS Messages app."""
import subprocess
from tools.permissions import request

from rich.console import Console
console = Console()


def send_imessage(contact: str, message: str) -> str:
    """Send an iMessage or SMS to a contact (phone number, email, or name)."""
    if not request(f"Send message to {contact}: \"{message[:60]}{'…' if len(message)>60 else ''}\""):
        return "Message not sent — permission denied."

    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    safe_contact = contact.replace("\\", "\\\\").replace('"', '\\"')

    script = f'''
tell application "Messages"
    set targetService to first service whose service type is iMessage
    set targetBuddy to buddy "{safe_contact}" of targetService
    send "{safe_msg}" to targetBuddy
end tell
'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "Messages timed out — is the Messages app responding?"
    except Exception as exc:
        return f"Couldn't send message: {exc}"

    if r.returncode == 0:
        return f"Message sent to {contact}."

    err = r.stderr.strip().lower()
    # Detect "buddy not found" / contact resolution failures
    if "buddy" in err or "not found" in err or "invalid" in err:
        return (
            f"Contact not found: '{contact}'. "
            "Try using their phone number instead (e.g. +15551234567)."
        )

    # Fallback: try any service
    script2 = f'''
tell application "Messages"
    send "{safe_msg}" to buddy "{safe_contact}" of first service
end tell
'''
    try:
        r2 = subprocess.run(["osascript", "-e", script2], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "Messages timed out on second attempt."
    except Exception as exc:
        return f"Couldn't send message (fallback also failed): {exc}"

    if r2.returncode == 0:
        return f"Message sent to {contact}."

    err2 = r2.stderr.strip().lower()
    if "buddy" in err2 or "not found" in err2 or "invalid" in err2:
        return (
            f"Contact not found: '{contact}'. "
            "Try using their phone number instead (e.g. +15551234567)."
        )
    return f"Failed to send message. Make sure Messages is set up and '{contact}' is a valid contact."


def get_contacts(query: str) -> str:
    """Search macOS Contacts for a name and return their info."""
    safe_q = query.replace('"', '\\"')
    script = f'''
set results to ""
tell application "Contacts"
    set ppl to (every person whose name contains "{safe_q}")
    repeat with p in ppl
        set pname to name of p
        set pphones to phones of p
        set results to results & pname
        if (count of pphones) > 0 then
            set results to results & ": " & (value of item 1 of pphones)
        end if
        set results to results & linefeed
    end repeat
end tell
return results
'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "Contacts lookup timed out — is Contacts.app responding?"
    except Exception as exc:
        return f"Couldn't search contacts: {exc}"

    if r.returncode != 0:
        err = r.stderr.strip().lower()
        if "not authorized" in err or "1743" in err or "access" in err:
            return (
                "Contacts access denied. Grant access in "
                "System Settings > Privacy & Security > Contacts."
            )
        return f"Contacts error: {r.stderr.strip()}"

    out = r.stdout.strip()
    if not out:
        return f"No contacts found matching '{query}'."
    return out
