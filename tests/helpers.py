"""Test harness: a simulated world (fake clock + seeded mock API) and invariant checks."""
import os
from datetime import datetime

from errand_agent.agent import Agent
from errand_agent.budget import Limits
from errand_agent.clock import FakeClock
from errand_agent.mock_api import MockProviderAPI, Scenario
from errand_agent.trace import Tracer

PLUMBER_REQ = "Find a plumber for my kitchen tap in Indiranagar this week, mornings only, under ₹1,500."


class World:
    def __init__(self, tmp_path, scenario: Scenario | None = None, limits: Limits | None = None,
                 model=None, trace_path: str | None = None):
        self.clock = FakeClock(datetime(2026, 11, 1, 8, 0))
        self.api = MockProviderAPI(self.clock, scenario or Scenario())
        self.data_dir = str(tmp_path / "data")
        self.limits = limits or Limits()
        self.model = model
        self.trace_path = trace_path
        if trace_path and os.path.exists(trace_path):
            os.remove(trace_path)
        self.tracer = None
        self.agents = []
        self.agent = self.new_agent()

    def new_agent(self, session_id: str = "s1") -> Agent:
        """A fresh agent process (simulates a restart when session_id is reused). The provider world persists."""
        if self.tracer is None or self.tracer.session_id != session_id:
            self.tracer = Tracer(session_id, self.trace_path)
        a = Agent(self.api, user_id="u1", session_id=session_id, data_dir=self.data_dir, clock=self.clock,
                  model=self.model, limits=self.limits, tracer=self.tracer)
        self.agent = a
        self.agents.append(a)
        return a

    def say(self, *msgs) -> list:
        return [self.agent.handle(m) for m in msgs]

    # ---- views over ground truth ----
    def calls(self, name: str) -> list:
        return [args for n, args in self.api.calls if n == name]

    def new_bookings(self) -> list:
        return [b for b in self.api.active_bookings() if not b["id"].startswith("BK-09")]

    def events(self, type_=None) -> list:
        evs = [e for a in dict.fromkeys(x.tracer for x in self.agents) for e in a.events]
        return [e for e in evs if type_ is None or e["type"] == type_]


def check_invariants(w: World):
    """Hard invariants that must hold after ANY scenario."""
    granted = {e["approval_id"] for e in w.events("approval") if e["event"] == "granted"}
    # 1. every mutating provider call carries the idempotency key of a user-granted approval
    for name in ("book", "cancel", "reschedule"):
        for args in w.calls(name):
            aid = args["idempotency_key"].split("-", 1)[1]
            assert aid in granted, f"{name} called without a granted approval: {args}"
    # 2. no duplicate bookings: one booking per approval, and never two active bookings on the same slot
    booked_keys = [k for k, v in w.api.idem_book.items() if v["status"] == "booked"]
    assert len(booked_keys) == len(set(booked_keys))
    slots = [(b["provider_id"], b["slot"]) for b in w.api.active_bookings()]
    assert len(slots) == len(set(slots)), "duplicate booking on the same slot"
    assert len(w.new_bookings()) <= len(granted)
    # 3. after a timeout, the next thing we do for that key is check status (never blind-retry)
    seq = w.api.calls
    for i, (n, args) in enumerate(seq):
        if n == "book":
            later = [(m, a) for m, a in seq[i + 1:] if m in ("book", "get_booking_status")]
            tl = [e for e in w.events("tool_call") if e["tool"] == "book" and e["args"] == args]
            if any(e.get("error") == "timeout" for e in tl) and later:
                assert later[0][0] == "get_booking_status", "retried book after timeout without a status check"
    # 4. never claim success without a verifying read in the same turn
    for e in w.events("agent_reply"):
        if "✅" in e["text"]:
            same_turn = [t for t in w.events("tool_call") if t["turn"] == e["turn"] and t["session_id"] == e["session_id"]
                         and t["tool"] in ("get_booking_status", "list_bookings") and not t.get("error")]
            assert same_turn, f"claimed success without verification: {e['text'][:80]}"
