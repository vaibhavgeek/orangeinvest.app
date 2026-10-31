"""Local web UI:  python -m errand_agent.web  ->  http://127.0.0.1:8000

Chat on the left; live trace, workflow state, memory and the provider's ground-truth bookings on the right.
Controls let a tester pick a failure scenario, switch the parser (rule-based / OpenAI), fast-forward the clock
(e.g. to expire a hold) and kill + restart the agent process to test recovery. Stdlib only.
"""
import argparse
import json
import os
import shutil
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agent import Agent
from .budget import Limits
from .cli import SCENARIOS, load_dotenv
from .clock import SystemClock
from .mock_api import MockProviderAPI, Scenario, SimulatedCrash

STATIC = os.path.join(os.path.dirname(__file__), "static", "index.html")
PLAYBOOKS = os.path.join(os.path.dirname(__file__), "static", "playbooks.json")
SESSION = "web"


class OffsetClock(SystemClock):
    """Real time plus a tester-controlled offset, so holds can be expired without waiting 10 minutes."""

    def __init__(self):
        self.offset = timedelta()

    def now(self) -> datetime:
        return super().now() + self.offset

    def advance(self, minutes: float = 0, seconds: float = 0) -> None:
        self.offset += timedelta(minutes=minutes, seconds=seconds)


class App:
    def __init__(self, data_dir: str, clock_factory=OffsetClock):
        self.root = data_dir
        self.clock_factory = clock_factory
        self.lock = threading.Lock()
        self.agent = None
        self.config = {"scenario": "default", "parser": "rules", "user": "u1"}
        self.crashed = False

    @property
    def data_dir(self):
        return os.path.join(self.root, "data")

    @property
    def trace_path(self):
        return os.path.join(self.root, "traces", f"{SESSION}.jsonl")

    def _model(self):
        parser = self.config["parser"]
        if parser == "rules":
            return None
        from .nlu import LLMModel, OpenAIModel
        env = "OPENAI_API_KEY" if parser == "openai" else "ANTHROPIC_API_KEY"
        if not os.environ.get(env):
            raise ValueError(f"{env} is not set (put it in .env or the environment)")
        if parser == "openai":
            return OpenAIModel(os.environ[env], os.environ.get("OPENAI_MODEL"))
        return LLMModel(os.environ[env])

    def _boot(self, first: bool):
        """Start an agent process. On a restart the provider world, session state and memory are reloaded
        from disk, exactly as a real process restart would see them."""
        scenario = Scenario(**json.loads(json.dumps(SCENARIOS[self.config["scenario"]])))
        if not first:
            scenario.crash_after_book = False  # crash once; the restarted process should recover
        world = os.path.join(self.data_dir, "mock_world.json")
        self.api = MockProviderAPI(clock=self.clock, scenario=scenario, user_id=self.config["user"], path=world)
        self.agent = Agent(self.api, user_id=self.config["user"], session_id=SESSION, data_dir=self.data_dir,
                           model=self._model(), clock=self.clock, limits=Limits(),
                           trace_dir=os.path.dirname(self.trace_path))
        self.crashed = False
        return self.agent.resume()

    def reset(self, cfg: dict) -> dict:
        new = {k: cfg.get(k, v) for k, v in self.config.items()}
        if new["scenario"] not in SCENARIOS or new["parser"] not in ("rules", "openai", "anthropic"):
            raise ValueError("unknown scenario or parser")
        if not str(new["user"]).isalnum():
            raise ValueError("user id must be alphanumeric")
        old = self.config
        self.config = new
        try:
            self._model()
        except ValueError:
            self.config = old
            raise
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.data_dir, exist_ok=True)
        self.clock = self.clock_factory()
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
            "hold": S.get("hold"), "options": len(S["options"]), "turns": S["turns"],
            "max_turns": self.agent.limits.max_turns,
            "memory": self.agent.memory.facts, "bookings": bookings,
            "events": events, "next": since + len(events), "scenarios": sorted(SCENARIOS),
        }


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                with open(STATIC, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if self.path == "/playbooks.json":
                with open(PLAYBOOKS, "rb") as f:
                    return self._send(200, f.read())
            if self.path.startswith("/api/state"):
                since = 0
                if "since=" in self.path:
                    try:
                        since = max(0, int(self.path.split("since=", 1)[1].split("&")[0]))
                    except ValueError:
                        pass
                with app.lock:
                    return self._send(200, app.snapshot(since))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 64_000:
                    return self._send(413, {"error": "request too large"})
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("JSON object expected")
                with app.lock:
                    if self.path == "/api/message":
                        text = body.get("text")
                        if not isinstance(text, str):
                            raise ValueError("text must be a string")
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
    ap.add_argument("--llm", nargs="?", const="openai", choices=["openai", "anthropic"],
                    help="start with an LLM parser (can be switched in the UI)")
    a = ap.parse_args(argv)
    load_dotenv()
    app = App(a.data_dir)
    app.reset({"parser": a.llm or "rules"})
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    print(f"Errand agent web UI on http://{a.host}:{a.port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
