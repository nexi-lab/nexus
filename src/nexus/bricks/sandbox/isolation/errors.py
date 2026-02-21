"""Isolation error hierarchy.

All exceptions inherit from IsolationError so callers can catch the
entire family with a single except clause.  This is a standalone tree
— isolation errors represent a different concern from ProxyError.
"""

from __future__ import annotations


class IsolationError(Exception):
    """Base exception for all isolation errors."""


class IsolationStartupError(IsolationError):
    """Raised when a backend fails to import or initialise in the worker."""

    def __init__(self, module: str, cls: str, cause: Exception | None = None) -> None:
        self.module = module
        self.cls = cls
        self.cause = cause
        detail = f"Failed to start isolated backend {module}:{cls}"
        if cause is not None:
            detail += f": {cause}"
        super().__init__(detail)


class IsolationCallError(IsolationError):
    """Raised when a delegated method raises an exception in the worker."""

    def __init__(self, method: str, cause: BaseException | None = None) -> None:
        self.method = method
        self.cause = cause
        detail = f"Isolated call '{method}' failed"
        if cause is not None:
            detail += f": {cause}"
        super().__init__(detail)


class IsolationTimeoutError(IsolationError):
    """Raised when a delegated call exceeds its deadline."""

    def __init__(self, method: str, timeout: float) -> None:
        self.method = method
        self.timeout = timeout
        super().__init__(f"Isolated call '{method}' timed out after {timeout:.1f}s")


class IsolationPoolError(IsolationError):
    """Raised when the executor pool is shut down or unhealthy."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Isolation pool error: {reason}")
