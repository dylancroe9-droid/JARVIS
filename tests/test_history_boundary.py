"""
Claude history boundary guard.

The blind trim (self.messages[-40:]) can leave the window starting on an assistant
message, or on a user message that is an orphan tool_result (its parent tool_use
fell out of the window). Claude 400s on either, and since the trim keeps landing
there, EVERY following turn 400s too — silently dropping Dylan to the weaker Groq
model. It fires in plain conversation with no tools. This was the scan's #1
daily-driver blocker.
"""

from brain.jarvis import Jarvis


def _first(msgs):
    j = Jarvis.__new__(Jarvis)
    j.messages = msgs
    return j._claude_messages()


def _is_tool_result_first(out):
    c = out[0]["content"]
    return isinstance(c, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def test_strips_leading_assistant_message():
    out = _first([
        {"role": "assistant", "content": "previous reply"},
        {"role": "user", "content": "hey"},
    ])
    assert out[0]["role"] == "user"


def test_strips_leading_orphan_tool_result():
    out = _first([
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "r"}]},
        {"role": "user", "content": "hey"},
    ])
    assert out[0]["role"] == "user"
    assert not _is_tool_result_first(out)


def test_normal_history_untouched():
    out = _first([
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ])
    assert out[0]["role"] == "user"
    assert out[0]["content"][0]["text"] if isinstance(out[0]["content"], list) else out[0]["content"]


def test_valid_tool_pairing_survives():
    # A tool_use/tool_result pair that is NOT at the front must be kept intact.
    out = _first([
        {"role": "user", "content": "what's tesla at"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "1", "name": "get_stock_price", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "$400"}]},
        {"role": "assistant", "content": "$400."},
    ])
    assert out[0]["role"] == "user"
    assert len(out) == 4, "a valid mid-list tool pair must not be stripped"


def test_current_user_turn_always_survives():
    # Even if everything before it is bad, the latest user turn (the one being sent)
    # must always remain — it's the last message.
    out = _first([
        {"role": "assistant", "content": "junk"},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "r"}]},
        {"role": "user", "content": "the actual question"},
    ])
    assert out[-1]["content"] == "the actual question" or \
           out[-1]["content"][0].get("text") == "the actual question"
    assert out[0]["role"] == "user" and not _is_tool_result_first(out)
