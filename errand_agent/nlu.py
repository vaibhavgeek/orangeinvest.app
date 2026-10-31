"""Language understanding. The model ONLY extracts structured fields from the user's message.
It never sees provider reviews, never decides approval and never calls tools.

RuleBasedModel is the deterministic default (used offline and in evals). OpenAIModel / LLMModel
(Anthropic) are optional drop-ins that return the same schema; their output is validated and anything
outside the schema (e.g. "approved": true) is dropped.
"""
import json
import re
import time
import urllib.request
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta

from .mock_api import AREAS, SERVICES

INTENTS = {"book", "reschedule", "cancel", "memory_view", "memory_forget", "memory_set"}
SLOT_FIELDS = ("service", "area", "date_from", "date_to", "time_start", "time_end", "budget")


@dataclass
class Parsed:
    intent: str | None = None
    service: str | None = None
    area: str | None = None
    date_from: str | None = None     # ISO date
    date_to: str | None = None
    time_start: int | None = None    # minutes after midnight
    time_end: int | None = None
    time_exact: bool = False
    budget: int | None = None
    no_budget: bool = False
    same_as_last: bool = False
    selection: int | None = None     # 1-based option number
    memory_key: str | None = None    # area | earliest_time | preferred_provider[:service] | all
    memory_value: str | int | None = None

    def slot_updates(self) -> dict:
        d = {k: getattr(self, k) for k in SLOT_FIELDS if getattr(self, k) is not None}
        if self.no_budget:
            d["no_budget"] = True
        if self.same_as_last:
            d["same_as_last"] = True
        if self.time_start is not None or self.time_end is not None:
            d["time_exact"] = self.time_exact
        return d

    @classmethod
    def from_dict(cls, d: dict):
        """Strict validation of model output. Returns (Parsed, dropped_keys)."""
        known = {f.name for f in fields(cls)}
        dropped = [k for k in d if k not in known]
        p = cls()
        for k in known & set(d):
            v = d[k]
            if v is None:
                continue
            if k == "intent" and v not in INTENTS:
                dropped.append(k); continue
            if k == "service" and v not in SERVICES:
                dropped.append(k); continue
            if k == "area" and v not in AREAS:
                dropped.append(k); continue
            if k in ("date_from", "date_to"):
                try:
                    date.fromisoformat(v)
                except (TypeError, ValueError):
                    dropped.append(k); continue
            if k in ("time_start", "time_end", "budget", "selection"):
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    dropped.append(k); continue
            if k in ("time_exact", "no_budget", "same_as_last") and not isinstance(v, bool):
                dropped.append(k); continue
            setattr(p, k, v)
        return p, dropped


SERVICE_WORDS = [
    ("ac_service", r"\b(ac|a/c|air ?con\w*)\b"),
    ("plumber", r"plumb|\btap\b|leak|pipe|drain|toilet|sink|faucet"),
    ("electrician", r"electric|wiring|switch|socket|\bfan\b|\blights?\b"),
    ("pest_control", r"pest|cockroach|termite"),
    ("cleaning", r"clean"),
    ("carpenter", r"carpent|furniture|cabinet|wardrobe"),
]
AREA_ALIASES = {a.lower(): a for a in AREAS} | {"hsr": "HSR Layout"}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "1st": 1, "2nd": 2, "3rd": 3}
TIME = r"(\d{1,2})(?::(\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)?"


def _to_minutes(h: str, m: str | None, ampm: str | None, bare_ok: bool) -> int | None:
    hour, minute = int(h), int(m or 0)
    if ampm:
        pm = ampm.lower().startswith("p")
        if hour == 12:
            hour = 12 if pm else 0
        elif pm:
            hour += 12
    elif m is None and not bare_ok:
        return None
    elif hour < 8:  # "after 2" means 14:00 for a home visit
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


