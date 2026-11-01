"""HTTP backend for the web test console (web/, React):  python -m errand_agent.web  ->  http://127.0.0.1:8000

JSON API under /api. POST /api/session takes the password (or a 24-hour access pass handed out with the first
correct password) and issues an anonymous user with a signed token that is
valid for SESSION_TTL_SECONDS (15 minutes). Every other call sends it as `Authorization: Bearer <token>`. For those
15 minutes it is the same user: memory, bookings and the mock provider world survive "New chat" (a fresh
conversation), so earlier bookings can be rescheduled or cancelled. When the token expires the user and all their
data are deleted and the next session starts as a brand-new user. The user id only ever comes from the verified
token, never from a message or a request body. If web/dist exists (npm run build), the built frontend is served
from / too. Stdlib only.
"""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agent import Agent
from .budget import Limits
from .cli import SCENARIOS, load_dotenv
from .clock import SystemClock
from .mock_api import MockProviderAPI, Scenario, SimulatedCrash
from .nlu import REPLY_PROMPT, OpenAIModel, extraction_prompt

DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "dist")
PLAYBOOKS = os.path.join(os.path.dirname(__file__), "static", "playbooks.json")
FIXED_START = datetime(2026, 11, 1, 8, 0)  # same as tests/helpers.py
SESSION_TTL = int(os.environ.get("SESSION_TTL_SECONDS", str(15 * 60)))
PASSWORD = "VAIBHAV IS ORANGE"  # gates POST /api/session; nothing else works without a session token
ACCESS_TTL = 24 * 3600  # after the password, the browser can start new 15-minute users for a day without it


