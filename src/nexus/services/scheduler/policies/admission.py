"""Unified admission policy composing rate limiting and fair-share (Issue #2749).

Single entry point for all pre-enqueue admission checks:
1. Token-bucket rate limiting (tasks/second)
2. Fair-share concurrency limiting (running tasks)

Raises typed exceptions that map to HTTP status codes.
"""

from nexus.services.scheduler.exceptions import CapacityExceeded, RateLimitExceeded
from nexus.services.scheduler.policies.fair_share import FairShareCounter
from nexus.services.scheduler.policies.rate_limiter import TokenBucketLimiter


class AdmissionPolicy:
    """Composes rate limiting and fair-share into a single admission check.

    Usage::

        policy = AdmissionPolicy(fair_share=fs, rate_limiter=rl)
        policy.check(agent_id)  # raises on rejection

    Args:
        fair_share: Per-agent concurrency tracker.
        rate_limiter: Per-agent token-bucket rate limiter.
    """

    def __init__(
        self,
        *,
        fair_share: FairShareCounter,
        rate_limiter: TokenBucketLimiter,
    ) -> None:
        self._fair_share = fair_share
        self._rate_limiter = rate_limiter

    @property
    def fair_share(self) -> FairShareCounter:
        """Access the underlying fair-share counter."""
        return self._fair_share

    @property
    def rate_limiter(self) -> TokenBucketLimiter:
        """Access the underlying rate limiter."""
        return self._rate_limiter

    def check(self, agent_id: str) -> None:
        """Run all admission checks for the given agent.

        Checks are ordered cheapest-first:
        1. Rate limit (pure in-memory, O(1))
        2. Fair-share capacity (pure in-memory, O(1))

        Raises:
            RateLimitExceeded: If the agent exceeds its submission rate.
            CapacityExceeded: If the agent is at its concurrent task limit.
        """
        if not self._rate_limiter.try_acquire(agent_id):
            raise RateLimitExceeded(
                f"Agent {agent_id} exceeds {self._rate_limiter.rate}/s submission rate"
            )

        if not self._fair_share.admit(agent_id):
            snap = self._fair_share.snapshot(agent_id)
            raise CapacityExceeded(
                f"Agent {agent_id} is at capacity ({snap.running_count}/{snap.max_concurrent})"
            )
