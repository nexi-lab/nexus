"""Tests for cancellation handling (src/nexus/llm/cancellation.py)."""

from __future__ import annotations

from nexus.llm.cancellation import (
    AsyncCancellationToken,
    CancellationToken,
    request_shutdown,
    reset_shutdown_flag,
    should_continue,
)


class TestShutdownFlag:
    """Tests for the global shutdown flag functions."""

    def setup_method(self) -> None:
        """Reset global shutdown flag before each test."""
        reset_shutdown_flag()

    def teardown_method(self) -> None:
        """Reset global shutdown flag after each test."""
        reset_shutdown_flag()

    def test_should_continue_initially_true(self) -> None:
        """should_continue returns True when no shutdown has been requested."""
        assert should_continue() is True

    def test_request_shutdown(self) -> None:
        """After request_shutdown, should_continue returns False."""
        request_shutdown()
        assert should_continue() is False

    def test_reset_shutdown_flag(self) -> None:
        """reset_shutdown_flag restores should_continue to True."""
        request_shutdown()
        assert should_continue() is False

        reset_shutdown_flag()
        assert should_continue() is True


class TestCancellationToken:
    """Tests for the synchronous CancellationToken."""

    def setup_method(self) -> None:
        """Reset global shutdown flag before each test."""
        reset_shutdown_flag()

    def teardown_method(self) -> None:
        """Reset global shutdown flag after each test."""
        reset_shutdown_flag()

    def test_token_not_cancelled_initially(self) -> None:
        """A fresh token should not be cancelled."""
        token = CancellationToken()
        assert token.is_cancelled() is False

    def test_token_cancel(self) -> None:
        """Calling cancel() should make is_cancelled return True."""
        token = CancellationToken()
        token.cancel()
        assert token.is_cancelled() is True

    def test_token_check_shutdown_enabled(self) -> None:
        """With check_shutdown=True, global shutdown flag cancels the token."""
        token = CancellationToken(check_shutdown=True)
        assert token.is_cancelled() is False

        request_shutdown()
        assert token.is_cancelled() is True

    def test_token_check_shutdown_disabled(self) -> None:
        """With check_shutdown=False, global shutdown flag is ignored."""
        token = CancellationToken(check_shutdown=False)
        request_shutdown()
        assert token.is_cancelled() is False

    def test_token_with_callback_not_cancelled(self) -> None:
        """Token with callback returning False is not cancelled."""
        token = CancellationToken(on_cancel_fn=lambda: False)
        assert token.is_cancelled() is False

    def test_token_with_callback_cancelled(self) -> None:
        """Token with callback returning True is cancelled."""
        token = CancellationToken(on_cancel_fn=lambda: True)
        assert token.is_cancelled() is True

    def test_token_callback_checked_each_time(self) -> None:
        """Callback is re-evaluated on each is_cancelled call."""
        call_count = 0

        def toggle_cancel() -> bool:
            nonlocal call_count
            call_count += 1
            # Cancel on third call
            return call_count >= 3

        token = CancellationToken(on_cancel_fn=toggle_cancel)
        assert token.is_cancelled() is False  # call_count=1
        assert token.is_cancelled() is False  # call_count=2
        assert token.is_cancelled() is True   # call_count=3
        assert token.is_cancelled() is True   # call_count=4 (still True)

    def test_token_manual_cancel_takes_precedence(self) -> None:
        """Manual cancel returns True even if callback returns False."""
        token = CancellationToken(on_cancel_fn=lambda: False)
        assert token.is_cancelled() is False

        token.cancel()
        assert token.is_cancelled() is True

    def test_token_all_cancellation_sources(self) -> None:
        """Test interaction of manual cancel, callback, and shutdown flag."""
        # Start with nothing cancelled
        token = CancellationToken(on_cancel_fn=lambda: False, check_shutdown=True)
        assert token.is_cancelled() is False

        # Global shutdown triggers cancellation
        request_shutdown()
        assert token.is_cancelled() is True

        # Reset shutdown, still not cancelled (callback returns False)
        reset_shutdown_flag()
        assert token.is_cancelled() is False

        # Manual cancel overrides everything
        token.cancel()
        assert token.is_cancelled() is True


