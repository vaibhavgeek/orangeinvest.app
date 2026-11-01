"""The errand agent: an explicit state machine. The model only parses text; this code decides."""
import json
import os
import re
import uuid
from datetime import datetime, timedelta

from . import guard
from .approval import ApprovalError, is_affirmative, is_negative, new_approval
from .budget import Budget, LimitExceeded, Limits
from .clock import SystemClock
from .memory import MemoryStore, label, show_value
from .mock_api import AREAS, CITY, ToolError, ToolTimeout
from .nlu import SERVICE_NAMES, ModelUnavailable, OpenAIModel, context_line
from .state import StateStore, new_state
from .tools import ToolGateway


V = "\x1e"  # wraps parts of a reply shown verbatim (tables, lists, the approval line, suggestion chips)


def verbatim(s: str) -> str:
    return f"{V}{s}{V}"


LEAD_MINUTES = 60          # earliest bookable slot is this far from now
MAX_MESSAGE_CHARS = 1000   # longer messages are refused before any (paid) model call
MAX_STRIKES = 3            # off-purpose / injection attempts before the session is closed

SERVICES_LINE = "plumber, electrician, AC servicing, home cleaning, carpenter and pest control"
CLOSED_MESSAGE = ("This chat is closed because it was used for things other than booking errands. Start a New chat "
                  "to book, reschedule or cancel a home service.")
_LINK = re.compile(r"https?://[^\s)\]>]+|\bwww\.[^\s)\]>]+", re.I)
_AMOUNT = re.compile(r"₹\s?(\d[\d,]*)")
_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}|openai_api_key|you extract structured fields|how a turn works", re.I)


def fmt_slot(slot: str) -> str:
    return datetime.fromisoformat(slot).strftime("%a %d %b, %H:%M")


def fmt_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def fmt_dates(date_from: str, date_to: str) -> str:
    a, b = (datetime.fromisoformat(d).strftime("%A %d %b (%Y-%m-%d)") for d in (date_from, date_to))
    return a if date_from == date_to else f"{a} to {b}"


def rupees(n) -> str:
    return f"₹{int(n):,}"


