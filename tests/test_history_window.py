"""
Tests for brain.jarvis._slim_history — specifically the leading-orphan-tool
trim that prevents Groq 400s ("a tool message must be preceded by the assistant
tool_calls message") when a tail history slice begins partway through a tool
exchange.
"""

from brain.jarvis import Jarvis

slim = Jarvis._slim_history


def _roles(msgs):
    return [m["role"] for m in msgs]


def test_leading_tool_messages_are_dropped():
    window = [
        {"role": "tool", "tool_call_id": "a", "content": "weather: 72F"},
        {"role": "tool", "tool_call_id": "b", "content": "calendar: none"},
        {"role": "assistant", "content": "It's 72 and your calendar is clear."},
        {"role": "user", "content": "thanks"},
    ]
    out = slim(window)
    assert _roles(out) == ["assistant", "user"]   # orphan tools trimmed off front


def test_valid_window_starting_on_user_is_unchanged_in_role_order():
    window = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello."},
    ]
    out = slim(window)
    assert _roles(out) == ["user", "assistant"]


def test_assistant_toolcalls_then_tool_is_preserved():
    # A window that starts on the assistant carrying the tool_calls is VALID —
    # the tool responses that follow are in-window, so nothing should be trimmed.
    window = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "a", "type": "function",
                         "function": {"name": "get_weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "72F"},
        {"role": "assistant", "content": "It's 72."},
    ]
    out = slim(window)
    assert _roles(out) == ["assistant", "tool", "assistant"]


def test_all_tool_window_becomes_empty():
    window = [
        {"role": "tool", "tool_call_id": "a", "content": "x"},
        {"role": "tool", "tool_call_id": "b", "content": "y"},
    ]
    out = slim(window)
    assert out == []


def test_single_leading_tool_then_user():
    window = [
        {"role": "tool", "tool_call_id": "a", "content": "z"},
        {"role": "user", "content": "next question"},
        {"role": "assistant", "content": "Sure."},
    ]
    out = slim(window)
    assert _roles(out) == ["user", "assistant"]


def test_tool_content_still_truncated_after_trim():
    window = [
        {"role": "user", "content": "search"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "a", "type": "function",
                         "function": {"name": "search_web", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "X" * 5000},
    ]
    out = slim(window, max_chars=100)
    tool_msg = out[-1]
    assert len(tool_msg["content"]) < 200
    assert tool_msg["content"].endswith("…[truncated]")
