import time
from datetime import datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().replace(second=0, microsecond=0)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """Deterministic clock for tests. sleep() advances time instead of blocking."""

    def __init__(self, start: datetime = datetime(2026, 11, 1, 8, 0)):
        self._now = start
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.advance(seconds=seconds)

    def advance(self, minutes: float = 0, seconds: float = 0) -> None:
        d = timedelta(minutes=minutes, seconds=seconds)
        self._now += d
        self._mono += d.total_seconds()