class AuthError(Exception):
    """Missing, forged or expired token, or a wrong password. `reason`: 'missing', 'invalid', 'expired', 'password'."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Tokens:
    """Stateless signed tokens: `<user>.<expiry>.<hmac>`. The secret is random per process unless SESSION_SECRET is
    set, so a server restart (which also wipes all session data) invalidates every token."""

    USER = re.compile(r"^u[0-9a-f]{10}$")

    def __init__(self, ttl: int = SESSION_TTL, secret: bytes | None = None, now=time.time):
        self.ttl, self.now = ttl, now
        self.secret = secret or os.environ.get("SESSION_SECRET", "").encode() or os.urandom(32)

    def _sign(self, body: str) -> str:
        return hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()[:32]

    def issue(self, user: str | None = None) -> dict:
        user, exp = user or "u" + secrets.token_hex(5), int(self.now()) + self.ttl
        body = f"{user}.{exp}"
        return {"token": f"{body}.{self._sign(body)}", "user": user, "expires_in": self.ttl, "ttl": self.ttl,
                "expires_at": exp}

    def verify(self, token: str | None) -> tuple:
        if not token:
            raise AuthError("missing")
        parts = str(token).split(".")
        if len(parts) != 3 or not self.USER.match(parts[0]) or not parts[1].isdigit():
            raise AuthError("invalid")
        user, exp, sig = parts
        if not hmac.compare_digest(sig, self._sign(f"{user}.{exp}")):
            raise AuthError("invalid")
        if int(exp) <= self.now():
            raise AuthError("expired")
        return user, int(exp)

    def access(self) -> str:
        """Proof that this browser knew the password. It can only start sessions; it is never a session token
        (its first part is 'access', which can't match USER)."""
        body = f"access.{int(self.now()) + ACCESS_TTL}"
        return f"{body}.{self._sign(body)}"

    def access_ok(self, token) -> bool:
        parts = str(token or "").split(".")
        return (len(parts) == 3 and parts[0] == "access" and parts[1].isdigit() and int(parts[1]) > self.now()
                and hmac.compare_digest(parts[2], self._sign(f"access.{parts[1]}")))


def password_ok(text) -> bool:
    return isinstance(text, str) and hmac.compare_digest(text.strip().encode(), PASSWORD.encode())


class OffsetClock(SystemClock):
    """Real time plus a tester-controlled offset, so holds can be expired without waiting 10 minutes."""

    def __init__(self, start: datetime | None = None):
        self.offset = timedelta()
        if start:
            self.offset = start - super().now()

    def now(self) -> datetime:
        return super().now() + self.offset

    def advance(self, minutes: float = 0, seconds: float = 0) -> None:
        self.offset += timedelta(minutes=minutes, seconds=seconds)


class App:
    """One user's world: mock provider, memory and bookings in `data/`, plus one agent conversation ("chat") at a
    time. `new_chat()` starts a fresh conversation for the same user; `reset()` wipes everything."""

    def __init__(self, data_dir: str, clock_factory=OffsetClock, model_factory=None, phrase_replies: bool = True,
                 user: str = "u1", expires_at: float | None = None):
        self.root = data_dir
        self.phrase_replies = phrase_replies
        self.clock_factory = clock_factory
        self.model_factory = model_factory or OpenAIModel.from_env
        self.lock = threading.Lock()
        self.agent = None
        self.user, self.expires_at = user, expires_at
        self.config = {"scenario": "default", "user": user, "clock": "real"}
        self.chat = 1
        self.crashed = False

    @property
    def data_dir(self):
        return os.path.join(self.root, "data")

    @property
    def session_id(self):
        return f"chat{self.chat}"

    @property
    def trace_path(self):
        return os.path.join(self.root, "traces", f"{self.session_id}.jsonl")

    def _boot(self, first: bool):
        """Start an agent process. On a restart the provider world, session state and memory are reloaded
        from disk, exactly as a real process restart would see them."""
        scenario = Scenario(**json.loads(json.dumps(SCENARIOS[self.config["scenario"]])))
        if not first:
            scenario.crash_after_book = False  # crash once; the restarted process should recover
        world = os.path.join(self.data_dir, "mock_world.json")
        self.api = MockProviderAPI(clock=self.clock, scenario=scenario, user_id=self.config["user"], path=world)
        self.agent = Agent(self.api, user_id=self.config["user"], session_id=self.session_id, data_dir=self.data_dir,
                           model=self.model_factory(), clock=self.clock, limits=Limits(),
                           trace_dir=os.path.dirname(self.trace_path), phrase_replies=self.phrase_replies)
        self.crashed = False
        return self.agent.resume()

    def reset(self, cfg: dict) -> dict:
        """Wipe this user's data (bookings, memory, chats) and start over. The user id is fixed by the session
        token, so a `user` in the request body is ignored."""
        new = {k: cfg.get(k, v) for k, v in self.config.items() if k != "user"} | {"user": self.user}
        if new["scenario"] not in SCENARIOS:
            raise ValueError("unknown scenario")
        if new["clock"] not in ("real", "fixed"):
            raise ValueError("clock must be 'real' or 'fixed'")
        self.config = new
        self.chat = 1
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.data_dir, exist_ok=True)
        # "fixed" starts the clock at the same moment the offline tests use, so playbooks behave identically
        self.clock = OffsetClock(FIXED_START) if new["clock"] == "fixed" and self.clock_factory is OffsetClock \
            else self.clock_factory()
        self._boot(first=True)
        return {"reply": None}

    def new_chat(self) -> dict:
        """A fresh conversation for the same user: bookings, memory, provider world and clock are kept, so an
        earlier booking can be rescheduled or cancelled. Strikes and any half-finished request stay behind."""
        if self.crashed:
            self._boot(first=False)  # recover the dead chat's in-flight booking before leaving it
        self.chat += 1
        self._boot(first=True)
        return {"reply": None}

    def restart(self) -> dict:
        note = self._boot(first=False)
        return {"reply": note or "(Agent process restarted. Nothing was in flight, so there was nothing to recover.)"}

    def message(self, text: str) -> dict:
        if self.crashed:
            return {"reply": "💥 The agent process is dead. Click “Restart process” to start a new one.", "system": True}
        try:
            return {"reply": self.agent.handle(text)}
        except SimulatedCrash:
            self.crashed = True
            return {"reply": "💥 Simulated crash: the process died right after the provider accepted the booking, "
                             "before the agent could record it. Click “Restart process” to watch it recover.",
                    "system": True}

    def advance(self, minutes: int) -> dict:
        self.clock.advance(minutes=minutes)
        return {"reply": f"⏩ Clock moved forward {minutes} minutes (now {self.clock.now():%a %d %b %H:%M}).",
                "system": True}

    def trace_export(self) -> dict:
        """Everything needed to debug this chat in one paste: the conversation, both system prompts and the raw
        trace. The parser prompt's last line changes every turn; each parse MODEL row carries its own copy."""
        rows = []
        if os.path.exists(self.trace_path):
            with open(self.trace_path) as f:
                rows = [json.loads(r) for r in f if r.strip()]
        chat = []
        for e in rows:
            if e["type"] == "user_message":
                chat.append(f"[t{e['turn']}] USER: {e['text']}")
            elif e["type"] == "agent_reply":
                chat.append(f"[t{e['turn']}] AGENT: {e['text']}")
        parts = [
            f"# Errand Agent trace — session {self.session_id}, model {self.agent.model.name}, "
            f"stage {self.agent.state['stage']}, clock {self.clock.now():%a %d %b %Y %H:%M}",
            "## Conversation", "\n".join(chat) or "(no messages yet)",
            "## Parser system prompt (last line = this turn's context; per-turn copies are in the MODEL parse rows)",
            extraction_prompt(self.agent.parse_ctx()),
            "## Reply-model system prompt", REPLY_PROMPT,
            "## Current request state", json.dumps(self.agent.state["request"], ensure_ascii=False, default=str),
            f"## Trace ({len(rows)} events, JSONL)", "\n".join(json.dumps(e, ensure_ascii=False) for e in rows),
        ]
        return {"text": "\n\n".join(parts)}

    def snapshot(self, since: int) -> dict:
        S = self.agent.state
        now = self.clock.now()
        bookings = []
        for b in sorted(self.api.bookings.values(), key=lambda b: b["slot"]):
            if b["user_id"] != self.config["user"]:
                continue
            status = "cancelled" if b.get("cancelled") else (
                "completed" if datetime.fromisoformat(b["slot"]) < now else "upcoming")
            bookings.append({k: b[k] for k in ("id", "provider_name", "service", "slot", "price")} | {"status": status})
        events = []
        if os.path.exists(self.trace_path):
            with open(self.trace_path) as f:
                rows = f.readlines()
            events = [json.loads(r) for r in rows[since:]]
        return {
            "config": self.config, "crashed": self.crashed, "now": now.strftime("%a %d %b %Y, %H:%M"),
            "user": self.user, "chat": self.chat,
            "expires_in": None if self.expires_at is None else max(0, int(self.expires_at - time.time())),
            "ttl": SESSION_TTL,
            "model": self.agent.model.name, "stage": S["stage"], "intent": S["intent"],
            "request": S["request"], "approval": S.get("approval"), "execution": S.get("execution"),
            "hold": S.get("hold"), "options": len(S["options"]), "option_labels": self.agent.option_labels(),
            "turns": S["turns"], "service": S["request"].get("service") or (S.get("target") or {}).get("service"),
            "suggestions": [x["label"] for x in S.get("suggestions") or []],
            "max_turns": self.agent.limits.max_turns,
            "closed": bool(S.get("closed")), "strikes": S.get("strikes", 0),
            "memory": self.agent.memory.facts, "bookings": bookings,
            "events": events, "next": since + len(events), "scenarios": sorted(SCENARIOS),
        }


