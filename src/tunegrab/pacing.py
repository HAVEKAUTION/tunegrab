"""Keep download starts under YouTube's per-connection budget.

yt-dlp's wiki puts it at roughly 300 videos an hour for a guest session (more when signed
in); past that YouTube answers "try again later" and can block the connection for about an
hour (docs/upgrade/02b_YOUTUBE_CONSTRAINTS.md, measure 3). A token bucket lets an album or
two go at full speed and only paces long runs. One bucket per process: the limit is per
connection, not per job.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

GUEST_PER_HOUR = 250  # a margin under the ~300/hour guest budget
SIGNED_IN_PER_HOUR = 1000  # with browser cookies (the wiki says ~2,000)
BURST = 30  # starts allowed back to back before pacing kicks in


class Pacer:
    """Token bucket: :meth:`reserve` books the next start and says how long to wait."""

    def __init__(
        self,
        per_hour: float,
        burst: int = BURST,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self.per_hour = per_hour
        self.burst = burst
        self._tokens = float(burst)
        self._updated = clock()

    @property
    def unlimited(self) -> bool:
        return self.per_hour <= 0

    def set_rate(self, per_hour: float) -> None:
        with self._lock:
            self._refill()
            self.per_hour = per_hour

    def _refill(self) -> None:
        now = self._clock()
        if not self.unlimited:
            rate = self.per_hour / 3600.0
            self._tokens = min(float(self.burst), self._tokens + (now - self._updated) * rate)
        self._updated = now

    def reserve(self) -> float:
        """Take the next start slot; returns the seconds to wait before starting (0 = now)."""
        if self.unlimited:
            return 0.0
        with self._lock:
            self._refill()
            self._tokens -= 1.0
            if self._tokens >= 0:
                return 0.0
            return -self._tokens / (self.per_hour / 3600.0)


_shared: Pacer | None = None
_shared_lock = threading.Lock()


def youtube_pacer(signed_in: bool) -> Pacer:
    """The process-wide pacer, at the guest or signed-in rate."""
    global _shared
    rate = SIGNED_IN_PER_HOUR if signed_in else GUEST_PER_HOUR
    with _shared_lock:
        if _shared is None:
            _shared = Pacer(rate)
        elif _shared.per_hour != rate:
            _shared.set_rate(rate)
        return _shared
