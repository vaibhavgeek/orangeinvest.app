import json
import os


class StateStore:
    """Workflow state per session, written atomically after every transition (write-ahead for actions)."""

    def __init__(self, data_dir: str):
        self.dir = os.path.join(data_dir, "sessions")
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, session_id: str) -> str:
        return os.path.join(self.dir, f"{session_id}.json")

    def load(self, session_id: str) -> dict | None:
        p = self._path(session_id)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return json.load(f)

    def save(self, state: dict):
        p = self._path(state["session_id"])
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, p)


def new_state(session_id: str, user_id: str) -> dict:
    return {
        "session_id": session_id,
        "user_id": user_id,
        "stage": "idle",          # idle | collecting | choosing | awaiting_approval | executing | done
        "intent": None,           # book | reschedule | cancel
        "request": {},            # known facts for the current errand
        "options": [],            # what we last showed the user
        "hold": None,
        "target": None,           # existing booking for reschedule / cancel
        "approval": None,
        "execution": None,        # write-ahead record of an in-flight mutating call
        "pending_memory": None,   # inferred fact awaiting the user's confirmation
        "last_result": None,
        "turns": 0,
        "history": [],            # last few {user, agent prose} turns, context for the reply model only
        "strikes": 0,             # attempts to use the agent for something other than errands (guard.py)
        "closed": False,          # True after MAX_STRIKES: the session answers without any model call
    }
