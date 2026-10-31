"""ToolGateway: the single choke point between the agent and the provider API.

- every call is budget-charged and traced (args + sanitised result + latency)
- mutating calls (book / cancel / reschedule) require a granted approval whose
  fingerprint matches the exact provider, slot and price being acted on
- provider free text is sanitised before it reaches the agent
"""
import time

from .approval import ApprovalError, require
from .mock_api import ToolError
from .sanitize import sanitize_provider


class ToolGateway:
    def __init__(self, api, user_id: str, budget, tracer, clock):
        self.api = api
        self.user_id = user_id
        self.budget = budget
        self.tracer = tracer
        self.clock = clock
        self.holds: dict = {}  # hold_id -> hold as returned by hold_slot (trusted, from our own call)

    def register_hold(self, hold: dict):
        self.holds[hold["hold_id"]] = hold

    def _call(self, name: str, args: dict, summarize=None):
        self.budget.charge_tool()
        t0 = time.perf_counter()
        try:
            result = getattr(self.api, name)(**args)
        except ToolError as e:
            self.tracer.event("tool_call", tool=name, args=args, error=e.code, transient=e.transient,
                              latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            raise
        except BaseException as e:  # BaseException: a crash mid-call must still leave a trace line
            self.tracer.event("tool_call", tool=name, args=args, error=f"unexpected: {e!r}"[:200],
                              latency_ms=round((time.perf_counter() - t0) * 1000, 1))
            raise
        self.tracer.event("tool_call", tool=name, args=args,
                          result=summarize(result) if summarize else result,
                          latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        return result

    # ---- read-only ----
    def search_providers(self, service, area, date_range):
        return self._call("search_providers", {"service": service, "area": area, "date_range": date_range},
                          summarize=lambda r: [{"id": p["id"], "price": p["price"], "rating": p["rating"],
                                                "n_free_slots": len(p["free_slots"])} for p in r])

    def get_provider(self, provider_id):
        raw = self._call("get_provider", {"provider_id": provider_id},
                         summarize=lambda r: sanitize_provider(r))
        return sanitize_provider(raw)

    def list_bookings(self):
        return self._call("list_bookings", {"user_id": self.user_id})

    def get_booking_status(self, idempotency_key):
        return self._call("get_booking_status", {"idempotency_key": idempotency_key})

    def hold_slot(self, provider_id, slot):
        res = self._call("hold_slot", {"provider_id": provider_id, "slot": slot})
        hold = {"hold_id": res["hold_id"], "provider_id": provider_id, "slot": slot,
                "quoted_price": res["quoted_price"], "expires_at": res["expires_at"]}
        self.register_hold(hold)
        return hold

    # ---- mutating: approval enforced here, not in the prompt ----
    def _guard(self, fn, *a, **kw):
        try:
            fn(*a, **kw)
        except ApprovalError as e:
            self.tracer.event("approval_blocked", reason=str(e))
            raise

    def book(self, hold_id, idempotency_key, approval):
        hold = self.holds.get(hold_id)
        if not hold:
            self.tracer.event("approval_blocked", reason=f"unknown hold {hold_id}")
            raise ApprovalError(f"book: unknown hold {hold_id}")
        self._guard(require, approval, "book", provider_id=hold["provider_id"], slot=hold["slot"],
                    price=hold["quoted_price"])
        if idempotency_key != approval["idempotency_key"]:
            self._guard(_raise, "book: idempotency key does not belong to this approval")
        return self._call("book", {"hold_id": hold_id, "idempotency_key": idempotency_key})

    def cancel(self, booking_id, idempotency_key, approval):
        self._guard(require, approval, "cancel", booking_id=booking_id)
        return self._call("cancel", {"booking_id": booking_id, "idempotency_key": idempotency_key})

    def reschedule(self, booking_id, new_slot, idempotency_key, approval):
        self._guard(require, approval, "reschedule", booking_id=booking_id, slot=new_slot)
        return self._call("reschedule", {"booking_id": booking_id, "new_slot": new_slot,
                                         "idempotency_key": idempotency_key})


def _raise(msg):
    raise ApprovalError(msg)