class TestAsyncCancellationToken:
    """Tests for the AsyncCancellationToken."""

    def setup_method(self) -> None:
        """Reset global shutdown flag before each test."""
        reset_shutdown_flag()

    def teardown_method(self) -> None:
        """Reset global shutdown flag after each test."""
        reset_shutdown_flag()

    def test_async_token_inherits_sync_behavior(self) -> None:
        """AsyncCancellationToken should be an instance of CancellationToken."""
        token = AsyncCancellationToken()
        assert isinstance(token, CancellationToken)

    async def test_async_token_not_cancelled_initially(self) -> None:
        """A fresh async token should not be cancelled."""
        token = AsyncCancellationToken()
        assert await token.is_cancelled_async() is False

    async def test_async_token_manual_cancel(self) -> None:
        """Calling cancel() on async token makes is_cancelled_async return True."""
        token = AsyncCancellationToken()
        token.cancel()
        assert await token.is_cancelled_async() is True

    async def test_async_token_check_shutdown(self) -> None:
        """Async token respects global shutdown flag."""
        token = AsyncCancellationToken(check_shutdown=True)
        assert await token.is_cancelled_async() is False

        request_shutdown()
        assert await token.is_cancelled_async() is True

    async def test_async_token_with_sync_callback(self) -> None:
        """Async token respects sync on_cancel_fn callback."""
        token = AsyncCancellationToken(on_cancel_fn=lambda: True)
        assert await token.is_cancelled_async() is True

    async def test_async_token_with_async_callback_not_cancelled(self) -> None:
        """Async callback returning False does not cancel the token."""

        async def not_cancelled() -> bool:
            return False

        token = AsyncCancellationToken(on_cancel_async_fn=not_cancelled)
        assert await token.is_cancelled_async() is False

    async def test_async_token_with_async_callback_cancelled(self) -> None:
        """Async callback returning True cancels the token."""

        async def is_cancelled() -> bool:
            return True

        token = AsyncCancellationToken(on_cancel_async_fn=is_cancelled)
        assert await token.is_cancelled_async() is True

    async def test_async_token_callback_exception_ignored(self) -> None:
        """If async callback raises, the exception is caught and token is not cancelled."""

        async def failing_callback() -> bool:
            raise RuntimeError("Test error")

        token = AsyncCancellationToken(on_cancel_async_fn=failing_callback)
        # Exception should be silently caught, token should NOT be cancelled
        assert await token.is_cancelled_async() is False

    async def test_async_token_multiple_cancellation_sources(self) -> None:
        """Test interaction of all cancellation sources for async token."""
        cancel_via_async = False

        async def async_check() -> bool:
            return cancel_via_async

        token = AsyncCancellationToken(
            on_cancel_fn=lambda: False,
            on_cancel_async_fn=async_check,
            check_shutdown=True,
        )

        # Nothing cancelled
        assert await token.is_cancelled_async() is False

        # Async callback triggers cancellation
        cancel_via_async = True
        assert await token.is_cancelled_async() is True

        # Reset async, check global shutdown
        cancel_via_async = False
        assert await token.is_cancelled_async() is False

        request_shutdown()
        assert await token.is_cancelled_async() is True

        # Reset shutdown, manual cancel takes effect
        reset_shutdown_flag()
        token.cancel()
        assert await token.is_cancelled_async() is True

    async def test_async_token_sync_check_before_async(self) -> None:
        """Manual cancel is checked synchronously before async callback is awaited."""
        call_count = 0

        async def async_check() -> bool:
            nonlocal call_count
            call_count += 1
            return False

        token = AsyncCancellationToken(on_cancel_async_fn=async_check)

        # Manual cancel should short-circuit: async callback should NOT be called
        token.cancel()
        assert await token.is_cancelled_async() is True
        # The sync is_cancelled() returns True first, so async callback is never reached
        assert call_count == 0