class RuleBasedModel:
    name = "rule-based-v1"

    def parse(self, text: str, ctx: dict):
        t0 = time.perf_counter()
        p = self._parse(text, ctx)
        usage = {"model": self.name, "input_tokens": max(1, len(text) // 4),
                 "output_tokens": max(1, len(json.dumps(asdict(p))) // 4), "cost_usd": 0.0,
                 "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}
        return p, usage, []

    def _parse(self, text: str, ctx: dict) -> Parsed:
        t = " " + text.lower().strip() + " "
        today: date = ctx["today"]
        p = Parsed()

        if re.search(r"what do you (remember|know)|show (me )?(my )?(memory|preferences|saved)|"
                     r"my (saved )?preferences\b|what have you saved", t):
            p.intent = "memory_view"
            return p
        m = re.search(r"\bforget\b(.*)", t)
        if m:
            p.intent = "memory_forget"
            p.memory_key = self._memory_key(m.group(1)) or "all"
            if p.memory_key == "preferred_provider":
                svc = self._service(m.group(1))
                p.memory_key = f"preferred_provider:{svc}" if svc else "preferred_provider"
            return p

        p.service = self._service(t)
        for alias, area in sorted(AREA_ALIASES.items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"\b{re.escape(alias)}\b", t):
                p.area = area
                break
        self._dates(t, today, p)
        self._times(t, p)
        self._budget(t, p)
        if re.search(r"same\b.{0,30}\b(as last time|as before|again|last time)|\bsame (one|guy|person|"
                     r"technician|electrician|plumber|provider|people)\b|\busual\b", t):
            p.same_as_last = True

        # memory statements: explicit "remember"/"from now on" or statements about where I live
        remember = re.search(r"\bremember\b|\bfrom now on\b|\balways\b|\bin general\b|\bmy (default|usual) ", t)
        if p.area and re.search(r"my area is|i live in|i (have |'ve )?moved to|change my area|i'm in|i am in|"
                                r"my address is|update my area", t) or (remember and p.area):
            p.intent, p.memory_key, p.memory_value = "memory_set", "area", p.area
            return p
        if (remember or re.search(r"earliest (start )?time", t)) and p.time_start is not None:
            p.intent, p.memory_key, p.memory_value = "memory_set", "earliest_time", p.time_start
            return p

        if re.search(r"\bcancel\b", t) and re.search(r"booking|appointment|visit|my |the ", t):
            p.intent = "cancel"
        elif re.search(r"\b(move|reschedule|shift|postpone|push|prepone)\b", t) and \
                not re.search(r"\b(i )?moved to\b", t):
            p.intent = "reschedule"
        elif re.search(r"\b(book|find|need|get|looking for|schedule|arrange|want|send)\b", t) and \
                (p.service or p.same_as_last or re.search(r"\b(plumber|electrician|someone|technician)\b", t)):
            p.intent = "book"

        sel = None
        m = re.search(r"^\s*(?:option\s*|#|no\.?\s*)?([1-9])\s*[.!]?\s*$", t)
        if m:
            sel = int(m.group(1))
        m = m or re.search(r"\b(?:go with|pick|choose|take|book|want|select)\s+(?:the\s+)?(?:option\s*|#|number\s*)?"
                           r"([1-9]|first|second|third|fourth|fifth)\b", t)
        if sel is None and m:
            sel = ORDINALS.get(m.group(1)) or int(m.group(1))
        m2 = re.search(r"\b(?:the\s+)?(first|second|third|fourth|fifth|1st|2nd|3rd)\s+(one|option|slot)\b", t)
        if sel is None and m2:
            sel = ORDINALS[m2.group(1)]
        if sel is None:
            m3 = re.search(r"\boption\s*([1-9])\b", t)
            sel = int(m3.group(1)) if m3 else None
        p.selection = sel
        return p

    @staticmethod
    def _service(t: str) -> str | None:
        best = None
        for svc, pat in SERVICE_WORDS:
            m = re.search(pat, t)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), svc)
        return best[1] if best else None

    @staticmethod
    def _memory_key(t: str) -> str | None:
        if re.search(r"\b(all|everything)\b", t):
            return "all"
        if re.search(r"area|location|where i live|address", t):
            return "area"
        if re.search(r"time|earliest|before|morning", t):
            return "earliest_time"
        if re.search(r"electrician|plumber|technician|provider|preferred|usual|cleaner|carpenter|ac", t):
            return "preferred_provider"
        return None

    @staticmethod
    def _dates(t: str, today: date, p: Parsed):
        def rng(a: date, b: date):
            p.date_from, p.date_to = a.isoformat(), b.isoformat()
        if re.search(r"\bday after tomorrow\b", t):
            d = today + timedelta(days=2); rng(d, d); return
        if re.search(r"\btoday\b", t):
            rng(today, today); return
        if re.search(r"\btomorrow\b", t):
            d = today + timedelta(days=1); rng(d, d); return
        if re.search(r"\bnext week\b", t):
            start = today + timedelta(days=7 - today.weekday())
            rng(start, start + timedelta(days=6)); return
        if re.search(r"\bthis week\b|\bthis coming week\b", t):
            rng(today, today + timedelta(days=6)); return
        if re.search(r"\b(this |the )?weekend\b", t):
            sat = today + timedelta(days=(5 - today.weekday()) % 7)
            rng(sat, sat + timedelta(days=1)); return
        m = re.search(r"\b(?:in|within) the next (\d{1,2}) days\b|\bwithin (\d{1,2}) days\b", t)
        if m:
            rng(today, today + timedelta(days=int(m.group(1) or m.group(2)))); return
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")[a-z]*\b", t) or \
            re.search(r"\b(" + "|".join(MONTHS) + r")[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
        if m:
            a, b = m.groups()
            day, mon = (int(a), b) if a.isdigit() else (int(b), a)
            try:
                d = date(today.year, MONTHS.index(mon[:3]) + 1, day)
                if d < today:
                    d = date(today.year + 1, d.month, d.day)
                rng(d, d); return
            except ValueError:
                pass
        for i, wd in enumerate(WEEKDAYS):
            if re.search(rf"\b({wd}|{wd[:3]})\b", t):
                ahead = (i - today.weekday()) % 7
                if re.search(rf"next\s+({wd}|{wd[:3]})", t) and ahead == 0:
                    ahead = 7
                d = today + timedelta(days=ahead)
                rng(d, d); return

    @staticmethod
    def _times(t: str, p: Parsed):
        if re.search(r"\bmornings?\b", t):
            p.time_start, p.time_end = 8 * 60, 12 * 60
        elif re.search(r"\bafternoons?\b", t):
            p.time_start, p.time_end = 12 * 60, 17 * 60
        elif re.search(r"\bevenings?\b", t):
            p.time_start, p.time_end = 17 * 60, 20 * 60
        t2 = re.sub(r"\bnoon\b", "12:00", t)
        m = re.search(r"(?:not before|no earlier than|not earlier than|after|from|earliest(?: time)?(?: is| to)?)\s+" + TIME, t2)
        if m:
            v = _to_minutes(*m.groups(), bare_ok=True)
            if v is not None:
                p.time_start = v
                p.time_end = p.time_end if (p.time_end and p.time_end > v) else None
        m = re.search(r"(?<!not\s)(?<!no\s)\b(?:before|by|no later than|until)\s+" + TIME, t2)
        if m:
            v = _to_minutes(*m.groups(), bare_ok=True)
            if v is not None:
                p.time_end = v
        if p.time_start is None and p.time_end is None:
            m = re.search(r"\bat\s+" + TIME, t2) or re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)", t2) \
                or re.search(r"\b(\d{1,2}):(\d{2})()\b", t2)
            if m:
                g = m.groups()
                v = _to_minutes(g[0], g[1], g[2] or None, bare_ok=bool(re.search(r"\bat\s+\d", t2)))
                if v is not None:
                    p.time_start, p.time_end, p.time_exact = v, v + 60, True

    @staticmethod
    def _budget(t: str, p: Parsed):
        if re.search(r"no budget|any price|no limit|price doesn'?t matter|don'?t care about (the )?price|"
                     r"no max|whatever it costs|no price limit", t):
            p.no_budget = True
            return
        m = re.search(r"(?:under|below|less than|within|max(?:imum)?|budget(?: of| is)?|up ?to|"
                      r"no more than|upto)\s*(?:₹|rs\.?|inr)?\s*([\d,]{3,})", t) or \
            re.search(r"(?:₹|\brs\.?\s*|\binr\s*)([\d,]{3,})", t)
        if m:
            v = int(m.group(1).replace(",", ""))
            if v >= 100:
                p.budget = v


