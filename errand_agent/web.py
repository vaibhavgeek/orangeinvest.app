"""HTTP backend for the web test console (web/, React):  python -m errand_agent.web  ->  http://127.0.0.1:8000

JSON API under /api. Every browser tab gets its own isolated session (X-Session-Id header): its own mock provider
world, memory, clock and trace. If web/dist exists (npm run build), the built frontend is served from / too.
Stdlib only.
"""
import argparse
import json
import os
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
from .nlu import OpenAIModel

DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "dist")
PLAYBOOKS = os.path.join(os.path.dirname(__file__), "static", "playbooks.json")
SESSION = "web"
FIXED_START = datetime(2026, 11, 1, 8, 0)  # same as tests/helpers.py


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
    def __init__(self, data_dir: str, clock_factory=OffsetClock, model_factory=None, phrase_replies: bool = True):
        self.root = data_dir
        self.phrase_replies = phrase_replies
        self.clock_factory = clock_factory
        self.model_factory = model_factory or OpenAIModel.from_env
        self.lock = threading.Lock()
        self.agent = None
        self.config = {"scenario": "default", "user": "u1", "clock": "real"}
        self.crashed = False

    @property
    def data_dir(self):
        return os.path.join(self.root, "data")

    @property
    def trace_path(self):
        return os.path.join(self.root, "traces", f"{SESSION}.jsonl")

    def _boot(self, first: bool):
        """Start an agent process. On a restart the provider world, session state and memory are reloaded
        from disk, exactly as a real process restart would see them."""
        scenario = Scenario(**json.loads(json.dumps(SCENARIOS[self.config["scenario"]])))
        if not first:
            scenario.crash_after_book = False  # crash once; the restarted process should recover
        world = os.path.join(self.data_dir, "mock_world.json")
        self.api = MockProviderAPI(clock=self.clock, scenario=scenario, user_id=self.config["user"], path=world)
        self.agent = Agent(self.api, user_id=self.config["user"], session_id=SESSION, data_dir=self.data_dir,
                           model=self.model_factory(), clock=self.clock, limits=Limits(),
                           trace_dir=os.path.dirname(self.trace_path), phrase_replies=self.phrase_replies)
        self.crashed = False
        return self.agent.resume()

    def reset(self, cfg: dict) -> dict:
        new = {k: cfg.get(k, v) for k, v in self.config.items()}
        if new["scenario"] not in SCENARIOS:
            raise ValueError("unknown scenario")
        if new["clock"] not in ("real", "fixed"):
            raise ValueError("clock must be 'real' or 'fixed'")
        if not str(new["user"]).isalnum():
            raise ValueError("user id must be alphanumeric")
        self.config = new
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.data_dir, exist_ok=True)
        # "fixed" starts the clock at the same moment the offline tests use, so playbooks behave identically
        self.clock = OffsetClock(FIXED_START) if new["clock"] == "fixed" and self.clock_factory is OffsetClock \
            else self.clock_factory()
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


SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class Hub:
    """One App per browser session, created on first use. Oldest sessions are evicted past max_sessions.
    LLM-parsed messages are capped per day across all sessions so a public deployment can't run up a bill."""

    def __init__(self, root: str, max_sessions: int = 300,
                 llm_daily_cap: int = int(os.environ.get("LLM_DAILY_CAP", "1500"))):
        self.root = root
        self.max_sessions, self.llm_daily_cap = max_sessions, llm_daily_cap
        self.sessions: OrderedDict = OrderedDict()
        self.lock = threading.Lock()
        self.llm_day, self.llm_used = time.strftime("%Y-%m-%d"), 0
        shutil.rmtree(root, ignore_errors=True)

    def get(self, sid: str | None) -> App:
        sid = sid or "local"
        if sid != "local" and not SESSION_ID.match(sid):
            raise ValueError("bad session id")
        with self.lock:
            app = self.sessions.get(sid)
            if app is None:
                app = App(os.path.join(self.root, sid))
                app.reset({})
                self.sessions[sid] = app
                while len(self.sessions) > self.max_sessions:
                    _, old = self.sessions.popitem(last=False)
                    shutil.rmtree(old.root, ignore_errors=True)
            self.sessions.move_to_end(sid)
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
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Session-Id")
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
            return hub.get(self.headers.get("X-Session-Id"))

        def do_GET(self):
            try:
                if self.path == "/api/health":
                    return self._send(200, {"ok": True, "sessions": len(hub.sessions)})
                if self.path in ("/api/playbooks", "/playbooks.json"):
                    with open(PLAYBOOKS, "rb") as f:
                        return self._send(200, f.read())
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
                    elif self.path == "/api/reset":
                        out = app.reset(body)
                    elif self.path == "/api/restart":
                        out = app.restart()
                    elif self.path == "/api/advance":
                        out = app.advance(max(1, min(24 * 60, int(body.get("minutes", 15)))))
                    else:
                        return self._send(404, {"error": "not found"})
                return self._send(200, out)
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
