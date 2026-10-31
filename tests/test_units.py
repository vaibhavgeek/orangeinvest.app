"""Unit tests for the guardrails, plus regression tests for the three bugs in docs/BUGS.md."""
import json
import pathlib
import tempfile
from datetime import date

import pytest

from errand_agent.approval import ApprovalError, is_affirmative, is_negative, new_approval
from errand_agent.budget import Budget, Limits
from errand_agent.clock import FakeClock
from errand_agent.mock_api import INJECTION_REVIEWS, NORMAL_REVIEWS, MockProviderAPI, SimulatedCrash
from errand_agent.nlu import LLMModel, OpenAIModel
from errand_agent.sanitize import looks_like_injection
from errand_agent.tools import ToolGateway
from errand_agent.trace import Tracer, redact
from helpers import PLUMBER_REQ, World


def gateway():
    c = FakeClock()
    api = MockProviderAPI(c)
    t = Tracer("unit")
    return ToolGateway(api, "u1", Budget(Limits(), c), t, c), api, t


@pytest.mark.parametrize("text,yes", [("yes", True), ("Yes please", True), ("ok", True), ("go ahead", True),
                                      ("confirm", True), ("yes but tomorrow", False), ("hmm maybe", False),
                                      ("no", False), ("not yet", False), ("book a plumber in HSR tomorrow", False),
                                      ("2", False), ("", False)])
def test_affirmative_is_strict(text, yes):
    assert is_affirmative(text) is yes


@pytest.mark.parametrize("text,no", [("no", True), ("nope", True), ("cancel that", True), ("never mind", True),
                                     ("yes", False), ("Cancel my AC booking", False)])
def test_negative(text, no):
    assert is_negative(text) is no


def test_gateway_blocks_unapproved_mutations():
    gw, api, tracer = gateway()
    slot = api.search_providers("plumber", "Indiranagar", ["2026-11-02", "2026-11-02"])[0]
    pid, s = slot["id"], slot["free_slots"][0]
    hold = gw.hold_slot(pid, s)
    ap = new_approval("book", pid, "x", s, hold["quoted_price"])
    with pytest.raises(ApprovalError):                       # pending, not granted
        gw.book(hold["hold_id"], ap["idempotency_key"], ap)
    ap["status"] = "granted"
    with pytest.raises(ApprovalError):                       # wrong idempotency key
        gw.book(hold["hold_id"], "book-other", ap)
    ap["price"] = 1                                          # tampered after creation
    with pytest.raises(ApprovalError):
        gw.book(hold["hold_id"], ap["idempotency_key"], ap)
    other = new_approval("book", pid, "x", s, hold["quoted_price"] + 100)
    other["status"] = "granted"
    with pytest.raises(ApprovalError):                       # price differs from the hold's quote
        gw.book(hold["hold_id"], other["idempotency_key"], other)
    with pytest.raises(ApprovalError):
        gw.cancel("BK-0902", "cancel-x", None)
    assert not [c for c in api.calls if c[0] in ("book", "cancel")]
    assert len(tracer.of_type("approval_blocked")) == 5


def test_injection_detector():
    assert all(looks_like_injection(t.format(other="X")) for t in INJECTION_REVIEWS)
    assert not any(looks_like_injection(t) for t in NORMAL_REVIEWS)


def test_redaction_keeps_token_counts():
    out = redact({"api_key": "abc", "input_tokens": 12, "note": "key sk-ant-1234567890abc", "nested": {"Authorization": "x"}})
    assert out["api_key"] == "[REDACTED]" and out["input_tokens"] == 12
    assert "sk-ant" not in out["note"] and out["nested"]["Authorization"] == "[REDACTED]"


def test_llm_output_is_schema_validated():
    fake = lambda payload: {"content": [{"text": json.dumps({"intent": "book", "service": "plumber",
                                                               "approved": True, "area": "Mars"})}],
                            "usage": {"input_tokens": 100, "output_tokens": 20}}
    p, usage, dropped = LLMModel("k", transport=fake).parse("plumber", {"today": date(2026, 11, 1)})
    assert p.intent == "book" and p.service == "plumber" and p.area is None
    assert set(dropped) == {"approved", "area"} and usage["cost_usd"] > 0

    def broken(payload):
        raise TimeoutError("network")
    p, usage, _ = LLMModel("k", transport=broken).parse("need a plumber", {"today": date(2026, 11, 1)})
    assert p.service == "plumber" and "fallback_reason" in usage


def test_openai_output_is_schema_validated_and_dates_owned_by_code():
    sent = {}

    def fake(payload):
        sent.update(payload)
        out = {"intent": "book", "service": "electrician", "approved": True, "selection": "2",
               "date_from": "2026-11-01", "date_to": "2026-11-01"}  # wrong: "tomorrow" is 11-02
        return {"choices": [{"message": {"content": json.dumps(out)}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 40}}
    ctx = {"today": date(2026, 11, 1), "stage": "idle", "n_options": 0}
    p, usage, dropped = OpenAIModel("sk-test-SECRET", transport=fake).parse("electrician tomorrow", ctx)
    assert p.intent == "book" and p.service == "electrician" and p.selection is None
    assert set(dropped) == {"approved", "selection"} and usage["cost_usd"] > 0
    assert (p.date_from, p.date_to) == ("2026-11-02", "2026-11-02") and "date_override" in usage
    assert sent["response_format"] == {"type": "json_object"} and sent["model"] == OpenAIModel.DEFAULT
    assert "sk-test-SECRET" not in json.dumps(sent)  # key travels only in the HTTP header, never in the payload

    bad = lambda payload: {"choices": [{"message": {"content": "sure! booking now"}}]}
    p, usage, _ = OpenAIModel("k", transport=bad).parse("need a plumber", ctx)
    assert p.service == "plumber" and "fallback_reason" in usage

# ---------------- regression tests for docs/BUGS.md ----------------

def test_bug1_crash_during_tool_call_is_still_traced(tmp_path):
    """Bug 1: a crash inside book() left no tool_call line in the trace, so the recovery looked unexplained."""
    from errand_agent.mock_api import Scenario
    w = World(tmp_path, Scenario(crash_after_book=True, book_outcomes=["success"]))
    w.say(PLUMBER_REQ, "1")
    with pytest.raises(SimulatedCrash):
        w.say("yes")
    book_events = [e for e in w.events("tool_call") if e["tool"] == "book"]
    assert book_events and "SimulatedCrash" in book_events[0]["error"]


def test_bug2_seeded_runs_are_reproducible():
    """Bug 2: book outcomes were seeded by the (random) idempotency key, so the same seed gave different runs."""
    def run():
        w = World(pathlib.Path(tempfile.mkdtemp()))
        replies = []
        for _ in range(4):
            replies += w.say(PLUMBER_REQ, "1", "yes")
            w.agent.state["stage"] = "done"
        return [(e["tool"], e.get("error")) for e in w.events("tool_call")], len(w.new_bookings())
    assert run() == run()


def test_bug3_new_errand_does_not_inherit_old_constraints(tmp_path):
    """Bug 3: a new request for a different service mid-flow kept the previous errand's '2pm' and budget."""
    w = World(tmp_path)
    w.say("Need an electrician in Jayanagar tomorrow afternoon under 500", "at 2pm")
    w.say("Find a cleaner in Whitefield this weekend, no budget")
    r = w.agent.state["request"]
    assert r["service"] == "cleaning" and r.get("time_start") is None and r.get("budget") is None
    assert len({o["slot"][11:] for o in w.agent.state["options"]}) > 1