class Agent:
    def __init__(self, api, user_id: str = "u1", session_id: str = "s1", data_dir: str = "data",
                 clock=None, model=None, limits: Limits | None = None, trace_dir: str | None = None,
                 tracer=None, phrase_replies: bool = True):
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
        self.model = model or OpenAIModel.from_env()
        self.phrase_replies = phrase_replies  # False only in tests that assert on the code's facts
        self.tools = ToolGateway(api, user_id, self.budget, self.tracer, self.clock)
        if self.state.get("hold"):
            self.tools.register_hold(self.state["hold"])
        self._effects: list = []
        self._turn_note = ""
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
        if self.state.get("closed"):
            # no parser, no reply model: a closed session costs nothing
            self.tracer.event("input_rejected", reason="session_closed")
            self.tracer.event("agent_reply", text=CLOSED_MESSAGE, stage=self.state["stage"])
            self._save()
            return CLOSED_MESSAGE
        if self.state["turns"] > self.limits.max_turns:
            self.tracer.event("limit_exceeded", kind="turns", limit=self.limits.max_turns)
            reply = self._limit_message(LimitExceeded("turns", self.limits.max_turns)).replace(V, "")
            self.tracer.event("agent_reply", text=reply)
            self._save()
            return reply
        return self._guarded(lambda: self._step(text), text)

    def _guarded(self, fn, user_text: str = "") -> str:
        self.budget.start_turn()
        self._effects = []
        self._turn_note = ""
        try:
            reply = fn()
            if self._turn_note:
                reply = self._turn_note + " " + reply
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
        draft = reply
        reply = self._phrase(user_text, reply)
        self.tracer.event("agent_reply", text=reply, stage=self.state["stage"], facts=draft.replace(V, ""))
        self.tracer.event("turn_end", tool_calls=self.budget.tool_calls, model_calls=self.budget.model_calls,
                          elapsed_s=round(self.clock.monotonic() - self.budget.t0, 3))
        return reply

    def _phrase(self, user_text: str, draft: str) -> str:
        """Code decided the facts; the model writes the words. Verbatim parts are appended unchanged, so tables,
        prices, slots and provider reviews never pass through the model. If the model can't be used (down, or
        this turn's model-call limit is spent), the facts are sent as they are rather than leaving the user
        without an answer."""
        parts = draft.split(V)
        prose = " ".join(p.strip() for p in parts[0::2] if p.strip())
        blocks = [p.strip("\n") for p in parts[1::2] if p.strip()]
        plain = "\n".join(p.strip("\n") for p in parts if p.strip())
        if not self.phrase_replies or not prose or not hasattr(self.model, "compose"):
            self._remember_turn(user_text, prose)
            return plain
        facts = prose + (" (Shown below this text, verbatim: " + str(len(blocks)) + " block(s) with the options, "
                         "the exact action to confirm, or one-click choices.)" if blocks else "")
        try:
            self.budget.charge_model()
            text, usage = self.model.compose(user_text, facts, history=self.state.get("history") or [],
                                             state=self._reply_state())
        except (ModelUnavailable, LimitExceeded) as e:
            self.tracer.event("error", error=f"reply_not_phrased: {e}")
            self._remember_turn(user_text, prose)
            return plain
        self.tracer.event("model_call", purpose="reply", **usage)
        bad = self._reply_violation(text, plain)
        if bad:
            # the model added something code didn't decide (or leaked): send the facts as they are
            self.tracer.event("reply_rejected", reason=bad, text=text[:300])
            self._remember_turn(user_text, prose)
            return plain
        self._remember_turn(user_text, text)
        return "\n".join([text] + blocks)

    def _reply_violation(self, text: str, facts: str) -> str | None:
        """Output guard for the reply model. Anything it may repeat must come from this turn's facts, the
        request state or earlier replies; links other than Vaibhav's site and prompt/key leaks are refused."""
        known = " ".join([facts, json.dumps(self._reply_state(), ensure_ascii=False)]
                         + [h["agent"] for h in self.state.get("history") or []])
        if "✅" in text and "✅" not in facts:
            return "unearned_checkmark"
        if any(b not in known for b in re.findall(r"BK-\d+", text)):
            return "unknown_booking_id"
        amounts = {int(a.replace(",", "")) for a in _AMOUNT.findall(known)}
        r = self.state["request"]
        amounts |= {int(r[k]) for k in ("budget",) if r.get(k)}
        if any(int(a.replace(",", "")) not in amounts for a in _AMOUNT.findall(text)):
            return "unknown_amount"
        if any("vaibhavgeek.tech" not in u.lower() for u in _LINK.findall(text)):
            return "foreign_link"
        if _SECRET.search(text):
            return "leak"
        return None

    HISTORY_TURNS = 6

    def _remember_turn(self, user_text: str, agent_prose: str):
        """Short history for the reply model. Only the agent's prose is kept: verbatim blocks (tables, provider
        reviews) never reach the model, here or anywhere else."""
        if not user_text or not agent_prose:
            return
        h = self.state.setdefault("history", [])
        h.append({"user": user_text[:500], "agent": agent_prose[:800]})
        del h[:-self.HISTORY_TURNS]
        self._save()

    def _reply_state(self) -> dict:
        S, r = self.state, self.state["request"]
        known = {k: r[k] for k in ("service", "area", "date_from", "date_to", "budget", "no_budget") if r.get(k)}
        for k in ("time_start", "time_end"):
            if r.get(k) is not None:
                known[k] = fmt_min(r[k])
        if r.get("provider"):
            known["provider"] = r["provider"]["name"]
        return {"stage": S["stage"], "intent": S["intent"], "city": CITY, "request": known,
                "today": self.clock.now().strftime("%A %Y-%m-%d")}

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

    def parse_ctx(self) -> dict:
        ctx = {"today": self.clock.now().date(), "stage": self.state["stage"], "n_options": len(self.state["options"])}
        r = self.state["request"]
        anchor = (r["date_from"], r["date_to"]) if r.get("date_from") else self.state.get("prev_dates")
        if anchor:
            ctx["anchor"] = tuple(anchor)
            ctx["anchor_label"] = fmt_dates(*anchor)
        return ctx

    def _parse(self, text: str):
        self.budget.charge_model()
        ctx = self.parse_ctx()
        p, usage, dropped = self.model.parse(text, ctx)
        out = {k: v for k, v in vars(p).items() if v not in (None, False)}
        self.tracer.event("model_call", **usage, purpose="parse", prompt_context=context_line(ctx), output=out,
                          dropped_fields=dropped)
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

        if len(text) > MAX_MESSAGE_CHARS:
            self.tracer.event("input_rejected", reason="too_long", chars=len(text))
            return (f"That message is too long ({len(text)} characters; the limit is {MAX_MESSAGE_CHARS}). "
                    "Please send a shorter request. Nothing was changed.")

        attack = guard.screen(text)
        if attack:
            # caught in code before any model call, so the attempt costs no tokens
            self.tracer.event("input_rejected", reason=attack)
            return self._strike(attack)

        sug = self._suggestion(text)
        if sug is not None:
            self.tracer.event("suggestion_click", label=sug["label"], updates=sug["updates"])
            S["suggestions"] = []
            self._merge(sug["updates"])
            S["options"] = []
            return self._advance()

        try:
            p = self._parse(text)
        except ModelUnavailable as e:
            self.tracer.event("error", error=f"model_unavailable: {e}")
            return ("Sorry, I couldn't understand that just now because my language model isn't responding. "
                    "Nothing was changed. Please try again in a moment.")

        if p.off_topic:
            self.tracer.event("input_rejected", reason="off_topic", by="parser")
            if not (p.intent or p.slot_updates() or p.selection is not None):
                return self._strike("off_topic")
            # a real errand with an unrelated request attached: count it, drop that part, carry on
            note = self._strike("off_topic", inline=True)
            if self.state.get("closed"):
                return note
            self._turn_note = note

        if p.errands and len(p.errands) > 1:
            self.tracer.event("input_rejected", reason="several_errands", errands=p.errands)
            return (f"That's more than one errand ({' and '.join(p.errands)}). I handle one at a time so each gets "
                    "its own confirmation. Which should I do first? Nothing was changed.")

        if p.area is None and p.unknown_area:
            self.tracer.event("input_rejected", reason="unsupported_area", area=p.unknown_area)
            return (f"I can only book in {CITY} for now, in these areas: {', '.join(AREAS)}. I haven't saved "
                    "anything. Tell me one of those if it's right for you.")

        if p.service is None and p.unknown_service:
            self.tracer.event("input_rejected", reason="unsupported_service", service=p.unknown_service)
            return f"The user asked for {p.unknown_service}. That is not a service you offer. Nothing was changed."

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

        # a different service in the middle of a booking: start over for it, keeping only the area, without asking
        switch = (S["intent"] == "book" and stage not in ("idle", "done") and p.service
                  and S["request"].get("service") and p.service != S["request"]["service"]
                  and p.intent in (None, "book"))
        if switch:
            p.intent = "book"
            return self._start_flow(p, switched_from=S["request"]["service"])
        if p.intent in ("book", "reschedule", "cancel") and (
                p.intent != S["intent"] or stage in ("idle", "done")):
            return self._start_flow(p)

        if S["intent"] is None or stage in ("idle", "done"):
            if updates.get("service") or updates.get("same_as_last"):
                p.intent = "book"
                return self._start_flow(p)
            if is_affirmative(text) or is_negative(text):
                return "There's nothing waiting for your confirmation right now. What can I help you with?"
            return ("Nothing to act on: no request is in progress and this message isn't a booking, reschedule, "
                    "cancel or memory request. Respond to what the user actually said, then say what you can help "
                    "with. Nothing was changed.")

        if S["stage"] in ("choosing", "collecting") and is_negative(text) and not updates and p.selection is None:
            self._void("user stopped")
            S.update({"intent": None, "request": {}, "options": [], "hold": None, "target": None, "approval": None})
            self._transition("idle", "user stopped the request")
            return "OK, I've dropped that request. Nothing was booked or changed."

        if updates:
            before = self._search_key()
            self._merge(updates)
            if (S["stage"] in ("choosing", "collecting") and S["intent"] == "book"
                    and S.get("last_search") == before == self._search_key()):
                self.state["request"].pop("_note", None)
                return self._not_understood()
            S["suggestions"] = []
            if S["stage"] == "awaiting_approval":
                self._transition("collecting", "request corrected")
            if S["intent"] == "book":
                S["options"] = []
            elif S["intent"] == "reschedule" and S.get("target"):
                S["options"] = []
            return self._advance()

        if p.selection is not None and S["options"]:
            return self._select(p.selection)

        if S["stage"] == "choosing" or (S["intent"] == "book" and S.get("last_search")
                                        and S["last_search"] == self._search_key()):
            return self._not_understood()
        return self._advance()

    def _strike(self, category: str, inline: bool = False) -> str:
        """Count an attempt to use the agent for something other than errands. Refusals are fixed text, shown
        verbatim, so they cost no reply-model call; the MAX_STRIKES-th attempt closes the session."""
        S = self.state
        S["strikes"] = n = S.get("strikes", 0) + 1
        self.tracer.event("strike", category=category, count=n, limit=MAX_STRIKES)
        if n >= MAX_STRIKES:
            self._close(category)
            return verbatim(f"I've closed this chat because it was used for things other than booking errands "
                            f"{MAX_STRIKES} times. Nothing was booked, cancelled or changed, and your existing "
                            "bookings are as they were. Start a New chat to book a service.")
        warn = (" Please keep to errands: one more request like this and I'll close this chat."
                if n == MAX_STRIKES - 1 else "")
        if inline:
            return "I've ignored the part of your message that isn't about errands." + warn
        if category == "off_topic":
            msg = (f"I can only help with booking home services in {CITY} ({SERVICES_LINE}) and with rescheduling "
                   "or cancelling your bookings, so I can't help with that.")
        else:
            msg = (f"I can't do that. I only book home services in {CITY} ({SERVICES_LINE}) and manage your "
                   "bookings, and I don't change how I work or share how I'm built. Nothing was changed.")
        return verbatim(msg + (warn or " What service do you need?"))

    def _close(self, reason: str):
        S = self.state
        self._void("session closed")
        S.update({"intent": None, "request": {}, "options": [], "hold": None, "target": None,
                  "pending_memory": None, "suggestions": [], "closed": True, "prev_dates": None})
        self._transition("idle", f"session closed ({reason})")

    def _not_understood(self) -> str:
        """Never silently repeat the same search: say so, and show what can be done."""
        S = self.state
        msg = "Sorry, I didn't understand that, so nothing has changed."
        if S["options"]:
            return msg + " Pick an option by number, or tell me what to change.\n" + self._options_text()
        if S.get("suggestions"):
            return msg + " Pick one of these, or tell me what to change.\n" + \
                verbatim("Or say: " + " · ".join(x["label"] for x in S["suggestions"]))
        return msg + " Tell me what to change, e.g. a different day, time or budget, or say 'search everything'."

    def _suggestion(self, text: str):
        S = self.state
        if S["intent"] != "book" or S["stage"] not in ("choosing", "collecting"):
            return None
        t = text.strip().lower().rstrip(".!")
        return next((x for x in S.get("suggestions") or [] if x["label"].lower() == t), None)

    def _search_key(self) -> str:
        skip = {"budget_asked", "pref_asked", "area_src", "provider_src"}
        return json.dumps({k: v for k, v in self.state["request"].items() if not k.startswith("_") and k not in skip},
                          sort_keys=True, default=str)

    def _start_flow(self, p, switched_from: str | None = None) -> str:
        S = self.state
        old = S["request"]
        keep = {k: old[k] for k in ("area", "area_src", "date_from", "date_to") if switched_from and old.get(k)}
        if old.get("date_from"):  # what "same day" refers to in the next request
            S["prev_dates"] = [old["date_from"], old["date_to"]]
        self._void("new request")
        S.update({"intent": p.intent, "request": keep, "options": [], "hold": None, "target": None,
                  "approval": None, "execution": None, "pending_memory": None, "suggestions": [], "last_search": None})
        self._merge(p.slot_updates())
        if switched_from:
            r = S["request"]
            # no "should I look for your last provider?" question either: the switch goes straight on
            r["pref_asked"] = True
            kept = []
            if keep.get("area") and r.get("area") == keep["area"]:
                kept.append(f"your area, {r['area']}")
            if keep.get("date_from") and (r.get("date_from"), r.get("date_to")) == (keep["date_from"], keep["date_to"]):
                kept.append(f"the same day, {fmt_dates(r['date_from'], r['date_to']).split(' (')[0]} "
                            "(say a different day to change it)")
            note = (f"Switched to {SERVICE_NAMES[p.service]}: I've dropped the {SERVICE_NAMES[switched_from]} "
                    "request (nothing was booked)" + (" and kept " + " and ".join(kept) if kept else "") + ".")
            r["_note"] = " ".join(x for x in (note, r.get("_note")) if x)
            self.tracer.event("service_switch", **{"from": switched_from}, to=p.service, kept=sorted(keep))
        self._transition("collecting", f"new {p.intent} request")
        return self._advance()

    def _merge(self, updates: dict):
        r = self.state["request"]
        updates = dict(updates)
        relax = updates.pop("relax", None)
        if relax:
            self._relax(set(relax))
        for k, v in updates.items():
            r[k] = v
        if "time_start" in updates or "time_end" in updates:
            r.pop("any_time", None)
        if updates.get("same_as_last"):
            r.pop("any_provider", None)
        if "budget" in updates:
            r.pop("no_budget", None)
        if updates.get("no_budget"):
            r.pop("budget", None)
        if "area" in updates:
            r["area_src"] = "user"
            if self.memory.usable("area") != updates["area"]:
                self.memory.state("area", updates["area"])
                r["_note"] = f"I'll remember {updates['area']} as your area (say 'forget my area' anytime)."

    def _relax(self, relax: set):
        """Drop the filters the user asked to widen. Explicit values in the same message are applied after this."""
        r = self.state["request"]
        if "all" in relax:
            relax |= {"budget", "dates", "time", "provider"}
            r["browse"] = True
        if "budget" in relax:
            r.pop("budget", None)
            r["no_budget"] = True
        if "time" in relax:
            for k in ("time_start", "time_end", "time_exact"):
                r.pop(k, None)
            r["any_time"] = True
        if "dates" in relax:
            today = self.clock.now().date()
            r["date_from"] = today.isoformat()
            r["date_to"] = max(r.get("date_to") or "", (today + timedelta(days=6)).isoformat())
        if "provider" in relax:
            r.pop("provider", None)
            r.pop("provider_src", None)
            r.pop("same_as_last", None)
            r["any_provider"] = True
        self.tracer.event("relax", filters=sorted(relax))

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
        if r.get("time_exact") or r.get("any_time"):
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
        if svc and not r.get("provider") and not r.get("pref_asked") and not r.get("any_provider"):
            key = f"preferred_provider:{svc}"
            f = self.memory.get(key)
            if f and f["source"] == "inferred" and not f["confirmed"]:
                r["pref_asked"] = True
                S["pending_memory"] = {"key": key, "value": f["value"]}
                self._save()
                return " ".join(notes + [f"Last time you booked {f['value']['name']} for {SERVICE_NAMES[svc]}. "
                                         f"Should I look for them again? (yes/no)"])
            val = None if r.get("any_provider") else self.memory.usable(key)
            if val:
                r["provider"], r["provider_src"] = val, "memory"
                notes.append(f"Using your preferred provider {val['name']}.")
        missing = []
        if not svc:
            missing.append("what service you need (plumber, electrician, AC servicing, cleaning, carpenter, pest control)")
        if not r.get("area"):
            missing.append(f"your area in {CITY} ({', '.join(AREAS)})")
        if not r.get("date_from"):
            missing.append("when (e.g. 'tomorrow', 'this week', 'Saturday')")
        if r.get("budget") is None and not r.get("no_budget") and not r.get("budget_asked") and not r.get("provider"):
            missing.append("your budget (or say 'no budget')")
            r["budget_asked"] = True
        if missing:
            if S["stage"] != "collecting":
                self._transition("collecting", "missing details")
            return " ".join(notes + ["To find options I still need: " + "; ".join(missing) + "."])
        past = self._past_dates()
        if past:
            return " ".join(notes + [past])
        return " ".join(notes + [self._search_and_present()]).strip()

    def _bookable(self, slot: str) -> bool:
        """Providers need lead time; the mock (like many real APIs) will happily list slots in the past."""
        return datetime.fromisoformat(slot) >= self.clock.now() + timedelta(minutes=LEAD_MINUTES)

    def _past_dates(self) -> str | None:
        """Clamp a range that started in the past; reject one that ended in the past."""
        r = self.state["request"]
        today = self.clock.now().date().isoformat()
        if r.get("date_to") and r["date_to"] < today:
            when = r["date_from"] if r["date_from"] == r["date_to"] else f"{r['date_from']} to {r['date_to']}"
            r.pop("date_from"); r.pop("date_to")
            self._transition("collecting", "date in the past")
            return f"{when} is in the past. When would you like it instead (e.g. 'tomorrow', 'this week')?"
        if r.get("date_from") and r["date_from"] < today:
            r["date_from"] = today
        return None

    def _window(self):
        r = self.state["request"]
        start, end = r.get("time_start") or 0, r.get("time_end") or 24 * 60
        e = self._earliest()
        if e is not None:
            start = max(start, e)
        return start, end

    def _match(self, results, mine, drop: set):
        """Providers with free slots that pass every filter except those in `drop` (time, budget, provider)."""
        r = self.state["request"]
        start, end = (0, 24 * 60) if "time" in drop else self._window()
        excluded = set(r.get("excluded", []))
        cands = []
        for p in results:
            if r.get("provider") and "provider" not in drop and p["id"] != r["provider"]["id"]:
                continue
            if r.get("budget") and "budget" not in drop and p["price"] > r["budget"]:
                continue
            slots = [s for s in p["free_slots"]
                     if start <= datetime.fromisoformat(s).hour * 60 < end and s not in mine and self._bookable(s)
                     and f"{p['id']}|{s}" not in excluded]
            if slots:
                cands.append((p, slots))
        if "budget" in drop:
            cands.sort(key=lambda c: (c[0]["price"], -c[0]["rating"]))
        else:
            cands.sort(key=lambda c: (-c[0]["rating"], c[0]["price"]))
        return cands

    def _constraints(self) -> str:
        r = self.state["request"]
        start, end = self._window()
        when = (f"on {datetime.fromisoformat(r['date_from']).strftime('%a %d %b')}" if r["date_from"] == r["date_to"]
                else f"between {r['date_from']} and {r['date_to']}")
        cons = [f"in {r['area']}", when, f"from {fmt_min(start)} to {fmt_min(end)}" if (start, end) != (0, 1440) else ""]
        if r.get("budget"):
            cons.append(f"under {rupees(r['budget'])}")
        return " ".join(c for c in cons if c)

    def _search_and_present(self) -> str:
        S, r = self.state, self.state["request"]
        S["suggestions"] = []
        results = self.tools.search_providers(r["service"], r["area"], [r["date_from"], r["date_to"]])
        mine = {b["slot"] for b in self.tools.list_bookings() if b["status"] == "upcoming"}
        S["last_search"] = self._search_key()
        cands, drop = self._match(results, mine, set()), set()
        today = self.clock.now().date()
        week = (today.isoformat(), (today + timedelta(days=6)).isoformat())
        narrow = r["date_from"] > week[0] or r["date_to"] < week[1]
        if not cands:
            # Work out which filter is blocking, from results we already have, before searching wider.
            start, end = self._window()
            active = {k for k, on in (("time", (start, end) != (0, 1440)), ("budget", bool(r.get("budget"))),
                                      ("provider", bool(r.get("provider")))) if on}
            tries = [d for d in ({"time"}, {"budget"}, {"time", "budget"}, {"provider"}, {"provider", "time", "budget"})
                     if d <= active]
            tries = [d for i, d in enumerate(tries) if d not in tries[:i]]
            for d in tries:
                cands = self._match(results, mine, d)
                if cands:
                    drop = d
                    break
            if not cands and narrow:
                wide = self.tools.search_providers(r["service"], r["area"], list(week))
                for d in [set()] + tries:
                    cands = self._match(wide, mine, d)
                    if cands:
                        drop = d | {"dates"}
                        break
        if not cands:
            S["options"] = []
            self._transition("collecting", "no matching options")
            who = r["provider"]["name"] if r.get("provider") else f"{SERVICE_NAMES[r['service']]}s"
            return (f"I couldn't find any free {who} {self._constraints()}" +
                    (", and nothing at all is free there in the next 7 days." if narrow or r["date_to"] >= week[1]
                     else ".") + " Try another area or a later week.")
        per = 3 if r.get("browse") else 2
        top = cands if r.get("browse") else cands[:3]
        options = [{"provider_id": p["id"], "provider_name": p["name"], "slot": s, "price": p["price"],
                    "rating": p["rating"], "attributes": p["attributes"]}
                   for p, slots in top for s in slots[:per]]
        for pid in dict.fromkeys(o["provider_id"] for o in options):
            d = self.tools.get_provider(pid)
            safe = [rv["text"] for rv in d["reviews"] if not rv["flagged"]]
            flagged = sum(rv["flagged"] for rv in d["reviews"])
            for o in options:
                if o["provider_id"] == pid:
                    o["review"] = safe[0] if safe else None
                    o["flagged_reviews"] = flagged
        S["options"] = [dict(o, kind="new") for o in options]
        note = self._blocked_note(drop, cands) if drop else ""
        S["suggestions"] = self._suggestions_for(drop, cands, narrow) if drop else []
        self._transition("choosing", f"{len(options)} options found" + (f", relaxed {sorted(drop)}" if drop else ""))
        if drop:
            self.tracer.event("fallback", relaxed=sorted(drop), suggestions=[x["label"] for x in S["suggestions"]])
        return (note + "\n" if note else "") + self._options_text(header=not note)

    def _blocked_note(self, drop: set, cands) -> str:
        r = self.state["request"]
        who = r["provider"]["name"] if r.get("provider") else f"{SERVICE_NAMES[r['service']]}s"
        parts = [f"I couldn't find any free {who} {self._constraints()}."]
        if "budget" in drop:
            p = min((c[0] for c in cands), key=lambda p: p["price"])
            parts.append(f"The cheapest available is {p['name']} at {rupees(p['price'])}.")
        if "time" in drop:
            parts.append("There are free slots at other times of day.")
        if "provider" in drop:
            parts.append(f"{r['provider']['name']} has nothing free, but other providers do.")
        if "dates" in drop:
            parts.append("Those days are full, but there are free slots later this week.")
        parts.append("Here's what you can book instead:")
        return " ".join(parts)

    def _suggestions_for(self, drop: set, cands, narrow: bool) -> list:
        """One-click changes. Clicking one is handled in code, with no model call."""
        r = self.state["request"]
        out = []
        if "budget" in drop:
            cheapest = min(c[0]["price"] for c in cands)
            out.append({"label": f"Raise budget to {rupees(cheapest)}", "updates": {"budget": cheapest}})
            out.append({"label": "No budget limit", "updates": {"relax": ["budget"]}})
        if "time" in drop:
            out.append({"label": "Any time of day", "updates": {"relax": ["time"]}})
        if "provider" in drop:
            out.append({"label": "Any provider", "updates": {"relax": ["provider"]}})
        if narrow:
            out.append({"label": "Look at other days", "updates": {"relax": ["dates"]}})
        if not r.get("browse"):
            out.append({"label": "Show everything", "updates": {"relax": ["all"]}})
        return out

    def option_labels(self) -> list:
        """Human labels for the options currently shown, e.g. for echoing the user's click in the UI."""
        S, out = self.state, []
        svc = SERVICE_NAMES.get(S["request"].get("service") or (S.get("target") or {}).get("service"), "")
        for o in S["options"]:
            if o["kind"] == "booking":
                b = o["booking"]
                out.append(f"{SERVICE_NAMES[b['service']]} with {b['provider_name']}, {fmt_slot(b['slot'])}")
            elif o["kind"] == "slot":
                out.append(f"{svc} with {S['target']['provider_name']}, {fmt_slot(o['slot'])}".strip())
            else:
                out.append(f"{svc} with {o['provider_name']}, {fmt_slot(o['slot'])} ({rupees(o['price'])})".strip())
        return [s[:1].upper() + s[1:] for s in out]

    def _options_text(self, header: bool = True) -> str:
        S = self.state
        opts = S["options"]
        if opts and all(o["kind"] == "new" for o in opts):
            body, tail = self._options_table(opts), "Reply with a slot number to book it."
        else:
            lines = []
            for i, o in enumerate(opts, 1):
                if o["kind"] == "booking":
                    b = o["booking"]
                    lines.append(f"{i}. {SERVICE_NAMES[b['service']]} with {b['provider_name']} on {fmt_slot(b['slot'])} "
                                 f"({rupees(b['price'])}) [{b['id']}]")
                else:
                    lines.append(f"{i}. {fmt_slot(o['slot'])}")
            body, tail = "\n".join(lines), "Reply with an option number."
        out = ("Here are your options:\n" if header else "") + verbatim(body) + "\n" + tail
        if S.get("suggestions"):
            out += "\n" + verbatim("Or say: " + " · ".join(x["label"] for x in S["suggestions"]))
        return out

    @staticmethod
    def _options_table(opts) -> str:
        cell = lambda x: str(x).replace("|", "/").replace("\n", " ")
        days = {o["slot"][:10] for o in opts}
        one_day = len(days) == 1
        groups: dict = {}
        for i, o in enumerate(opts, 1):
            groups.setdefault(o["provider_id"], []).append((i, o))
        rows = ["| Provider | Rating | Price | Free slots | Reviews |", "|---|---|---|---|---|"]
        for items in groups.values():
            o = items[0][1]
            slots = " · ".join(f"[{i}] " + (fmt_min(datetime.fromisoformat(x["slot"]).hour * 60
                                                    + datetime.fromisoformat(x["slot"]).minute)
                                            if one_day else fmt_slot(x["slot"]).replace(",", ""))
                               for i, x in items)
            name = cell(o["provider_name"]) + (f" ({cell(', '.join(o['attributes']))})" if o.get("attributes") else "")
            # brackets out of review text: "[n]" in a cell is a slot button in the web console. Reviews flagged as
            # injection attempts are dropped silently (sanitize.py).
            review = f'"{cell(o["review"]).replace("[", "(").replace("]", ")")}"' if o.get("review") else "—"
            rows.append(f"| {name} | ★{o['rating']} | {rupees(o['price'])} | {slots} | {review} |")
        head = f"All slots are on {datetime.fromisoformat(opts[0]['slot']).strftime('%a %d %b')}.\n" if one_day else ""
        return head + "\n".join(rows)

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
        return verbatim(f"Please confirm: {what}.") + " Reply 'yes' to confirm or 'no' to stop."

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
        if self.memory.booked(f"preferred_provider:{r['service']}",
                              {"id": ap["provider_id"], "name": ap["provider_name"]}, booking_id):
            msg += (f" I've saved {ap['provider_name']} as your preferred {SERVICE_NAMES[r['service']]} provider and "
                    "will look for them first next time. Say 'any provider' then, or 'forget my preferred "
                    f"{SERVICE_NAMES[r['service']]}', to change that.")
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
            # no day given: offer what the provider has free over the next week
            today = self.clock.now().date()
            r["date_from"], r["date_to"] = today.isoformat(), (today + timedelta(days=6)).isoformat()
            r["next_free"] = True
        past = self._past_dates()
        if past:
            return past
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
                 and s != t["slot"] and f"{t['provider_id']}|{s}" not in excluded and self._bookable(s)]
        if not slots:
            self._transition("collecting", "no reschedule slots")
            if r.pop("next_free", None):
                r.pop("date_from", None), r.pop("date_to", None)
                return (f"{t['provider_name']} has no free slots in the next 7 days. Tell me a later day to move your "
                        f"{SERVICE_NAMES[t['service']]} ({fmt_slot(t['slot'])}) to.")
            return (f"{t['provider_name']} has no free slots between {r['date_from']} and {r['date_to']}"
                    f" in that time window. Try another day or time.")
        if r.get("time_exact") and len(slots) == 1:
            return self._propose("reschedule", t["provider_id"], t["provider_name"], slots[0], t["price"],
                                 booking_id=t["id"])
        week = r.pop("next_free", None)
        S["options"] = [{"kind": "slot", "slot": s} for s in slots[:6 if week else 4]]
        self._transition("choosing", "reschedule slots found")
        when = " in the next 7 days" if week else ""
        return (f"Moving your {SERVICE_NAMES[t['service']]} from {fmt_slot(t['slot'])}. {t['provider_name']} is free "
                f"at these times{when}. Pick one, or tell me another day or time.\n" + self._options_text(header=False))

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
