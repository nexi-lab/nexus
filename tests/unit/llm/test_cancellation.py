"""Tests for LLM cancellation handling."""

import pytest

from nexus.llm.cancellation import (
    AsyncCancellationToken,
    CancellationToken,
    request_shutdown,
    reset_shutdown_flag,
    should_continue,
)


class TestShutdownFlag:
    """Test global shutdown flag functions."""

    def setup_method(self):
        """Reset shutdown flag before each test."""
        reset_shutdown_flag()

    def teardown_method(self):
        """Reset shutdown flag after each test."""
        reset_shutdown_flag()

    def test_should_continue_initially_true(self):
        """Test that should_continue is initially True."""
        assert should_continue() is True

    def test_request_shutdown(self):
        """Test requesting shutdown."""
        assert should_continue() is True
        request_shutdown()
        assert should_continue() is False

    def test_reset_shutdown_flag(self):
        """Test resetting shutdown flag."""
        request_shutdown()
        assert should_continue() is False
        reset_shutdown_flag()
        assert should_continue() is True


class TestCancellationToken:
    """Test CancellationToken class."""

    def setup_method(self):
        """Reset shutdown flag before each test."""
        reset_shutdown_flag()

    def teardown_method(self):
        """Reset shutdown flag after each test."""
        reset_shutdown_flag()

    def test_token_not_cancelled_initially(self):
        """Test that token is not cancelled initially."""
        token = CancellationToken()
        assert token.is_cancelled() is False

    def test_token_cancel(self):
        """Test cancelling a token."""
        token = CancellationToken()
        assert token.is_cancelled() is False
        token.cancel()
        assert token.is_cancelled() is True

    def test_token_check_shutdown_enabled(self):
        """Test token checks global shutdown flag when enabled."""
        token = CancellationToken(check_shutdown=True)
        assert token.is_cancelled() is False
        request_shutdown()
        assert token.is_cancelled() is True

    def test_token_check_shutdown_disabled(self):
        """Test token ignores global shutdown flag when disabled."""
        token = CancellationToken(check_shutdown=False)
        assert token.is_cancelled() is False
        request_shutdown()
        assert token.is_cancelled() is False

    def test_token_with_callback_not_cancelled(self):
        """Test token with callback that returns False."""
        callback_called = []

        def on_cancel():
            callback_called.append(True)
            return False

        token = CancellationToken(on_cancel_fn=on_cancel, check_shutdown=False)
        assert token.is_cancelled() is False
        assert len(callback_called) == 1

    def test_token_with_callback_cancelled(self):
        """Test token with callback that returns True."""
        callback_called = []

        def on_cancel():
            callback_called.append(True)
            return True

        token = CancellationToken(on_cancel_fn=on_cancel, check_shutdown=False)
        assert token.is_cancelled() is True
        assert len(callback_called) == 1

    def test_token_callback_checked_each_time(self):
        """Test that callback is checked on each is_cancelled call."""
        call_count = [0]

        def on_cancel():
            call_count[0] += 1
            return call_count[0] > 2

        token = CancellationToken(on_cancel_fn=on_cancel, check_shutdown=False)
        assert token.is_cancelled() is False  # call 1
        assert token.is_cancelled() is False  # call 2
        assert token.is_cancelled() is True  # call 3
        assert call_count[0] == 3

    def test_token_manual_cancel_takes_precedence(self):
        """Test that manual cancel takes precedence."""

        def on_cancel():
            return False

        token = CancellationToken(on_cancel_fn=on_cancel, check_shutdown=False)
        token.cancel()
        assert token.is_cancelled() is True

    def test_token_all_cancellation_sources(self):
        """Test token with all cancellation sources."""

        def on_cancel():
            return False

        # None should be cancelled
        token1 = CancellationToken(on_cancel_fn=on_cancel, check_shutdown=True)
        assert token1.is_cancelled() is False

        # Manual cancel
        token2 = CancellationToken(on_cancel_fn=on_cancel, check_shutdown=True)
        token2.cancel()
        assert token2.is_cancelled() is True

        # Shutdown flag
        request_shutdown()
        token3 = CancellationToken(on_cancel_fn=on_cancel, check_shutdown=True)
        assert token3.is_cancelled() is True


