"""
Practice tests — generation, key verification, and rendering.

The verification pass is the whole point: an AI-written practice test with a wrong
answer key teaches you the wrong thing and you trust it, which is worse than no
practice at all. These lock in that a question only survives if an independent
re-solve agrees with the stated key.
"""

import json
from types import SimpleNamespace

import pytest

import tools.practice_test as pt


def _q(answer="A", stem="What is 2+2?"):
    return {"question": stem,
            "choices": {"A": "4", "B": "5", "C": "6", "D": "7"},
            "answer": answer, "why": "arithmetic"}


class _FakeClaude:
    """Stands in for the Anthropic client; replies come from a scripted list."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.messages = SimpleNamespace(create=self._create)
        self.calls = 0

    def _create(self, **kw):
        self.calls += 1
        text = self._replies.pop(0) if self._replies else ""
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


@pytest.fixture(autouse=True)
def _clean_state():
    pt._STATE.update(questions=[], answers={}, revealed=False, topic="")
    yield
    pt._STATE.update(questions=[], answers={}, revealed=False, topic="")


class TestGeneration:
    def test_parses_a_json_array(self, monkeypatch):
        payload = json.dumps([_q("B")])
        monkeypatch.setattr(pt, "_claude", lambda: (_FakeClaude([payload]), "m"))
        assert len(pt._generate("x", 1, "AP")) == 1

    def test_tolerates_prose_around_the_json(self, monkeypatch):
        payload = "Sure, here you go:\n" + json.dumps([_q()]) + "\nHope that helps!"
        monkeypatch.setattr(pt, "_claude", lambda: (_FakeClaude([payload]), "m"))
        assert len(pt._generate("x", 1, "AP")) == 1

    def test_drops_malformed_questions(self, monkeypatch):
        bad = [
            _q(),
            {"question": "no choices", "answer": "A"},              # missing choices
            {"question": "q", "choices": {"A": "1"}, "answer": "A"},  # partial choices
            {"question": "q", "choices": {k: k for k in "ABCD"}, "answer": "E"},  # bad key
        ]
        monkeypatch.setattr(pt, "_claude", lambda: (_FakeClaude([json.dumps(bad)]), "m"))
        assert len(pt._generate("x", 4, "AP")) == 1

    def test_survives_unparseable_output(self, monkeypatch):
        monkeypatch.setattr(pt, "_claude", lambda: (_FakeClaude(["not json at all"]), "m"))
        assert pt._generate("x", 1, "AP") == []


class TestKeyVerification:
    def test_keeps_questions_the_solver_agrees_with(self, monkeypatch):
        monkeypatch.setattr(pt, "_claude", lambda: (_FakeClaude(["A", "B"]), "m"))
        kept, dropped = pt._verify([_q("A"), _q("B")])
        assert len(kept) == 2 and dropped == 0

    def test_drops_questions_with_a_wrong_key(self, monkeypatch):
        # Stated key is A; an independent solve says C — the key is not trustworthy.
        monkeypatch.setattr(pt, "_claude", lambda: (_FakeClaude(["C"]), "m"))
        kept, dropped = pt._verify([_q("A")])
        assert kept == [] and dropped == 1

    def test_reads_the_letter_out_of_a_wordy_reply(self, monkeypatch):
        monkeypatch.setattr(pt, "_claude", lambda: (_FakeClaude(["I'd go with B here."]), "m"))
        kept, _ = pt._verify([_q("B")])
        assert len(kept) == 1

    def test_a_failing_solve_drops_the_question(self, monkeypatch):
        class Boom(_FakeClaude):
            def _create(self, **kw):
                raise RuntimeError("api down")
        monkeypatch.setattr(pt, "_claude", lambda: (Boom([]), "m"))
        kept, dropped = pt._verify([_q("A")])
        assert kept == [] and dropped == 1


class TestCreateFlow:
    def _stub(self, monkeypatch, generated, verdicts, served=True):
        monkeypatch.setattr(pt, "_generate", lambda *a, **k: generated)
        monkeypatch.setattr(pt, "_verify", lambda qs: verdicts)
        monkeypatch.setattr(pt, "_serve", lambda: served)
        monkeypatch.setattr(pt, "_open_or_reload", lambda: None)

    def test_says_so_when_the_port_is_taken(self, monkeypatch):
        # Must not claim a test is on screen when it can't actually be served.
        self._stub(monkeypatch, [_q()], ([_q()], 0), served=False)
        assert "can't put the test on screen" in pt.create_practice_test("x", 1)

    def test_refuses_to_show_a_test_with_no_trustworthy_keys(self, monkeypatch):
        self._stub(monkeypatch, [_q(), _q()], ([], 2))
        msg = pt.create_practice_test("photosynthesis", 2)
        assert "none passed" in msg
        assert pt._STATE["questions"] == []

    def test_reports_how_many_were_thrown_out(self, monkeypatch):
        self._stub(monkeypatch, [_q(), _q()], ([_q()], 1))
        msg = pt.create_practice_test("photosynthesis", 2)
        assert "1 thrown out" in msg

    def test_says_so_when_generation_fails(self, monkeypatch):
        self._stub(monkeypatch, [], ([], 0))
        assert "Couldn't generate" in pt.create_practice_test("asdfghjkl", 3)

    def test_count_is_clamped(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(pt, "_generate", lambda t, n, d: seen.setdefault("n", n) and [])
        monkeypatch.setattr(pt, "_verify", lambda qs: ([], 0))
        pt.create_practice_test("x", count=500)
        assert seen["n"] == 25


class TestAnswerReveal:
    def test_needs_a_test_open(self):
        assert "No practice test open" in pt.show_test_answers()

    def test_marks_the_test_revealed(self, monkeypatch):
        monkeypatch.setattr(pt, "_open_or_reload", lambda: None)
        pt._STATE.update(questions=[_q()], topic="calc")
        pt.show_test_answers()
        assert pt._STATE["revealed"] is True


class TestRendering:
    def test_escapes_question_text(self):
        pt._STATE.update(questions=[_q(stem='<script>alert("x")</script>')], topic="t")
        assert "<script>alert" not in pt._render()

    def test_escapes_the_topic(self):
        pt._STATE.update(questions=[_q()], topic="<img onerror=1>")
        assert "<img onerror" not in pt._render()

    def test_carries_the_key_for_scoring(self):
        pt._STATE.update(questions=[_q("C")], topic="t")
        assert 'data-key="C"' in pt._render()

    def test_auto_fills_only_when_revealed(self):
        # The script always *references* __AUTO__; what changes is whether the
        # flag is set, so assert on the assignment rather than the mention.
        pt._STATE.update(questions=[_q()], topic="t", revealed=False)
        assert "__AUTO__=true" not in pt._render()
        pt._STATE["revealed"] = True
        assert "__AUTO__=true" in pt._render()


class TestTruncationSalvage:
    """
    A whole-array json.loads is all-or-nothing. One live run hit the output cap
    mid-array and lost all 6 questions — $0.12 and 79 seconds for nothing.
    """

    def test_recovers_questions_from_a_truncated_array(self):
        full = json.dumps([_q("A"), _q("B"), _q("C")])
        truncated = full[: full.rindex("}") ]      # cut mid-way, no closing ]
        got = pt._parse_questions(truncated)
        assert len(got) == 2, "should keep the questions that completed"

    def test_still_prefers_the_clean_whole_array(self):
        assert len(pt._parse_questions(json.dumps([_q(), _q()]))) == 2

    def test_salvage_skips_invalid_objects(self):
        txt = ('[{"question":"q","choices":{"A":"1"},"answer":"A"},'
               + json.dumps(_q("B"))[0:] )
        assert all(set("ABCD") <= set(q["choices"]) for q in pt._parse_questions(txt))

    def test_empty_on_junk(self):
        assert pt._parse_questions("I'm afraid I can't do that") == []

    def test_token_budget_scales_with_count(self, monkeypatch):
        seen = {}
        class C:
            def __init__(self): self.messages = self
            def create(self, **kw):
                seen["max"] = kw["max_tokens"]
                return SimpleNamespace(content=[SimpleNamespace(type="text", text="[]")])
        monkeypatch.setattr(pt, "_claude", lambda: (C(), "m"))
        pt._generate("t", 6, "AP")
        big = seen["max"]
        pt._generate("t", 1, "AP")
        assert big > seen["max"], "more questions must get more headroom"


class TestToolDescription:
    """
    Live run: "quiz me on the quadratic formula, 4 questions" got a clarifying
    question back ("one at a time or all at once?") instead of a test. Asking
    someone who is studying to negotiate the format first is exactly the friction
    that makes a tool not get used.
    """

    def _desc(self, name):
        from brain.tools import TOOL_DEFINITIONS
        return next(t for t in TOOL_DEFINITIONS if t["name"] == name)["description"]

    def test_generation_is_told_not_to_ask_first(self):
        d = self._desc("create_practice_test").lower()
        assert "immediately" in d
        assert "do not ask" in d

    def test_reveal_is_scoped_to_generated_tests(self):
        d = self._desc("show_test_answers").lower()
        assert "graded assignment" in d or "school portal" in d
