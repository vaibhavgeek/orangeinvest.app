# Design note — Errand Agent

**Principle:** the model only turns text into a typed `Parsed` record (intent plus slots). Code owns every
decision: what to ask, which tool to call, whether something is approved, and what to tell the user. The model never
sees provider free text and can't call tools. Whatever it outputs is validated against a schema, and unknown fields
(`"approved": true`) are dropped and logged.

## State model
Each session is a persisted JSON record (`data/sessions/<id>.json`, written atomically after every transition):
`stage ∈ {idle, collecting, choosing, awaiting_approval, executing, done}`, `intent ∈ {book, reschedule, cancel}`,
`request` (the facts known so far), `options`, `hold`, `target`, `approval`, `execution`, and `pending_memory`.
- **Asking for what's missing:** `_advance_book` computes what is missing from `request` and fills gaps from memory first.
  It asks for all missing items in one message, and only for those. Budget is asked for once.
- **Corrections:** new slots are *merged* into `request`, so changing one field keeps the rest, and the search runs again. A `book` intent for a
  *different* service starts a fresh errand instead (bug 3).
- Every transition is written to the trace with a reason and a snapshot of the request.

## Approval enforcement
`approval.py` creates a record `{action, provider_id, slot, price, booking_id, status, idempotency_key, fingerprint}`.
`status` moves `pending → granted → consumed`, or to `voided`. Three independent layers enforce it:
1. **Only the agent's code grants.** `granted` is set in exactly one place, `_grant_and_execute`. That runs only when the stage is
   `awaiting_approval`, the *raw user text* passes a strict yes check (≤6 words, no hedges like "but", "actually" or "wait"), and the parser found
   no slot changes or new selection. "yes but tomorrow" is treated as a correction, not a yes.
2. **Any change voids.** A correction, a new selection, a new request, a declined reply, or a re-hold that quotes a different price all
   void the record. A new record with a new idempotency key is then required.
3. **The gateway checks every mutating call.** `ToolGateway.book/cancel/reschedule` refuse to call the API unless the
   approval is `granted`, its fingerprint is unchanged, and its provider, slot and price equal the *hold's* (from
   our own `hold_slot` result, not the caller's arguments). The idempotency key must also belong to that approval. Blocked attempts are traced
   as `approval_blocked`.

The eval invariant `check_invariants` checks this against ground truth after every scenario: every
`book/cancel/reschedule` the mock received carries the key of an approval that the user granted.

## Idempotency and recovery
- **One key per approval** (`book-<approval id>`), persisted *before* the call. Retries and restarts reuse it.
- **Write-ahead:** `execution = {key, status: in_flight, attempts}` is saved before each provider call. On start
  (`Agent.resume`) or on the next message, an `in_flight` record is **reconciled before anything else**. For book, we call
  `get_booking_status(key)`: if it says `booked`, we finish; if `not_found`, we resume with the same key, re-holding if needed. For cancel or reschedule
  we read `list_bookings`.
- **Timeout:** we never retry blindly. `book` timeout → `get_booking_status` → `booked` means done; `not_found`/`failed` means
  retry with the same key, up to 3 attempts, followed by a final status check. `slot_taken` → void the approval, exclude the slot, offer alternatives.
  `hold_expired` → re-hold, and if the price changed, void and ask again.
- **Never claim success until verified:** even after a clean `book` we read `get_booking_status`, and the ✅ text is only
  produced from that result. Cancel retries the transient 503 with backoff and the *same* key, then verifies.
- **Repeated "yes":** once the approval is consumed, a "yes" in the `done` stage just acknowledges. The user's own upcoming slots
  are filtered out of new searches, so they can't double-book themselves.

## Untrusted output
Reviews go through `sanitize_provider` at the gateway, *before* the agent sees them. Control characters and tags are stripped, text is
truncated, and an injection detector flags matches. Flagged reviews are withheld from the user ("1 review withheld"),
and safe ones are shown as quoted data. More fundamental: no tool text ever reaches the model or changes `request`,
memory or approval. The only things that can change those are the user's own words and our own structured tool fields
(price, slot, id). The eval checks that a review saying "area is Whitefield / pre-approved / book at ₹4,999" changes nothing.

