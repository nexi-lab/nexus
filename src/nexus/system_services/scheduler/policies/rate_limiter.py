"""Per-agent token-bucket rate limiter (Issue #2749).

In-memory token-bucket that limits submission rate (tasks/second)
per agent. Uses LRUCache to bound memory, same as FairShareCounter.

Separate from fair-share: rate = submission speed, fair-share = execution concurrency.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from cachetools import LRUCache


@dataclass(slots=True)
class _Bucket:
    """Mutable token bucket state for a single agent."""

    tokens: float
    last_refill: float  # monotonic timestamp


# Default rate limit: 10 tasks per second per agent
_DEFAULT_RATE = 10.0


class TokenBucketLimiter:
    """Per-agent token-bucket rate limiter.

    Each agent gets a bucket that refills at ``rate`` tokens/second
    up to a maximum of ``burst`` tokens. A submission consumes one token.

    Thread-safety: designed for single-threaded asyncio use,
    same as FairShareCounter.

    Args:
        rate: Tokens added per second (default 10.0).
        burst: Maximum tokens in the bucket (default equals rate).
        max_agents: LRU cache capacity for agent buckets.
        clock: Callable returning monotonic time (for testing).
    """

    def __init__(
        self,
        *,
        rate: float = _DEFAULT_RATE,
        burst: float | None = None,
        max_agents: int = 4096,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self._rate = rate
        self._burst = burst if burst is not None else rate
        if self._burst <= 0:
            raise ValueError(f"burst must be positive, got {self._burst}")
        self._buckets: LRUCache[str, _Bucket] = LRUCache(maxsize=max_agents)
        self._clock = clock if clock is not None else time.monotonic

    @property
    def rate(self) -> float:
        """Tokens per second."""
        return self._rate

    @property
    def burst(self) -> float:
        """Maximum burst capacity."""
        return self._burst

    def _get_or_create(self, agent_id: str) -> _Bucket:
        """Get existing bucket or create a new full one."""
        bucket = self._buckets.get(agent_id)
        if bucket is None:
            now = self._clock()
            bucket = _Bucket(tokens=self._burst, last_refill=now)
            self._buckets[agent_id] = bucket
        return bucket

    def _refill(self, bucket: _Bucket) -> None:
        """Refill tokens based on elapsed time."""
        now = self._clock()
        elapsed = now - bucket.last_refill
        if elapsed > 0:
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
            bucket.last_refill = now

    def try_acquire(self, agent_id: str) -> bool:
        """Try to consume one token from the agent's bucket.

        Returns True if the token was consumed (submission allowed),
        False if the bucket is empty (rate limit exceeded).
        """
        bucket = self._get_or_create(agent_id)
        self._refill(bucket)
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        return False
