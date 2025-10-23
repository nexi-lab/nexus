"""Cancellation handling utilities for async LLM operations."""

from __future__ import annotations

import signal
from collections.abc import Awaitable, Callable

# Global shutdown flag
_shutdown_requested = False


def _signal_handler(_signum: int, _frame: object) -> None:
    """Handle shutdown signals."""
    global _shutdown_requested
    _shutdown_requested = True


def should_continue() -> bool:
    """Check if operations should continue.

    Returns:
        True if operations should continue, False if shutdown requested
    """
    return not _shutdown_requested


def request_shutdown() -> None:
    """Request a graceful shutdown."""
    global _shutdown_requested
    _shutdown_requested = True


def reset_shutdown_flag() -> None:
    """Reset the shutdown flag (useful for testing)."""
    global _shutdown_requested
    _shutdown_requested = False


def install_signal_handlers() -> None:
    """Install signal handlers for graceful shutdown."""
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


class CancellationToken:
    """Token for tracking and coordinating cancellation across async operations."""

    def __init__(
        self,
        on_cancel_fn: Callable[[], bool] | None = None,
        check_shutdown: bool = True,
    ):
        """Initialize cancellation token.

        Args:
            on_cancel_fn: Optional sync callback that returns True if cancellation requested
            check_shutdown: Whether to check global shutdown flag
        """
        self._on_cancel_fn = on_cancel_fn
        self._check_shutdown = check_shutdown
        self._cancelled = False

    def is_cancelled(self) -> bool:
        """Check if cancellation has been requested.

        Returns:
            True if cancelled, False otherwise
        """
        if self._cancelled:
            return True

        if self._check_shutdown and not should_continue():
            return True

        return bool(self._on_cancel_fn and self._on_cancel_fn())

    def cancel(self) -> None:
        """Request cancellation."""
        self._cancelled = True


class AsyncCancellationToken(CancellationToken):
    """Async version of cancellation token supporting async callbacks."""

    def __init__(
        self,
        on_cancel_fn: Callable[[], bool] | None = None,
        on_cancel_async_fn: Callable[[], Awaitable[bool]] | None = None,
        check_shutdown: bool = True,
    ):
        """Initialize async cancellation token.

        Args:
            on_cancel_fn: Optional sync callback that returns True if cancellation requested
            on_cancel_async_fn: Optional async callback that returns True if cancellation requested
            check_shutdown: Whether to check global shutdown flag
        """
        super().__init__(on_cancel_fn, check_shutdown)
        self._on_cancel_async_fn = on_cancel_async_fn

    async def is_cancelled_async(self) -> bool:
        """Async check if cancellation has been requested.

        Returns:
            True if cancelled, False otherwise
        """
        # Check sync conditions first
        if self.is_cancelled():
            return True

        # Check async callback
        if self._on_cancel_async_fn:
            try:
                if await self._on_cancel_async_fn():
                    return True
            except Exception:
                # If callback fails, don't cancel
                pass

        return False
