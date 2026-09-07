"""Bounded request rates for the operations that cost money or CPU.

Scoped per authenticated principal rather than per IP, because every browser behind one corporate
NAT shares an address and would otherwise share a budget. An unauthenticated request is rejected
by the route before it reaches anything expensive, so this never needs to fall back to IP.

Deliberately in-process. A distributed limiter belongs in the ingress or an API gateway where the
whole fleet's traffic is visible, and building a Redis-backed one here would give a
worse-than-useless guarantee: N replicas would each allow the full budget while the code implied a
global one. What this does provide is a real per-replica ceiling that stops a single client
saturating a worker or spending a provider budget, and the deployment documentation states the
per-replica multiplication plainly rather than hiding it.

Health and readiness are never limited: throttling a probe turns load into an outage.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

from app.core.errors import DomainError


@dataclass
class SlidingWindow:
    """Per-key request timestamps within one window. Bounded by construction."""

    limit: int
    window_seconds: float
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)
    #: Keys tracked at once. A limiter that grows without bound under a spray of distinct
    #: principals would be a memory-exhaustion vector of its own.
    max_keys: int = 10_000

    def check(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self.max_keys:
                    self._evict(moment)
                hits = self._hits.setdefault(key, deque())
            cutoff = moment - self.window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(0.0, hits[0] + self.window_seconds - moment)
            hits.append(moment)
            return True, 0.0

    def _evict(self, moment: float) -> None:
        cutoff = moment - self.window_seconds
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            del self._hits[key]
        if len(self._hits) >= self.max_keys:
            # Still full of live keys: drop the oldest rather than refuse to track anything.
            oldest = sorted(self._hits, key=lambda k: self._hits[k][-1])[: self.max_keys // 10]
            for key in oldest:
                del self._hits[key]


class RateLimiter:
    """Named budgets, applied per tenant and user."""

    def __init__(self, budgets: dict[str, tuple[int, float]]) -> None:
        self._windows = {
            name: SlidingWindow(limit, window) for name, (limit, window) in budgets.items()
        }

    def enforce(self, budget: str, tenant_id: object, user_id: object) -> None:
        window = self._windows.get(budget)
        if window is None:
            return
        allowed, retry_after = window.check(f"{budget}:{tenant_id}:{user_id}")
        if not allowed:
            raise DomainError(
                "RATE_LIMITED",
                "Too many requests. Please retry shortly.",
                429,
                {"retry_after_seconds": round(retry_after, 1)},
            )