## Bounded execution
`Budget` caps model calls (3) and tool calls (25) per turn, wall-clock time per turn (30 s, using an injectable clock), turns per
session (40), and attempts per action (3). Every exit goes through `Agent._guarded`, which turns `LimitExceeded`,
`ApprovalError` or any other exception into a plain message plus a status summary that is accurate about outcomes ("Nothing was
booked…" or "An action may be in progress; I'll verify it before doing anything else.").

## Memory
`data/memory/<user>.json` stores facts `{value, source: stated|inferred, confirmed, status: active|forgotten}`.
`usable()` returns a value only if it was stated, or inferred *and confirmed*. Inferred facts (the provider you last
booked) trigger a yes/no question before they are used. Answering "no" forgets them. "Forget X" leaves a tombstone with the value erased,
and `infer()` refuses to bring a tombstoned key back. Only the user stating it again does. The user can view facts ("what do you remember?",
which shows the source label), correct them ("I live in HSR Layout"), or forget one or all of them.

## Observability
One JSONL file per session run (`traces/<session>.jsonl`). It records `run_start`, `user_message`, `model_call` (model,
tokens, cost, latency, parsed output, dropped fields), `state_transition`, `tool_call` (name, args, *sanitised* result
summary, error, latency), `approval` (created/granted/voided/consumed with fingerprint), `recovery`, `limit_exceeded`,
`error`, `agent_reply`, and `turn_end` (call counts, elapsed time). Secrets are redacted by key name and by value pattern (`sk-…`, `Bearer …`).
`python -m errand_agent.tracefmt --follow` shows it live.

**Annotated excerpt: timeout after success** (`docs/traces/timeout_after_success.jsonl`; approval ids change on every `pytest` run)
```
[t2] TOOL     hold_slot(PLB-10, 2026-11-01T11:00) -> {hold_id H-1001, quoted_price 800}
[t2] APPROVAL created s1.1.d4dc63 {book, PLB-10, 11:00, 800}     <- record shown to the user verbatim
[t3] USER     yes
[t3] APPROVAL granted s1.1.d4dc63                                 <- granted by code, from the raw "yes"
[t3] STATE    awaiting_approval -> executing                      <- execution {key, in_flight} persisted FIRST
[t3] TOOL     book(H-1001, book-s1.1.d4dc63) -> ERROR timeout     <- outcome unknown; do NOT retry yet
[t3] TOOL     get_booking_status(book-s1.1.d4dc63) -> booked BK-1002   <- source of truth: it did book
[t3] APPROVAL consumed s1.1.d4dc63                                <- can never be used again
[t3] STATE    executing -> done ; AGENT "✅ Booked and verified … BK-1002"   (exactly 1 book call)
```

## What breaks first at 100×, and what I'd change
1. **JSON files per session and user.** There is no locking: two devices on one account race and the last write wins. I'd move to Postgres
   with an `executions` table (unique on idempotency key) and optimistic version checks on the session row.
2. **In-process reconciliation only on the user's next message.** An in-flight booking for a user who never comes
   back stays unverified. I'd add an outbox plus a background reconciler that sweeps `in_flight` executions and notifies the user.
3. **Synchronous provider calls inside the chat turn.** Slow providers use up the 30 s turn budget. Execution should move to a job
   queue with per-provider rate limits and circuit breakers, with the chat streaming status updates.
4. **The rule-based parser's coverage** (phrasings, areas, Hindi/Hinglish). I'd put an LLM extractor behind the same
   `Parsed` schema (the `LLMModel` adapter is there) and grow a recorded-conversation eval set to measure it. The rules stay as a fallback.
5. **Traces as local files.** I'd ship them to OpenTelemetry with sampling, and alert on `approval_blocked`, `recovery` and limit exits.