class ScriptedModel:
    """Replays recorded model outputs (dicts) in order, then falls back to the rule-based model."""
    name = "scripted"

    def __init__(self, outputs: list):
        self.outputs = list(outputs)
        self.fallback = RuleBasedModel()

    def parse(self, text: str, ctx: dict):
        if not self.outputs:
            return self.fallback.parse(text, ctx)
        p, dropped = Parsed.from_dict(self.outputs.pop(0))
        return p, {"model": self.name, "input_tokens": len(text) // 4, "output_tokens": 0, "cost_usd": 0.0}, dropped


def extraction_prompt(ctx: dict) -> str:
    """System prompt shared by the LLM backends. The model only extracts; code decides everything."""
    today: date = ctx["today"]
    schema = {f.name: str(f.type) for f in fields(Parsed)}
    week_end = (today + timedelta(days=6)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    calendar = ", ".join(f"{(today + timedelta(days=i)).strftime('%A')} {(today + timedelta(days=i)).isoformat()}"
                         + (" (today)" if i == 0 else " (tomorrow)" if i == 1 else "") for i in range(8))
    return (
        "You extract structured fields from ONE user message for a home-services booking assistant. "
        "You do not book, approve, or answer; you only return a JSON object.\n"
        f"Today is {today.isoformat()} ({today.strftime('%A')}). Calendar: {calendar}. "
        "Always resolve relative dates with this calendar. "
        f"Conversation stage: {ctx.get('stage')}. "
        f"Options currently shown to the user: {ctx.get('n_options', 0)}.\n"
        f"JSON schema (field: type): {json.dumps(schema)}\n"
        "Rules:\n"
        f"- intent is one of {sorted(INTENTS)} or null. Use null when the message only adds details "
        "(e.g. just an area, a time, or an option number).\n"
        f"- service is one of {sorted(SERVICES)}; area is one of {AREAS} (exact spelling).\n"
        "- Dates are ISO YYYY-MM-DD; a single day sets date_from = date_to.\n"
        "- Times are integer minutes after midnight. mornings = 480-720, afternoons = 720-1020, "
        "evenings = 1020-1200. An exact time like 'at 11am' gives time_start=660, time_end=720, time_exact=true.\n"
        "- budget is an integer in rupees (₹1,500 -> 1500). no_budget=true if they say price doesn't matter.\n"
        "- selection is the 1-based option number the user picks ('2', 'the second one').\n"
        "- same_as_last=true for 'same as last time' / 'the same electrician'.\n"
        "- memory_set: a lasting preference ('my area is X', 'remember not before 10am'). memory_key is "
        "'area' (memory_value = area) or 'earliest_time' (memory_value = minutes).\n"
        "- memory_forget: memory_key is 'area', 'earliest_time', 'preferred_provider:<service>' or 'all'.\n"
        "- memory_view: 'what do you remember about me'.\n"
        "- Never output fields outside the schema. Words like 'yes' or 'go ahead' are NOT fields; return {}.\n"
        "- Use null / omit anything not stated. Do not guess.\n"
        "Examples:\n"
        f"'Find a plumber this week, mornings only, under ₹1,500' -> {{\"intent\":\"book\",\"service\":\"plumber\","
        f"\"date_from\":\"{today.isoformat()}\",\"date_to\":\"{week_end}\",\"time_start\":480,\"time_end\":720,"
        "\"budget\":1500}\n"
        f"'tomorrow at 11am' -> {{\"date_from\":\"{tomorrow}\",\"date_to\":\"{tomorrow}\",\"time_start\":660,"
        "\"time_end\":720,\"time_exact\":true}\n"
        "'Indiranagar' -> {\"area\":\"Indiranagar\"}\n"
        "'2' -> {\"selection\":2}\n"
        "'my area is HSR Layout' -> {\"intent\":\"memory_set\",\"area\":\"HSR Layout\",\"memory_key\":\"area\","
        "\"memory_value\":\"HSR Layout\"}\n"
        "'forget my preferred electrician' -> {\"intent\":\"memory_forget\",\"memory_key\":"
        "\"preferred_provider:electrician\"}\n"
        "'cancel my booking' -> {\"intent\":\"cancel\"}\n"
        "'move it to Friday afternoon' -> {\"intent\":\"reschedule\", ...dates for Friday..., \"time_start\":720,"
        "\"time_end\":1020}\n"
        "Reply with the JSON object only."
    )


class _LLMBase:
    """Schema-validated LLM extraction; any failure (network, JSON, schema) falls back to the
    deterministic parser. The model is never given tool output or reviews."""

    PRICE = {"input": 0.0, "output": 0.0}  # USD per token

    def __init__(self, api_key: str, model: str, transport=None, timeout: float = 20):
        self.api_key = api_key
        self.name = model
        self.timeout = timeout
        self.transport = transport or self._http
        self.fallback = RuleBasedModel()

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=headers | {"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def parse(self, text: str, ctx: dict):
        t0 = time.perf_counter()
        try:
            raw, n_in, n_out = self._call(extraction_prompt(ctx), text)
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            if not isinstance(data, dict):
                raise ValueError("model output is not a JSON object")
            p, dropped = Parsed.from_dict(data)
            usage = {"model": self.name, "input_tokens": n_in, "output_tokens": n_out,
                     "cost_usd": round(n_in * self.PRICE["input"] + n_out * self.PRICE["output"], 6),
                     "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
            # Calendar arithmetic is code's job: LLMs were observed resolving "tomorrow" / "next week"
            # to the wrong day. If the deterministic parser recognised a date phrase, its dates win.
            rules = self.fallback._parse(text, ctx)
            if rules.date_from and (rules.date_from, rules.date_to) != (p.date_from, p.date_to):
                usage["date_override"] = {"llm": [p.date_from, p.date_to], "code": [rules.date_from, rules.date_to]}
                p.date_from, p.date_to = rules.date_from, rules.date_to
            return p, usage, dropped
        except Exception as e:
            p, usage, dropped = self.fallback.parse(text, ctx)
            usage["fallback_reason"] = repr(e)[:160]
            return p, usage, dropped


class OpenAIModel(_LLMBase):
    """OpenAI Chat Completions in JSON mode. Model is configurable via OPENAI_MODEL."""

    PRICE = {"input": 2.5e-6, "output": 15e-6}  # approximate; only used for trace cost estimates
    DEFAULT = "gpt-6.1-terra"  # newest GPT-6.1 release; beat gpt-6.1-sol on our parse checks, faster, half price

    def __init__(self, api_key: str, model: str | None = None, transport=None, timeout: float = 30):
        super().__init__(api_key, model or self.DEFAULT, transport, timeout)

    def _http(self, payload: dict) -> dict:
        return self._post("https://api.openai.com/v1/chat/completions", payload,
                          {"authorization": f"Bearer {self.api_key}"})

    def _call(self, system: str, text: str):
        resp = self.transport({"model": self.name,
                               "messages": [{"role": "system", "content": system},
                                            {"role": "user", "content": text}],
                               "response_format": {"type": "json_object"}})
        u = resp.get("usage", {})
        return (resp["choices"][0]["message"]["content"] or "",
                u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


class LLMModel(_LLMBase):
    """Anthropic Messages API backend."""

    PRICE = {"input": 3e-6, "output": 15e-6}

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5", transport=None, timeout: float = 20):
        super().__init__(api_key, model, transport, timeout)

    def _http(self, payload: dict) -> dict:
        return self._post("https://api.anthropic.com/v1/messages", payload,
                          {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})

    def _call(self, system: str, text: str):
        resp = self.transport({"model": self.name, "max_tokens": 400, "system": system,
                               "messages": [{"role": "user", "content": text}]})
        u = resp.get("usage", {})
        return resp["content"][0]["text"], u.get("input_tokens", 0), u.get("output_tokens", 0)
