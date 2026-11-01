"""Recorded OpenAI responses ("cassette") so tests and red-team playbooks run offline through the real
OpenAIModel code path.

    replay (default)  a request with no recording fails loudly (CassetteMiss); nothing goes to the network
    record            CASSETTE_MODE=record: misses are sent to OpenAI (needs OPENAI_API_KEY) and saved;
                      (CASSETTE_PRUNE=1 also drops recordings the run didn't use; only do that on a full run)

Recordings are keyed by a hash of the whole request (model, system prompt, user text), so any prompt
change needs a fresh `CASSETTE_MODE=record python3 -m pytest`.
"""
import atexit
import hashlib
import json
import os
import threading

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "cassettes",
                            "openai.json")


class CassetteMiss(BaseException):
    """No recording for this request. BaseException so the agent's error handling can't swallow it."""


class Cassette:
    _instances: dict = {}

    def __init__(self, path: str = DEFAULT_PATH, mode: str | None = None):
        self.path = path
        self.mode = mode or os.environ.get("CASSETTE_MODE", "replay")
        self.lock = threading.Lock()
        self.used: set = set()
        self.dirty = False
        try:
            with open(path) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {}
        self.live = None
        if self.mode == "record":
            from .cli import load_dotenv
            from .nlu import OpenAIModel
            load_dotenv()
            self.live = OpenAIModel.from_env()._http
            atexit.register(self.save)

    @classmethod
    def shared(cls, path: str = DEFAULT_PATH):
        if path not in cls._instances:
            cls._instances[path] = cls(path)
        return cls._instances[path]

    @staticmethod
    def key(payload: dict) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]

    def __call__(self, payload: dict) -> dict:
        k = self.key(payload)
        with self.lock:
            self.used.add(k)
            if k in self.data:
                return self.data[k]["response"]
        if self.mode != "record":
            raise CassetteMiss(f"no recorded OpenAI response for {payload['messages'][-1]['content']!r} "
                               "(re-record with CASSETTE_MODE=record python3 -m pytest)")
        resp = self.live(payload)
        keep = {"choices": [{"message": {"content": resp["choices"][0]["message"]["content"]}}],
                "usage": {k2: resp.get("usage", {}).get(k2, 0) for k2 in ("prompt_tokens", "completion_tokens")}}
        with self.lock:
            self.data[k] = {"text": payload["messages"][-1]["content"], "response": keep}
            self.dirty = True
        return keep

    def save(self):
        with self.lock:
            if self.mode != "record":
                return
            prune = os.environ.get("CASSETTE_PRUNE") == "1"
            data = {k: v for k, v in self.data.items() if k in self.used or not prune}
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
            os.replace(tmp, self.path)


def replay_model():
    """An OpenAIModel wired to the shared cassette (offline unless CASSETTE_MODE=record). The model name is
    pinned to the default so recordings don't depend on OPENAI_MODEL."""
    from .nlu import OpenAIModel
    return OpenAIModel("", transport=Cassette.shared())
