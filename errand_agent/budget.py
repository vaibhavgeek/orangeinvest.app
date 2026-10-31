from dataclasses import dataclass


class LimitExceeded(Exception):
    def __init__(self, kind: str, limit):
        super().__init__(f"{kind} limit ({limit}) exceeded")
        self.kind = kind
        self.limit = limit


@dataclass
class Limits:
    max_turns: int = 40                 # user turns per session
    max_model_calls_per_turn: int = 3
    max_tool_calls_per_turn: int = 25
    max_turn_seconds: float = 30.0      # wall clock per turn
    max_attempts: int = 3               # per book/cancel/reschedule action


class Budget:
    def __init__(self, limits: Limits, clock):
        self.limits = limits
        self.clock = clock
        self.start_turn()

    def start_turn(self):
        self.model_calls = 0
        self.tool_calls = 0
        self.t0 = self.clock.monotonic()

    def check_time(self):
        if self.clock.monotonic() - self.t0 > self.limits.max_turn_seconds:
            raise LimitExceeded("wall_clock_seconds", self.limits.max_turn_seconds)

    def charge_model(self):
        self.check_time()
        self.model_calls += 1
        if self.model_calls > self.limits.max_model_calls_per_turn:
            raise LimitExceeded("model_calls", self.limits.max_model_calls_per_turn)

    def charge_tool(self):
        self.check_time()
        self.tool_calls += 1
        if self.tool_calls > self.limits.max_tool_calls_per_turn:
            raise LimitExceeded("tool_calls", self.limits.max_tool_calls_per_turn)
