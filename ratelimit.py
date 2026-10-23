"""Sliding-window rate limiter for sign-in and registration.

State is held in memory, so lockouts clear when the server restarts. That suits a
single server process; a shared store would be needed to span several processes.

attempt() counts an attempt before the password is checked, and blocked attempts
are not counted. The check and the count happen under one lock, so a burst of
parallel requests cannot all slip past the limit. A lockout therefore ends a fixed
time after the limit was reached, and continued guessing does not extend it.
"""
import threading
import time
from collections import deque

MAX_KEYS = 50_000  # bounds memory when many distinct keys are tried


class RateLimiter:
    def __init__(self, limit, window, clock=time.monotonic):
        if limit < 1 or window <= 0:
            raise ValueError("limit must be at least 1 and window must be above 0 seconds")
        self.limit = limit
        self.window = window
        self._clock = clock
        self._events = {}
        self._lock = threading.Lock()

    def _recent(self, key, now):
        """Return the key's attempts still inside the window, or None. Caller holds the lock."""
        q = self._events.get(key)
        if q is None:
            return None
        while q and now - q[0] >= self.window:
            q.popleft()
        if not q:
            del self._events[key]
            return None
        return q

    def _sweep(self, now):
        for key in list(self._events):
            self._recent(key, now)

    def attempt(self, key):
        """Try to start an attempt for `key`.

        Returns 0.0 if the attempt is allowed (and is now counted), or the number of
        seconds to wait if the key is locked out (and the attempt is not counted).
        """
        now = self._clock()
        with self._lock:
            q = self._recent(key, now)
            if q is not None and len(q) >= self.limit:
                # Locked until enough old attempts expire that fewer than `limit` remain.
                return self.window - (now - q[len(q) - self.limit])
            if len(self._events) >= MAX_KEYS:
                self._sweep(now)
            self._events.setdefault(key, deque()).append(now)
            return 0.0

    def reset(self, key):
        """Forget all counted attempts for `key`, e.g. after a successful sign-in."""
        with self._lock:
            self._events.pop(key, None)