class TestAsyncCancellationToken:
    """Test AsyncCancellationToken class."""

    def setup_method(self):
        """Reset shutdown flag before each test."""
        reset_shutdown_flag()

    def teardown_method(self):
        """Reset shutdown flag after each test."""
        reset_shutdown_flag()

    def test_async_token_inherits_sync_behavior(self):
        """Test that async token has all sync token behavior."""
        token = AsyncCancellationToken()
        assert token.is_cancelled() is False
        token.cancel()
        assert token.is_cancelled() is True

    @pytest.mark.asyncio
    async def test_async_token_not_cancelled_initially(self):
        """Test that async token is not cancelled initially."""
        token = AsyncCancellationToken()
        assert await token.is_cancelled_async() is False

    @pytest.mark.asyncio
    async def test_async_token_manual_cancel(self):
        """Test manually cancelling async token."""
        token = AsyncCancellationToken()
        token.cancel()
        assert await token.is_cancelled_async() is True

    @pytest.mark.asyncio
    async def test_async_token_check_shutdown(self):
        """Test async token checks shutdown flag."""
        token = AsyncCancellationToken(check_shutdown=True)
        request_shutdown()
        assert await token.is_cancelled_async() is True

    @pytest.mark.asyncio
    async def test_async_token_with_sync_callback(self):
        """Test async token with sync callback."""

        def on_cancel():
            return True

        token = AsyncCancellationToken(on_cancel_fn=on_cancel, check_shutdown=False)
        assert await token.is_cancelled_async() is True

    @pytest.mark.asyncio
    async def test_async_token_with_async_callback_not_cancelled(self):
        """Test async token with async callback that returns False."""

        async def on_cancel_async():
            return False

        token = AsyncCancellationToken(on_cancel_async_fn=on_cancel_async, check_shutdown=False)
        assert await token.is_cancelled_async() is False

    @pytest.mark.asyncio
    async def test_async_token_with_async_callback_cancelled(self):
        """Test async token with async callback that returns True."""

        async def on_cancel_async():
            return True

        token = AsyncCancellationToken(on_cancel_async_fn=on_cancel_async, check_shutdown=False)
        assert await token.is_cancelled_async() is True

    @pytest.mark.asyncio
    async def test_async_token_callback_exception_ignored(self):
        """Test that async callback exceptions don't cancel."""

        async def on_cancel_async():
            raise RuntimeError("Test error")

        token = AsyncCancellationToken(on_cancel_async_fn=on_cancel_async, check_shutdown=False)
        assert await token.is_cancelled_async() is False

    @pytest.mark.asyncio
    async def test_async_token_multiple_cancellation_sources(self):
        """Test async token with multiple cancellation sources."""
        call_count = [0]

        def on_cancel_sync():
            call_count[0] += 1
            return False

        async def on_cancel_async():
            call_count[0] += 10
            return False

        token = AsyncCancellationToken(
            on_cancel_fn=on_cancel_sync,
            on_cancel_async_fn=on_cancel_async,
            check_shutdown=True,
        )

        # Should check sync callback first
        assert await token.is_cancelled_async() is False
        # Sync callback called via is_cancelled(), async callback also called
        assert call_count[0] >= 10

    @pytest.mark.asyncio
    async def test_async_token_sync_check_before_async(self):
        """Test that sync checks happen before async callback."""
        async_called = []

        async def on_cancel_async():
            async_called.append(True)
            return True

        token = AsyncCancellationToken(on_cancel_async_fn=on_cancel_async, check_shutdown=False)
        token.cancel()  # Manual cancel

        # Async callback should not be called because manual cancel is checked first
        result = await token.is_cancelled_async()
        assert result is True
        assert len(async_called) == 0  # Async callback not called due to early return
