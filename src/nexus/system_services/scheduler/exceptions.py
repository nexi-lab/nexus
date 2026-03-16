"""Scheduler-specific exceptions for admission control and overlap policies.

Provides typed exceptions that map cleanly to HTTP status codes:
- SubmissionError (base) -> 400
- RateLimitExceeded -> 429
- CapacityExceeded -> 429
- TaskAlreadyRunning -> 409

Related: Issue #2749
"""


class SubmissionError(Exception):
    """Base exception for task submission failures."""


class RateLimitExceeded(SubmissionError):
    """Agent has exceeded its per-second submission rate limit.

    Maps to HTTP 429 Too Many Requests.
    """


class CapacityExceeded(SubmissionError):
    """Agent has reached its maximum concurrent task limit (fair-share).

    Maps to HTTP 429 Too Many Requests.
    """


class TaskAlreadyRunning(SubmissionError):
    """A task with the same idempotency_key is already running (SKIP policy).

    Maps to HTTP 409 Conflict.
    """