class Hub:
    """One App per anonymous user, keyed by the user id inside a verified token and created on first use. Users
    whose token has expired are deleted (data and all) on the next request; the oldest are evicted past
    max_sessions. LLM-parsed messages are capped per day across all users so a public deployment can't run up a
    bill."""

    def __init__(self, root: str, max_sessions: int = 300,
                 llm_daily_cap: int = int(os.environ.get("LLM_DAILY_CAP", "1500")), tokens: Tokens | None = None):
        self.root = root
        self.max_sessions, self.llm_daily_cap = max_sessions, llm_daily_cap
        self.tokens = tokens or Tokens()
        self.sessions: OrderedDict = OrderedDict()
        self.lock = threading.Lock()
        self.llm_day, self.llm_used = time.strftime("%Y-%m-%d"), 0
        shutil.rmtree(root, ignore_errors=True)

    def _sweep(self) -> None:
        now = self.tokens.now()
        for user, app in list(self.sessions.items()):
            if app.expires_at is not None and app.expires_at <= now and app.lock.acquire(blocking=False):
                try:
                    del self.sessions[user]
                    shutil.rmtree(app.root, ignore_errors=True)
                finally:
                    app.lock.release()

    def issue(self, body: dict) -> dict:
        """A new anonymous user, for a request carrying the password or a still-valid access pass."""
        access = body.get("access")
        if not self.tokens.access_ok(access):
            if not password_ok(body.get("password")):
                raise AuthError("password")
            access = self.tokens.access()
        with self.lock:
            self._sweep()
        return self.tokens.issue() | {"access": access}

    def get(self, token: str | None) -> App:
        user, exp = self.tokens.verify(token)  # raises AuthError
        with self.lock:
            self._sweep()
            app = self.sessions.get(user)
            if app is None:
                app = App(os.path.join(self.root, user), user=user, expires_at=exp)
                app.reset({})
                self.sessions[user] = app
                while len(self.sessions) > self.max_sessions:
                    _, old = self.sessions.popitem(last=False)
                    shutil.rmtree(old.root, ignore_errors=True)
            self.sessions.move_to_end(user)
            return app

    def charge_llm(self) -> bool:
        with self.lock:
            today = time.strftime("%Y-%m-%d")
            if today != self.llm_day:
                self.llm_day, self.llm_used = today, 0
            if self.llm_used >= self.llm_daily_cap:
                return False
            self.llm_used += 1
            return True


CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json"}


def make_handler(hub: Hub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")  # no cookies; sessions are an explicit header
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()

        def _static(self, path: str):
            rel = os.path.normpath(path.split("?", 1)[0].lstrip("/")) or "index.html"
            full = os.path.join(DIST, rel)
            if rel.startswith("..") or not os.path.isfile(full):
                full = os.path.join(DIST, "index.html")  # SPA fallback
            if not os.path.isfile(full):
                return self._send(200, b"Backend is running. Build the frontend (cd web && npm run build) or use "
                                       b"./try_it.sh web.", "text/plain; charset=utf-8")
            with open(full, "rb") as f:
                return self._send(200, f.read(), CONTENT_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

        def _app(self) -> App:
            auth = self.headers.get("Authorization") or ""
            return hub.get(auth[7:].strip() if auth[:7].lower() == "bearer " else None)

        def _denied(self, e: AuthError):
            if e.reason == "password":
                time.sleep(0.5)  # slows down guessing
                return self._send(401, {"error": "Wrong password.", "password": True})
            msg = {"expired": "Your 15-minute session ended. Starting fresh as a new user.",
                   "missing": "No session token. Please start a new session.",
                   "invalid": "That session token isn't valid. Please start a new session."}[e.reason]
            return self._send(401, {"error": msg, "expired": True, "reason": e.reason})

        def do_GET(self):
            try:
                if self.path == "/api/health":
                    return self._send(200, {"ok": True, "sessions": len(hub.sessions)})
                if self.path in ("/api/playbooks", "/playbooks.json"):
                    with open(PLAYBOOKS, "rb") as f:
                        return self._send(200, f.read())
                if self.path == "/api/trace/export":
                    app = self._app()
                    with app.lock:
                        return self._send(200, app.trace_export())
                if self.path.startswith("/api/state"):
                    since = 0
                    if "since=" in self.path:
                        try:
                            since = max(0, int(self.path.split("since=", 1)[1].split("&")[0]))
                        except ValueError:
                            pass
                    app = self._app()
                    with app.lock:
                        return self._send(200, app.snapshot(since))
                if self.path.startswith("/api/"):
                    return self._send(404, {"error": "not found"})
                return self._static(self.path)
            except AuthError as e:
                return self._denied(e)
            except ValueError as e:
                return self._send(400, {"error": str(e)})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 64_000:
                    return self._send(413, {"error": "request too large"})
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("JSON object expected")
                if self.path == "/api/session":
                    return self._send(200, hub.issue(body))
                app = self._app()
                with app.lock:
                    if self.path == "/api/message":
                        text = body.get("text")
                        if not isinstance(text, str):
                            raise ValueError("text must be a string")
                        if not hub.charge_llm():
                            return self._send(429, {"error": "Today's message budget for this demo is used up. "
                                                             "Please try again tomorrow."})
                        out = app.message(text)
                    elif self.path == "/api/chat/new":
                        # same user and data, fresh 15 minutes: the old token stays valid until it runs out, but
                        # the user now lives until the new expiry
                        out = app.new_chat() | hub.tokens.issue(app.user)
                        app.expires_at = out.pop("expires_at")
                    elif self.path == "/api/reset":
                        out = app.reset(body)
                    elif self.path == "/api/restart":
                        out = app.restart()
                    elif self.path == "/api/advance":
                        out = app.advance(max(1, min(24 * 60, int(body.get("minutes", 15)))))
                    else:
                        return self._send(404, {"error": "not found"})
                return self._send(200, out)
            except AuthError as e:
                return self._denied(e)
            except (ValueError, TypeError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": f"server error: {e!r}"[:300]})

    return H


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (localhost only by default)")
    ap.add_argument("--data-dir", default="web_data")
    a = ap.parse_args(argv)
    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set (put it in .env or the environment). The agent parses with OpenAI only.")
    hub = Hub(a.data_dir)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(hub))
    print(f"Errand agent backend on http://{a.host}:{a.port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
