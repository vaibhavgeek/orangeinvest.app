"""One JSONL trace per session run. Secrets are redacted by key name and value shape."""
import json
import os
import re
import time

SECRET_KEYS = {"api_key", "apikey", "x-api-key", "authorization", "password", "secret",
               "access_token", "refresh_token", "auth_token"}
SECRET_VALUE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+)")


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if str(k).lower() in SECRET_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return SECRET_VALUE.sub("[REDACTED]", obj)
    return obj


class Tracer:
    def __init__(self, session_id: str, path: str | None = None):
        self.session_id = session_id
        self.path = path
        self.events: list = []
        self.turn = 0
        self.seq = 0
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def event(self, type_: str, **data) -> dict:
        self.seq += 1
        ev = {"seq": self.seq, "ts": round(time.time(), 3), "session_id": self.session_id,
              "turn": self.turn, "type": type_, **redact(data)}
        self.events.append(ev)
        if self.path:
            with open(self.path, "a") as f:
                f.write(json.dumps(ev, default=str) + "\n")
        return ev

    def of_type(self, type_: str) -> list:
        return [e for e in self.events if e["type"] == type_]
