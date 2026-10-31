# Three real bugs hit while building

## 1. A crash in the middle of a call left a hole in the trace

**What broke.** I was running the `crash_after_book` CLI scenario, and the restarted agent correctly reported
"Picking up where we left off: ✅ Booked". But the trace contained no `book` line at all. Reading the trace, the
booking seemed to appear from nowhere, and that is exactly the incident you would need the trace to explain.

**Why.** `ToolGateway._call` only logged a call after it returned, or inside `except ToolError` / `except Exception`.
A process death is modelled as `SimulatedCrash(BaseException)`. That is deliberate, so the agent's own error handling can't
swallow it. But it also skipped both except blocks, so the call was never written to the trace.

**Fix.** The fallback handler is now `except BaseException`. It writes the `tool_call` line (with the error) and then re-raises.
Trace lines are appended and flushed per event, so the line survives the crash.

**Regression tests.** `tests/test_units.py::test_bug1_crash_during_tool_call_is_still_traced`, plus an assertion in
`test_evals.py::test_restart_after_provider_booked`.

## 2. "Seeded" runs weren't reproducible

**What broke.** `test_happy_path_booking` failed with 0 bookings. When I ran the exact same flow by hand straight
afterwards, it booked successfully. With no forced outcome, the mock sometimes returned `slot_taken` and sometimes
`success`, even though the seed was the same.

**Why.** The mock drew the 70/15/15 outcome from `hash(seed, idempotency_key, attempt)`. The idempotency key comes from
the approval id, which was a `uuid4`. So every run hashed a different key and got a different outcome. The seed meant nothing.

**Fix.** The outcome is now drawn from `hash(seed, global book-call count)`. That depends only on the sequence of calls, not on
the ids. Approval ids are now `"{session}.{seq}.{short uuid}"`, so traces are readable and keys stay unique across worlds.

**Regression test.** `test_units.py::test_bug2_seeded_runs_are_reproducible` runs 4 bookings twice with no forced
outcomes and asserts the two tool/outcome sequences are identical. It fails against the old code.

## 3. A new errand inherited the previous errand's constraints

**What broke.** In exploratory chat: "Need an electrician in Jayanagar tomorrow afternoon under 500" → "at 2pm" →
"Find a cleaner in Whitefield this weekend". The cleaner options were all at 14:00, and the budget was still 500.

**Why.** The rule was: if a booking intent arrives while a booking flow is open, treat it as a correction to that flow.
That's right for "actually make it under 1000", but wrong for a request about a *different service*. The merge logic kept every
unrelated field, which is the correction behaviour the brief asks for, applied where it shouldn't be.

**Fix.** A `book` intent that names a different service from the open request now starts a fresh flow, and any
pending approval is voided. Stable facts still come back from memory (area, earliest time). Per-errand constraints do not.

**Regression test.** `test_units.py::test_bug3_new_errand_does_not_inherit_old_constraints`.
`test_evals.py::test_correction_keeps_other_facts` still guards the opposite case: a real correction keeps everything else.

I checked all three regression tests by reverting each fix. Each test fails without its fix and passes with it.
