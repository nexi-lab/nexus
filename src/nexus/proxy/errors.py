"""Proxy brick exceptions.

All exceptions inherit from ProxyError so callers can catch the
entire family with a single except clause.
"""

from __future__ import annotations


class ProxyError(Exception):
    """Base exception for all proxy brick errors."""


class OfflineQueuedError(ProxyError):
    """Raised when an operation is queued for later replay.

    This is not a hard failure — the operation will be retried
    when connectivity is restored.
    """

    def __init__(self, method: str, queue_id: int) -> None:
        self.method = method
        self.queue_id = queue_id
        super().__init__(f"Operation '{method}' queued for offline replay (id={queue_id})")


class CircuitOpenError(ProxyError):
    """Raised when the circuit breaker is open.

    The remote is known-unreachable; callers should back off.
    """

    def __init__(self, remote_url: str, retry_after: float) -> None:
        self.remote_url = remote_url
        self.retry_after = retry_after
        super().__init__(f"Circuit breaker open for {remote_url}; retry after {retry_after:.1f}s")


class QueueReplayError(ProxyError):
    """Raised when a queued operation fails during replay."""

    def __init__(self, op_id: int, method: str, cause: Exception) -> None:
        self.op_id = op_id
        self.method = method
        self.cause = cause
        super().__init__(f"Failed to replay queued operation {op_id} ({method}): {cause}")


class RemoteCallError(ProxyError):
    """Raised when a remote call fails after all retries."""

    def __init__(
        self,
        method: str,
        *,
        status_code: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.method = method
        self.status_code = status_code
        self.cause = cause
        detail = f"Remote call '{method}' failed"
        if status_code is not None:
            detail += f" (HTTP {status_code})"
        if cause is not None:
            detail += f": {cause}"
        super().__init__(detail)
