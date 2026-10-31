"""The errand agent: an explicit state machine. The model only parses text; this code decides."""
import os
import uuid
from datetime import datetime, timedelta

from .approval import ApprovalError, is_affirmative, is_negative, new_approval
from .budget import Budget, LimitExceeded, Limits
from .clock import SystemClock
from .memory import MemoryStore, label, show_value
from .mock_api import AREAS, ToolError, ToolTimeout
from .nlu import RuleBasedModel
from .state import StateStore, new_state
from .tools import ToolGateway

SERVICE_NAMES = {"plumber": "plumber", "electrician": "electrician", "ac_service": "AC servicing",
                 "cleaning": "home cleaning", "carpenter": "carpenter", "pest_control": "pest control"}


def fmt_slot(slot: str) -> str:
    return datetime.fromisoformat(slot).strftime("%a %d %b, %H:%M")


def fmt_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def rupees(n) -> str:
    return f"₹{int(n):,}"


class Agent:
    def __init__(self, api, user_id: str = "u1", session_id: str = "s1", data_dir: str = "data",
                 clock=None, model=None, limits: Limits | None = None, trace_dir: str | None = None,
                 tracer=None):
        from .trace import Tracer
        self.clock = clock or SystemClock()
        self.limits = limits or Limits()
        self.budget = Budget(self.limits, self.clock)
        self.store = StateStore(data_dir)
        self.state = self.store.load(session_id) or new_state(session_id, user_id)
        self.memory = MemoryStore(os.path.join(data_dir, "memory", f"{user_id}.json"))
        trace_path = os.path.join(trace_dir, f"{session_id}.jsonl") if trace_dir else None
        self.tracer = tracer or Tracer(session_id, trace_path)
        self.tracer.turn = self.state["turns"]
        self.model = model or RuleBasedModel()
        self.tools = ToolGateway(api, user_id, self.budget, self.tracer, self.clock)
        if self.state.get("hold"):
            self.tools.register_hold(self.state["hold"])
        self._effects: list = []
        self.tracer.event("run_start", user_id=user_id, stage=self.state["stage"], model=self.model.name,
                          limits=vars(self.limits), resumed=self.state["turns"] > 0)

    # ================= entry points =================
    def resume(self) -> str | None:
        """Call after a (re)start: reconciles any action that was in flight when we stopped."""
        if not self._in_flight():
            return None
        return self._guarded(self._reconcile)

    def handle(self, text: str) -> str:
        self.state["turns"] += 1
        self.tracer.turn = self.state["turns"]
        self.tracer.event("user_message", text=text)
        if self.state["turns"] > self.limits.max_turns:
            reply = self._limit_message(LimitExceeded("turns", self.limits.max_turns))
            self.tracer.event("limit_exceeded", kind="turns", limit=self.limits.max_turns)
            self.tracer.event("agent_reply", text=reply)
            self._save()
            return reply
        return self._guarded(lambda: self._step(text))

    def _guarded(self, fn) -> str:
        self.budget.start_turn()
        self._effects = []
        try:
            reply = fn()
        except LimitExceeded as e:
            self.tracer.event("limit_exceeded", kind=e.kind, limit=e.limit)
            reply = self._limit_message(e)
        except ApprovalError as e:  # should be unreachable; the gateway blocked an unapproved action
            self.tracer.event("error", error=f"approval_blocked: {e}")
            self._void("blocked by approval guard")
            reply = "I stopped before making any change because the action didn't match what you approved. " \
                    + self._status_summary()
        except Exception as e:
            self.tracer.event("error", error=repr(e)[:300])
            reply = "Sorry, something unexpected went wrong on my side, so I stopped. " + self._status_summary()
        self._save()
        self.tracer.event("agent_reply", text=reply, stage=self.state["stage"])
        self.tracer.event("turn_end", tool_calls=self.budget.tool_calls, model_calls=self.budget.model_calls,
                          elapsed_s=round(self.clock.monotonic() - self.budget.t0, 3))
        return reply

    # ================= helpers =================
    def _save(self):
        self.store.save(self.state)

    def _transition(self, stage: str, reason: str):
        old = self.state["stage"]
        self.state["stage"] = stage
        self.tracer.event("state_transition", **{"from": old}, to=stage, reason=reason,
                          request={k: v for k, v in self.state["request"].items() if k != "excluded"})
        self._save()

    def _in_flight(self) -> bool:
        ex = self.state.get("execution")
        return bool(ex and ex["status"] == "in_flight")

    def _status_summary(self) -> str:
        if self._in_flight():
            return ("An action may be in progress with the provider; I'll verify its real status before doing "
                    "anything else. Send any message to continue.")
        if self._effects:
            return "What did complete: " + "; ".join(self._effects) + "."
        return "Nothing was booked, cancelled or changed in this step."

    def _limit_message(self, e: LimitExceeded) -> str:
        if e.kind == "turns":
            return (f"This conversation has reached its limit of {e.limit} messages, so I've stopped. "
                    + self._status_summary() + " Please start a new session to continue.")
        desc = {"tool_calls": f"{e.limit} provider calls", "model_calls": f"{e.limit} model calls",
                "wall_clock_seconds": f"{e.limit:g} seconds"}.get(e.kind, str(e.limit))
        return f"I stopped because this step hit its safety limit ({desc}). " + self._status_summary()

    def _parse(self, text: str):
        self.budget.charge_model()
        ctx = {"today": self.clock.now().date(), "stage": self.state["stage"], "n_options": len(self.state["options"])}
        p, usage, dropped = self.model.parse(text, ctx)
        out = {k: v for k, v in vars(p).items() if v not in (None, False)}
        self.tracer.event("model_call", **usage, output=out, dropped_fields=dropped)
        return p

    def _void(self, reason: str):
        ap = self.state.get("approval")
        if ap and ap["status"] in ("pending", "granted"):
            ap["status"] = "voided"
            self.tracer.event("approval", event="voided", approval_id=ap["id"], reason=reason)

    def _hold_valid(self, hold) -> bool:
        return bool(hold) and datetime.fromisoformat(hold["expires_at"]) > self.clock.now() + timedelta(seconds=20)

    # ================= main step =================
    def _step(self, text: str) -> str:
        S = self.state
        if self._in_flight():
            msg = self._reconcile()
            return msg

        p = self._parse(text)

        if p.intent in ("memory_view", "memory_forget", "memory_set"):
            return self._memory_op(p)

        if S.get("pending_memory"):
            pm, S["pending_memory"] = S["pending_memory"], None
            if is_affirmative(text):
                self.memory.confirm(pm["key"])
                S["request"]["provider"] = pm["value"]
                S["request"]["provider_src"] = "memory"
                return f"Great, I'll look for {pm['value']['name']}. " + self._advance()
            if is_negative(text):
                self.memory.forget(pm["key"])
                return "OK, I won't use that and I've dropped it from memory. " + self._advance()
            # anything else: leave the inference unconfirmed (unused) and handle the message normally

        stage = S["stage"]
        updates = p.slot_updates()

        if stage == "awaiting_approval":
            if is_affirmative(text) and not updates and p.selection is None and p.intent in (None, S["intent"]):
                return self._grant_and_execute()
            if is_negative(text) and not updates:
                self._void("user declined")
                if S["options"]:
                    self._transition("choosing", "approval declined")
                    return "OK, I won't do that. Pick another option or tell me what to change.\n" + \
                        self._options_text()
                self._transition("idle", "approval declined")
                return "OK, I won't do that. Nothing was changed."
            if not updates and p.selection is None and p.intent in (None, S["intent"]):
                return "I need a clear yes or no.\n" + self._approval_text(S["approval"])
            self._void("user changed the request")

        if stage == "done" and is_affirmative(text) and not updates and p.intent is None:
            lr = S.get("last_result") or {}
            return f"That's already done ({lr.get('summary', 'completed')}). I won't do it again."

        new_errand = (p.intent == "book" and S["intent"] == "book" and p.service
                      and p.service != S["request"].get("service"))
        if p.intent in ("book", "reschedule", "cancel") and (
                p.intent != S["intent"] or stage in ("idle", "done") or new_errand):
            return self._start_flow(p)

        if S["intent"] is None or stage in ("idle", "done"):
            if updates.get("service") or updates.get("same_as_last"):
                p.intent = "book"
                return self._start_flow(p)
            if is_affirmative(text) or is_negative(text):
                return "There's nothing waiting for your confirmation right now. What can I help you with?"
            return ("I can book, reschedule or cancel home services, e.g. 'Find a plumber in Indiranagar this "
                    "week under ₹1,500', 'Move my AC servicing to Saturday' or 'Cancel my plumber booking'. "
                    "I can also show or forget what I remember about you.")

        if updates:
            self._merge(updates)
            if S["stage"] == "awaiting_approval":
                self._transition("collecting", "request corrected")
            if S["intent"] == "book":
                S["options"] = []
            elif S["intent"] == "reschedule" and S.get("target"):
                S["options"] = []
            return self._advance()

        if p.selection is not None and S["options"]:
            return self._select(p.selection)

        if S["stage"] == "choosing":
            return "Please pick an option by number, or tell me what to change.\n" + self._options_text()
        return self._advance()

    def _start_flow(self, p) -> str:
        S = self.state
        self._void("new request")
        S.update({"intent": p.intent, "request": {}, "options": [], "hold": None, "target": None,
                  "approval": None, "execution": None, "pending_memory": None})
        self._merge(p.slot_updates())
        self._transition("collecting", f"new {p.intent} request")
        return self._advance()

    def _merge(self, updates: dict):
        r = self.state["request"]
        for k, v in updates.items():
            r[k] = v
        if "budget" in updates:
            r.pop("no_budget", None)
        if updates.get("no_budget"):
            r.pop("budget", None)
        if "area" in updates:
            r["area_src"] = "user"
            if self.memory.usable("area") != updates["area"]:
                self.memory.state("area", updates["area"])
                r["_note"] = f"I'll remember {updates['area']} as your area (say 'forget my area' anytime)."

    def _advance(self) -> str:
        intent = self.state["intent"]
        if intent == "book":
            return self._advance_book()
        if intent == "reschedule":
            return self._advance_reschedule()
        if intent == "cancel":
            return self._advance_cancel()
        return "What would you like to do?"

    # ================= memory =================
    def _memory_op(self, p) -> str:
        S, r = self.state, self.state["request"]
        if p.intent == "memory_view":
            return self.memory.describe()
        if p.intent == "memory_forget":
            keys = self.memory.forget(p.memory_key)
            if r.get("area_src") == "memory" and any(k == "area" for k in keys):
                r.pop("area", None); r.pop("area_src", None)
            if r.get("provider_src") == "memory" and any(k.startswith("preferred_provider") for k in keys):
                r.pop("provider", None); r.pop("provider_src", None)
            if not keys:
                return "I didn't have anything saved for that."
            return "Done — I've forgotten: " + ", ".join(label(k) for k in keys) + ". I won't use it again."
        # memory_set
        self.memory.state(p.memory_key, p.memory_value)
        msg = f"Got it — saved {label(p.memory_key)}: {show_value(p.memory_key, p.memory_value)} (stated by you)."
        if S["intent"] and S["stage"] in ("collecting", "choosing", "awaiting_approval"):
            if p.memory_key == "area":
                r["area"], r["area_src"] = p.memory_value, "user"
            self._void("preferences changed")
            S["options"] = []
            if S["stage"] == "awaiting_approval":
                self._transition("collecting", "preferences changed")
            return msg + " " + self._advance()
        return msg

    def _earliest(self):
        r = self.state["request"]
        if r.get("time_exact"):
            return None
        return self.memory.usable("earliest_time")

    # ================= booking flow =================
    def _advance_book(self) -> str:
        S, r = self.state, self.state["request"]
        notes = [r.pop("_note")] if r.get("_note") else []
        if not r.get("area"):
            a = self.memory.usable("area")
            if a:
                r["area"], r["area_src"] = a, "memory"
                notes.append(f"Using your saved area: {a}.")
        if r.get("same_as_last") and not r.get("provider"):
            past = [b for b in self.tools.list_bookings() if b["status"] != "cancelled"
                    and (not r.get("service") or b["service"] == r["service"])]
            if not past:
                r.pop("same_as_last")
                notes.append("I couldn't find a previous booking to match, so I'll search all providers.")
            else:
                b = past[-1]
                r["provider"], r["provider_src"] = {"id": b["provider_id"], "name": b["provider_name"]}, "user"
                r["service"] = b["service"]
                if not r.get("area") and b.get("area"):
                    r["area"], r["area_src"] = b["area"], "history"
                notes.append(f"Looking for {b['provider_name']}, who you booked on {fmt_slot(b['slot'])}.")
        svc = r.get("service")
        if svc and not r.get("provider") and not r.get("pref_asked"):
            key = f"preferred_provider:{svc}"
            f = self.memory.get(key)
            if f and f["source"] == "inferred" and not f["confirmed"]:
                r["pref_asked"] = True
                S["pending_memory"] = {"key": key, "value": f["value"]}
                self._save()
                return " ".join(notes + [f"Last time you booked {f['value']['name']} for {SERVICE_NAMES[svc]}. "
                                         f"Should I look for them again? (yes/no)"])
            val = self.memory.usable(key)
            if val:
                r["provider"], r["provider_src"] = val, "memory"
                notes.append(f"Using your preferred provider {val['name']}.")
        missing = []
        if not svc:
            missing.append("what service you need (plumber, electrician, AC servicing, cleaning, carpenter, pest control)")
        if not r.get("area"):
            missing.append(f"your area ({', '.join(AREAS)})")
        if not r.get("date_from"):
            missing.append("when (e.g. 'tomorrow', 'this week', 'Saturday')")
        if r.get("budget") is None and not r.get("no_budget") and not r.get("budget_asked") and not r.get("provider"):
            missing.append("your budget (or say 'no budget')")
            r["budget_asked"] = True
        if missing:
            if S["stage"] != "collecting":
                self._transition("collecting", "missing details")
            return " ".join(notes + ["To find options I still need: " + "; ".join(missing) + "."])
        return " ".join(notes + [self._search_and_present()]).strip()

    def _window(self):
        r = self.state["request"]
        start, end = r.get("time_start") or 0, r.get("time_end") or 24 * 60
        e = self._earliest()
        if e is not None:
            start = max(start, e)
        return start, end

    def _search_and_present(self) -> str:
        S, r = self.state, self.state["request"]
        results = self.tools.search_providers(r["service"], r["area"], [r["date_from"], r["date_to"]])
        mine = {b["slot"] for b in self.tools.list_bookings() if b["status"] == "upcoming"}
        start, end = self._window()
        excluded = set(r.get("excluded", []))
        cands = []
        for p in results:
            if r.get("provider") and p["id"] != r["provider"]["id"]:
                continue
            if r.get("budget") and p["price"] > r["budget"]:
                continue
            slots = [s for s in p["free_slots"]
                     if start <= datetime.fromisoformat(s).hour * 60 < end and s not in mine
                     and f"{p['id']}|{s}" not in excluded]
            if slots:
                cands.append((p, slots))
        cands.sort(key=lambda c: (-c[0]["rating"], c[0]["price"]))
        options = [{"provider_id": p["id"], "provider_name": p["name"], "slot": s, "price": p["price"],
                    "rating": p["rating"], "attributes": p["attributes"]}
                   for p, slots in cands[:3] for s in slots[:2]]
        if not options:
            S["options"] = []
            self._transition("collecting", "no matching options")
            who = r["provider"]["name"] if r.get("provider") else f"{SERVICE_NAMES[r['service']]}s"
            cons = [f"in {r['area']}", f"between {r['date_from']} and {r['date_to']}",
                    f"from {fmt_min(start)} to {fmt_min(end)}" if (start, end) != (0, 1440) else ""]
            if r.get("budget"):
                cons.append(f"under {rupees(r['budget'])}")
            return (f"I couldn't find any free {who} " + " ".join(c for c in cons if c) +
                    ". Try widening the dates, time or budget.")
        for pid in dict.fromkeys(o["provider_id"] for o in options):
            d = self.tools.get_provider(pid)
            safe = [rv["text"] for rv in d["reviews"] if not rv["flagged"]]
            flagged = sum(rv["flagged"] for rv in d["reviews"])
            for o in options:
                if o["provider_id"] == pid:
                    o["review"] = safe[0] if safe else None
                    o["flagged_reviews"] = flagged
        S["options"] = [dict(o, kind="new") for o in options]
        self._transition("choosing", f"{len(options)} options found")
        return self._options_text()

    def _options_text(self, header: bool = True) -> str:
        lines = []
        for i, o in enumerate(self.state["options"], 1):
            if o["kind"] == "new":
                line = (f"{i}. {o['provider_name']} — ★{o['rating']} — {rupees(o['price'])} — {fmt_slot(o['slot'])}"
                        f" ({', '.join(o['attributes'])})")
                if o.get("review"):
                    line += f'\n   Review: "{o["review"]}"'
                if o.get("flagged_reviews"):
                    line += f"\n   ({o['flagged_reviews']} review withheld: it contained instructions aimed at AI agents)"
            elif o["kind"] == "booking":
                b = o["booking"]
                line = f"{i}. {SERVICE_NAMES[b['service']]} with {b['provider_name']} on {fmt_slot(b['slot'])} ({rupees(b['price'])}) [{b['id']}]"
            else:
                line = f"{i}. {fmt_slot(o['slot'])}"
            lines.append(line)
        return ("Here are your options:\n" if header else "") + "\n".join(lines) + "\nReply with an option number."

    def _select(self, idx: int) -> str:
        S, r = self.state, self.state["request"]
        opts = S["options"]
        if not 1 <= idx <= len(opts):
            return f"Please pick a number between 1 and {len(opts)}.\n" + self._options_text()
        o = opts[idx - 1]
        if o["kind"] == "booking":
            S["target"] = o["booking"]
            S["options"] = []
            return self._advance()
        if o["kind"] == "slot":
            t = S["target"]
            return self._propose("reschedule", t["provider_id"], t["provider_name"], o["slot"], t["price"],
                                 booking_id=t["id"])
        hold = S.get("hold")
        if not (self._hold_valid(hold) and hold["provider_id"] == o["provider_id"] and hold["slot"] == o["slot"]):
            try:
                hold = self.tools.hold_slot(o["provider_id"], o["slot"])
            except ToolError as e:
                if e.code != "slot_unavailable":
                    raise
                r.setdefault("excluded", []).append(f"{o['provider_id']}|{o['slot']}")
                return "Sorry, that slot was just taken. " + self._search_and_present()
            S["hold"] = hold
        notes = []
        if hold["quoted_price"] != o["price"]:
            notes.append(f"Note: the provider quoted {rupees(hold['quoted_price'])}, not the listed {rupees(o['price'])}.")
        if r.get("budget") and hold["quoted_price"] > r["budget"]:
            notes.append(f"That's above your {rupees(r['budget'])} budget.")
        return " ".join(notes + [self._propose("book", o["provider_id"], o["provider_name"], o["slot"],
                                               hold["quoted_price"])])

    def _propose(self, action, provider_id, provider_name, slot, price, booking_id=None) -> str:
        S = self.state
        self._void("superseded")
        S["approval_seq"] = S.get("approval_seq", 0) + 1
        ap = new_approval(action, provider_id, provider_name, slot, price, booking_id,
                          now=self.clock.now().isoformat(),
                          approval_id=f"{S['session_id']}.{S['approval_seq']}.{uuid.uuid4().hex[:6]}")
        S["approval"] = ap
        self.tracer.event("approval", event="created", approval_id=ap["id"], action=action,
                          provider_id=provider_id, slot=slot, price=price, booking_id=booking_id,
                          fingerprint=ap["fingerprint"])
        self._transition("awaiting_approval", f"{action} proposed")
        return self._approval_text(ap)

    def _approval_text(self, ap) -> str:
        svc = SERVICE_NAMES.get(self.state["request"].get("service") or (self.state.get("target") or {}).get("service"), "")
        if ap["action"] == "book":
            what = f"BOOK {ap['provider_name']} ({ap['provider_id']}) for {svc} on {fmt_slot(ap['slot'])} at {rupees(ap['price'])}"
        elif ap["action"] == "cancel":
            what = (f"CANCEL booking {ap['booking_id']}: {ap['provider_name']} on {fmt_slot(ap['slot'])} "
                    f"({rupees(ap['price'])})")
        else:
            old = self.state["target"]["slot"]
            what = (f"RESCHEDULE booking {ap['booking_id']} with {ap['provider_name']} from {fmt_slot(old)} "
                    f"to {fmt_slot(ap['slot'])} (price {rupees(ap['price'])})")
        return f"Please confirm: {what}. Reply 'yes' to confirm or 'no' to stop."

    def _grant_and_execute(self) -> str:
        S = self.state
        ap = S.get("approval")
        if not ap or ap["status"] != "pending":
            return "There's nothing waiting for your confirmation right now."
        ap["status"] = "granted"
        self.tracer.event("approval", event="granted", approval_id=ap["id"], fingerprint=ap["fingerprint"])
        S["execution"] = {"approval_id": ap["id"], "action": ap["action"], "key": ap["idempotency_key"],
                          "status": "in_flight", "attempts": 0}
        self._transition("executing", f"{ap['action']} approved")  # persisted before any provider call
        return {"book": self._exec_book, "cancel": self._exec_cancel,
                "reschedule": self._exec_reschedule}[ap["action"]]()

    def _exec_book(self) -> str:
        S = self.state
        ap, ex = S["approval"], S["execution"]
        while ex["attempts"] < self.limits.max_attempts:
            if not self._hold_valid(S["hold"]):
                try:
                    hold = self.tools.hold_slot(ap["provider_id"], ap["slot"])
                except ToolError as e:
                    if e.code != "slot_unavailable":
                        raise
                    return self._abort_book("The slot is no longer available, so nothing was booked.",
                                            exclude=True)
                S["hold"] = hold
                if hold["quoted_price"] != ap["price"]:
                    S["execution"] = None
                    self._void(f"price changed {ap['price']} -> {hold['quoted_price']}")
                    return (f"Your hold expired and the provider now quotes {rupees(hold['quoted_price'])} instead of "
                            f"{rupees(ap['price'])}, so I did NOT book. " +
                            self._propose("book", ap["provider_id"], ap["provider_name"], ap["slot"],
                                          hold["quoted_price"]))
            ex["attempts"] += 1
            self._save()
            try:
                self.tools.book(S["hold"]["hold_id"], ex["key"], ap)
            except ToolTimeout:
                pass  # outcome unknown: the status check below decides
            except ToolError as e:
                if e.code == "hold_expired":
                    S["hold"]["expires_at"] = self.clock.now().isoformat()
                    continue
                if e.code == "slot_taken":
                    return self._abort_book("Someone else just took that slot, so nothing was booked.",
                                            exclude=True)
                if not e.transient:
                    raise
            st = self.tools.get_booking_status(ex["key"])  # authoritative; never claim success without it
            if st["status"] == "booked":
                return self._finish_book(st["booking_id"])
            if st["status"] == "failed":
                return self._abort_book("The provider rejected the booking, so nothing was booked.", exclude=True)
        st = self.tools.get_booking_status(ex["key"])
        if st["status"] == "booked":
            return self._finish_book(st["booking_id"])
        return self._abort_book(f"I tried {ex['attempts']} times but the provider never confirmed. I checked its "
                                f"records: nothing was booked.", exclude=False)

    def _abort_book(self, msg: str, exclude: bool) -> str:
        S, r = self.state, self.state["request"]
        ap = S["approval"]
        S["execution"]["status"] = "failed"
        self._void(msg)
        if exclude:
            r.setdefault("excluded", []).append(f"{ap['provider_id']}|{ap['slot']}")
        S["hold"] = None
        self._transition("collecting", "booking not completed")
        return msg + " " + self._search_and_present()

    def _finish_book(self, booking_id: str) -> str:
        S, r = self.state, self.state["request"]
        ap = S["approval"]
        ap["status"] = "consumed"
        S["execution"].update(status="verified", booking_id=booking_id)
        self.tracer.event("approval", event="consumed", approval_id=ap["id"], booking_id=booking_id)
        summary = f"booking {booking_id}: {ap['provider_name']} on {fmt_slot(ap['slot'])} for {rupees(ap['price'])}"
        S["last_result"] = {"action": "book", "booking_id": booking_id, "summary": summary}
        self._effects.append(summary)
        self._transition("done", "booking verified")
        msg = f"✅ Booked and verified with the provider — {summary}."
        if self.memory.infer(f"preferred_provider:{r['service']}",
                             {"id": ap["provider_id"], "name": ap["provider_name"]}, evidence=booking_id):
            msg += (f" (I've noted {ap['provider_name']} as a possible favourite; I'll ask before using that.)")
        return msg

    # ================= cancel / reschedule =================
    def _find_target(self, verb: str):
        S, r = self.state, self.state["request"]
        ups = [b for b in self.tools.list_bookings() if b["status"] == "upcoming"
               and (not r.get("service") or b["service"] == r["service"])]
        if not ups:
            self._transition("idle", "no booking found")
            what = SERVICE_NAMES.get(r.get("service"), "")
            return None, f"You don't have any upcoming {what + ' ' if what else ''}bookings to {verb}."
        if len(ups) > 1:
            S["options"] = [{"kind": "booking", "booking": b} for b in ups]
            self._transition("choosing", "multiple bookings")
            return None, f"Which booking should I {verb}?\n" + self._options_text()
        return ups[0], None

    def _advance_cancel(self) -> str:
        S = self.state
        if not S.get("target"):
            t, msg = self._find_target("cancel")
            if not t:
                return msg
            S["target"] = t
        t = S["target"]
        return self._propose("cancel", t["provider_id"], t["provider_name"], t["slot"], t["price"], booking_id=t["id"])

    def _advance_reschedule(self) -> str:
        S, r = self.state, self.state["request"]
        if not S.get("target"):
            t, msg = self._find_target("reschedule")
            if not t:
                return msg
            S["target"] = t
        t = S["target"]
        if not r.get("date_from"):
            self._transition("collecting", "need new date")
            return f"When would you like to move your {SERVICE_NAMES[t['service']]} ({fmt_slot(t['slot'])}) to?"
        areas = [a for a in [t.get("area"), r.get("area"), self.memory.usable("area")] if a] + AREAS
        free = None
        for area in dict.fromkeys(areas):
            for p in self.tools.search_providers(t["service"], area, [r["date_from"], r["date_to"]]):
                if p["id"] == t["provider_id"]:
                    free = p["free_slots"]
                    break
            if free is not None:
                break
        start, end = self._window()
        excluded = set(r.get("excluded", []))
        slots = [s for s in (free or []) if start <= datetime.fromisoformat(s).hour * 60 < end
                 and s != t["slot"] and f"{t['provider_id']}|{s}" not in excluded]
        if not slots:
            self._transition("collecting", "no reschedule slots")
            return (f"{t['provider_name']} has no free slots between {r['date_from']} and {r['date_to']}"
                    f" in that time window. Try another day or time.")
        if r.get("time_exact") and len(slots) == 1:
            return self._propose("reschedule", t["provider_id"], t["provider_name"], slots[0], t["price"],
                                 booking_id=t["id"])
        S["options"] = [{"kind": "slot", "slot": s} for s in slots[:4]]
        self._transition("choosing", "reschedule slots found")
        return f"{t['provider_name']} is free at these times:\n" + self._options_text(header=False)

    def _exec_cancel(self) -> str:
        S = self.state
        ap, ex = S["approval"], S["execution"]
        while ex["attempts"] < self.limits.max_attempts:
            ex["attempts"] += 1
            self._save()
            try:
                self.tools.cancel(ap["booking_id"], ex["key"], ap)
                break
            except ToolError as e:
                if not e.transient:
                    if self._booking_now(ap["booking_id"]).get("status") == "cancelled":
                        break
                    return self._abort_other(f"The provider refused the cancellation ({e.code}). Your booking is unchanged.")
                self.clock.sleep(0.5 * 2 ** (ex["attempts"] - 1))  # backoff, then retry with the SAME key
        b = self._booking_now(ap["booking_id"])
        if b.get("status") == "cancelled":
            return self._finish_other(f"booking {ap['booking_id']} with {ap['provider_name']} on {fmt_slot(ap['slot'])} cancelled")
        return self._abort_other(f"I couldn't cancel after {ex['attempts']} attempts. I checked: booking "
                                 f"{ap['booking_id']} is still active.")

    def _exec_reschedule(self) -> str:
        S, r = self.state, self.state["request"]
        ap, ex = S["approval"], S["execution"]
        while ex["attempts"] < self.limits.max_attempts:
            ex["attempts"] += 1
            self._save()
            try:
                self.tools.reschedule(ap["booking_id"], ap["slot"], ex["key"], ap)
                break
            except ToolError as e:
                if e.code == "slot_conflict":
                    ex["status"] = "failed"
                    self._void("slot_conflict")
                    r.setdefault("excluded", []).append(f"{ap['provider_id']}|{ap['slot']}")
                    self._transition("collecting", "slot conflict")
                    return (f"The provider reported a conflict for {fmt_slot(ap['slot'])}, so your booking is "
                            f"unchanged at {fmt_slot(S['target']['slot'])}. " + self._advance_reschedule())
                if not e.transient:
                    return self._abort_other(f"The provider refused the change ({e.code}). Your booking is unchanged.")
                self.clock.sleep(0.5 * 2 ** (ex["attempts"] - 1))
        b = self._booking_now(ap["booking_id"])
        if b.get("slot") == ap["slot"] and b.get("status") == "upcoming":
            return self._finish_other(f"booking {ap['booking_id']} with {ap['provider_name']} moved to {fmt_slot(ap['slot'])}")
        return self._abort_other(f"I couldn't confirm the change after {ex['attempts']} attempts. I checked: your "
                                 f"booking is still at {fmt_slot(b.get('slot', S['target']['slot']))}.")

    def _booking_now(self, booking_id: str) -> dict:
        return next((b for b in self.tools.list_bookings() if b["id"] == booking_id), {})

    def _finish_other(self, summary: str) -> str:
        S = self.state
        S["approval"]["status"] = "consumed"
        S["execution"]["status"] = "verified"
        self.tracer.event("approval", event="consumed", approval_id=S["approval"]["id"])
        S["last_result"] = {"action": S["approval"]["action"], "summary": summary}
        self._effects.append(summary)
        self._transition("done", "change verified")
        return f"✅ Done and verified with the provider — {summary}."

    def _abort_other(self, msg: str) -> str:
        S = self.state
        S["execution"]["status"] = "failed"
        self._void(msg)
        self._transition("idle", "change not completed")
        return msg

    # ================= recovery =================
    def _reconcile(self) -> str:
        S = self.state
        ex, ap = S["execution"], S["approval"]
        self.tracer.event("recovery", action=ex["action"], key=ex["key"], attempts=ex["attempts"])
        if ex["action"] == "book":
            st = self.tools.get_booking_status(ex["key"])
            if st["status"] == "booked":
                return "Picking up where we left off: " + self._finish_book(st["booking_id"])
            return "Picking up where we left off (the booking hadn't gone through yet): " + self._exec_book()
        b = self._booking_now(ap["booking_id"])
        if ex["action"] == "cancel":
            if b.get("status") == "cancelled":
                return "Picking up where we left off: " + self._finish_other(f"booking {ap['booking_id']} cancelled")
            return "Picking up where we left off: " + self._exec_cancel()
        if b.get("slot") == ap["slot"]:
            return "Picking up where we left off: " + self._finish_other(
                f"booking {ap['booking_id']} moved to {fmt_slot(ap['slot'])}")
        return "Picking up where we left off: " + self._exec_reschedule()
