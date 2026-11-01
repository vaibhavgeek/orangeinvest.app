"""Persistent user memory. Each fact records who it came from (stated vs inferred)."""
import json
import os
from datetime import datetime

LABELS = {"area": "Area", "earliest_time": "Earliest start time"}


def label(key: str) -> str:
    if key.startswith("preferred_provider:"):
        return "Preferred " + key.split(":", 1)[1].replace("_", " ")
    return LABELS.get(key, key)


def show_value(key: str, value) -> str:
    if key == "earliest_time" and isinstance(value, int):
        return f"{value // 60:02d}:{value % 60:02d}"
    if isinstance(value, dict):
        return f"{value.get('name')} ({value.get('id')})"
    return str(value)


class MemoryStore:
    def __init__(self, path: str):
        self.path = path
        self.facts: dict = {}
        if os.path.exists(path):
            with open(path) as f:
                self.facts = json.load(f)

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.facts, f, indent=1)
        os.replace(tmp, self.path)

    def active(self) -> dict:
        return {k: f for k, f in self.facts.items() if f["status"] == "active"}

    def get(self, key: str) -> dict | None:
        f = self.facts.get(key)
        return f if f and f["status"] == "active" else None

    def usable(self, key: str):
        """Value only if the user stated it or confirmed the inference. Forgotten facts return None."""
        f = self.get(key)
        if f and (f["source"] == "stated" or f["confirmed"]):
            return f["value"]
        return None

    def state(self, key: str, value):
        self.facts[key] = {"value": value, "source": "stated", "confirmed": True, "status": "active",
                           "updated_at": datetime.now().isoformat(timespec="seconds")}
        self._save()

    def infer(self, key: str, value, evidence: str) -> bool:
        f = self.facts.get(key)
        if f and (f["status"] == "forgotten" or f["source"] == "stated" or f["value"] == value):
            return False  # never resurrect forgotten facts, never override what the user said
        self.facts[key] = {"value": value, "source": "inferred", "confirmed": False, "status": "active",
                           "evidence": evidence, "updated_at": datetime.now().isoformat(timespec="seconds")}
        self._save()
        return True

    def booked(self, key: str, value, booking_id: str) -> bool:
        """The user explicitly approved a booking with this provider: that counts as confirming it."""
        f = self.facts.get(key)
        if f and (f["status"] == "forgotten" or f["source"] == "stated"):
            return False  # never resurrect forgotten facts, never override what the user said
        if f and f["value"] == value and f["confirmed"]:
            return False
        self.facts[key] = {"value": value, "source": "inferred", "confirmed": True, "status": "active",
                           "evidence": booking_id, "updated_at": datetime.now().isoformat(timespec="seconds")}
        self._save()
        return True

    def confirm(self, key: str):
        if self.get(key):
            self.facts[key]["confirmed"] = True
            self._save()

    def forget(self, key: str) -> list:
        keys = list(self.active()) if key == "all" else [k for k in self.active()
                                                        if k == key or k.startswith(key + ":")]
        for k in keys:
            self.facts[k] = {"value": None, "source": self.facts[k]["source"], "confirmed": False,
                             "status": "forgotten", "updated_at": datetime.now().isoformat(timespec="seconds")}
        self._save()
        return keys

    def describe(self) -> str:
        act = self.active()
        if not act:
            return "I don't have any saved preferences for you."
        lines = []
        for k, f in sorted(act.items()):
            if f["source"] == "stated":
                src = "stated by you"
            elif f["confirmed"]:
                src = (f"confirmed: you booked them ({f['evidence']})" if str(f.get("evidence", "")).startswith("BK-")
                       else "inferred from your bookings, confirmed by you")
            else:
                src = "inferred from your bookings — NOT used until you confirm"
            lines.append(f"• {label(k)}: {show_value(k, f['value'])} ({src})")
        return "Here's what I remember:\n" + "\n".join(lines) + \
            "\nYou can correct any of these (e.g. 'my area is HSR Layout') or say 'forget my area'."
