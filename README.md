# Errand Agent

A chat agent that books, reschedules and cancels home-service errands against a mocked provider API. It
won't double-book, it won't act without your approval, and nothing a provider review says can talk it into anything.

Python 3.10+, no runtime dependencies (pytest is only needed to run the tests). It runs fully offline.

**Live test console:** https://main.d2wl4x6lwymmcp.amplifyapp.com (frontend on AWS Amplify, backend on EC2 at
https://3-111-160-146.sslip.io). Each browser tab gets its own isolated session.

## Run it (3 commands)

```bash
pip install -r requirements.txt                    # just pytest
pytest                                             # 97 tests: 28 eval scenarios + 32 red-team playbooks + units
./try_it.sh web                                    # backend :8000 + React console :5173 (needs Node 18+)
```

The web console (React + TypeScript, `web/`) shows the chat next to the live trace, the workflow state, memory, and the provider's ground-truth
bookings. It has controls to inject failure scenarios, fast-forward the clock (expire holds) and kill/restart the
process, plus one-click **red-team playbooks**: see [docs/RED_TEAM.md](docs/RED_TEAM.md). A terminal chat is also
available: `python -m errand_agent.cli --fresh --scenario timeout_after_success`.

To watch the trace while you chat, run this in a second terminal pane:

```bash
python -m errand_agent.tracefmt --follow traces/demo.jsonl
```

Try: `Find a plumber for my kitchen tap in Indiranagar this week, mornings only, under ₹1,500` → `1` → `yes`.
Other things to try: `Move my AC servicing to Saturday`, `Book the same electrician as last time, but not before 10 a.m.`,
`Cancel my AC servicing booking`, `what do you remember?`, `forget my area`.

`--scenario` picks the failure mode for the mock world: `default` (seeded 70/15/15 book outcomes),
`timeout_after_success`, `timeout_not_booked`, `slot_taken`, `price_change`, `crash_after_book`.
In `crash_after_book` the process dies right after the provider accepts the booking. Run the same command again
without `--fresh` and the agent reconciles on start.

The agent uses OpenAI only (`gpt-6.1-terra` by default; override with `OPENAI_MODEL`). The key is read from
`OPENAI_API_KEY` or a gitignored `.env` file (`OPENAI_API_KEY=sk-...`); the CLI and the backend refuse to start without it.

OpenAI does two jobs per turn. It **parses** the message into the typed `Parsed` record: unknown fields such as
`"approved": true` are dropped, and the model names the day (`tomorrow`, `next_week`, `10-15`) while code computes
the dates. Then it **phrases the reply** from the facts code decided. Tables, slot lists, the "Please confirm: …" line
and the one-click suggestions are appended verbatim and never pass through the model, so prices and slots can't be
altered. Approval is still decided by code from the raw text (strict yes/no check), never by the model. If the model
is unreachable while parsing, the agent says so and changes nothing; if it fails while phrasing, the plain facts are sent.

Tests and red-team playbooks replay recorded OpenAI responses (`tests/cassettes/openai.json`), so `pytest` needs no
network or key. After a prompt change, re-record with `CASSETTE_MODE=record python3 -m pytest` (add `CASSETTE_PRUNE=1`
on a full run to drop unused recordings).

## Layout

| Path | What |
|---|---|
| `errand_agent/mock_api.py` | The fixed tool contract as a seeded mock, with configurable failure scenarios |
| `errand_agent/tools.py` | `ToolGateway`: budget, tracing, sanitising, and the **approval check on every mutating call** |
| `errand_agent/agent.py` | The state machine: collect → choose → approve → execute → verify |
| `errand_agent/approval.py` | Approval records, fingerprints, strict yes/no detection |
| `errand_agent/nlu.py` | `OpenAIModel`: parse to the `Parsed` schema, `resolve_when` (code-owned dates), reply phrasing |
| `errand_agent/cassette.py` | Record/replay of OpenAI responses for offline tests |
| `errand_agent/memory.py` | Persistent preferences marked stated/inferred, with confirm and forget |
| `errand_agent/state.py`, `trace.py`, `budget.py` | Persisted workflow state, JSONL traces, hard limits |
| `tests/test_evals.py` | 28 scripted scenarios with invariant checks → `docs/EVAL_RESULTS.md` |
| `tests/test_units.py` | Guardrail unit tests + regression tests for the 3 bugs |
| `errand_agent/web.py` | JSON backend for the console (stdlib `http.server`), one isolated session per browser tab |
| `web/` | React + TypeScript (Vite) test console; `errand_agent/static/playbooks.json` holds the red-team playbooks |
| `deploy/`, `amplify.yml` | EC2 setup (systemd + Caddy HTTPS) and the Amplify build spec |
| `errand_agent/redteam.py`, `tests/test_playbooks.py` | Run the playbooks offline → `docs/RED_TEAM_RESULTS.md` |
| `docs/RED_TEAM.md` | Attacks/edge cases to try, what must happen, what red-teaming fixed |
| `docs/DESIGN.md` | Design note |
| `docs/BUGS.md` | Three real bugs hit while building |
| `docs/traces/timeout_after_success.jsonl` | Raw end-to-end trace (regenerated by `pytest`) |
| `docs/DEMO.md` | Script for the 3–5 min recording |

## Assumptions (where the brief was ambiguous)

- **The API is per-user authenticated.** `hold_slot`/`book` take no user argument, so the client carries the user, the way an auth header would.
- **Hold before approval.** We hold first so the user approves the *quoted* price, not the search price. If the hold expires and the re-hold quotes a different price, the approval is void.
- **Every `get_provider` response includes at least one injected review**, which is the strictest reading of the brief.
- **`get_booking_status` only knows about `book` keys.** Cancel and reschedule are verified by reading `list_bookings` afterwards.
- **The provider dedupes `book` by idempotency key**, which is realistic. The agent doesn't rely on that: it reuses one key per approval and always checks status after a timeout.
- **Budget is asked for once.** If the user ignores the question, we search without a cap rather than nagging. Preferences are never asked for; they come from memory or the request.
- **What gets remembered:** an area mentioned in a request is saved as *stated*, and the agent says so. A time constraint in a request applies to that request only, unless the user says "remember" or "from now on". A provider you booked becomes an *inferred* preference that the agent asks about before using.
- **"This week"** means today plus 6 days. A weekday name means its next occurrence.

## Deployment

- **Frontend:** AWS Amplify app connected to this repo (`amplify.yml`, app root `web/`). Every push to `main`
  rebuilds. The build-time env var `VITE_API_URL` points at the backend.
- **Backend:** Ubuntu 24.04 on EC2 (t4g.small, ap-south-1, Elastic IP). `deploy/setup_ec2.sh` installs the service
  under systemd and puts Caddy in front for automatic HTTPS on `<ip>.sslip.io`. To update after a push:
  `ssh ubuntu@<ip> 'sudo DOMAIN=3-111-160-146.sslip.io bash /opt/errand-agent/deploy/setup_ec2.sh'`.
- `OPENAI_API_KEY` lives on the server in `/etc/errand-agent.env`, which is never committed; the backend won't start
  without it. Messages are capped per day (`LLM_DAILY_CAP`).
