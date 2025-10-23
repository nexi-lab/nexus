"""Tests for async LLM provider with cancellation support."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from nexus.llm import (
    AsyncCancellationToken,
    CancellationToken,
    LLMCancellationError,
    LLMConfig,
    LLMProvider,
    Message,
    MessageRole,
    request_shutdown,
    reset_shutdown_flag,
    should_continue,
)


class TestCancellationToken:
    """Tests for CancellationToken."""

    def test_token_creation(self):
        """Test creating a cancellation token."""
        token = CancellationToken()
        assert not token.is_cancelled()

    def test_token_cancel(self):
        """Test manual cancellation."""
        token = CancellationToken()
        token.cancel()
        assert token.is_cancelled()

    def test_token_with_callback(self):
        """Test token with custom callback."""
        should_cancel = False

        def check_cancel():
            return should_cancel

        token = CancellationToken(on_cancel_fn=check_cancel)
        assert not token.is_cancelled()

        should_cancel = True
        assert token.is_cancelled()

    def test_token_with_shutdown_flag(self):
        """Test token with global shutdown flag."""
        reset_shutdown_flag()
        token = CancellationToken(check_shutdown=True)
        assert not token.is_cancelled()

        request_shutdown()
        assert token.is_cancelled()

        # Clean up
        reset_shutdown_flag()

    def test_token_without_shutdown_check(self):
        """Test token that ignores shutdown flag."""
        reset_shutdown_flag()
        token = CancellationToken(check_shutdown=False)

        request_shutdown()
        assert not token.is_cancelled()

        # Clean up
        reset_shutdown_flag()


class TestAsyncCancellationToken:
    """Tests for AsyncCancellationToken."""

    @pytest.mark.asyncio
    async def test_async_token_creation(self):
        """Test creating an async cancellation token."""
        token = AsyncCancellationToken()
        assert not await token.is_cancelled_async()

    @pytest.mark.asyncio
    async def test_async_token_cancel(self):
        """Test manual cancellation."""
        token = AsyncCancellationToken()
        token.cancel()
        assert await token.is_cancelled_async()

    @pytest.mark.asyncio
    async def test_async_token_with_async_callback(self):
        """Test token with async callback."""
        should_cancel = False

        async def check_cancel():
            return should_cancel

        token = AsyncCancellationToken(on_cancel_async_fn=check_cancel)
        assert not await token.is_cancelled_async()

        should_cancel = True
        assert await token.is_cancelled_async()

    @pytest.mark.asyncio
    async def test_async_token_with_sync_callback(self):
        """Test token with sync callback."""
        should_cancel = False

        def check_cancel():
            return should_cancel

        token = AsyncCancellationToken(on_cancel_fn=check_cancel)
        assert not await token.is_cancelled_async()

        should_cancel = True
        assert await token.is_cancelled_async()

    @pytest.mark.asyncio
    async def test_async_token_callback_exception(self):
        """Test that callback exceptions don't break cancellation."""

        async def bad_callback():
            raise ValueError("Test error")

        token = AsyncCancellationToken(on_cancel_async_fn=bad_callback)
        # Should not raise, just return False
        assert not await token.is_cancelled_async()


class TestAsyncLLMProvider:
    """Tests for async LLM provider methods."""

    @pytest.mark.asyncio
    async def test_async_complete_basic(self):
        """Test basic async completion without cancellation."""
        config = LLMConfig(model="gpt-4", api_key=SecretStr("test-key"))
        provider = LLMProvider.from_config(config)

        messages = [Message(role=MessageRole.USER, content="Hello")]

        # Mock the acompletion to avoid real API calls
        mock_response = {
            "id": "test-id",
            "choices": [{"message": {"content": "Hello back!"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }

        with patch.object(provider, "_acompletion_partial", new_callable=AsyncMock) as mock:
            mock.return_value = mock_response

            response = await provider.complete_async(messages)

            assert response.content == "Hello back!"
            assert response.response_id == "test-id"
            assert provider.metrics.total_requests > 0

    @pytest.mark.asyncio
    async def test_async_complete_with_cancellation(self):
        """Test async completion with cancellation token."""
        config = LLMConfig(model="gpt-4", api_key=SecretStr("test-key"))
        provider = LLMProvider.from_config(config)

        messages = [Message(role=MessageRole.USER, content="Hello")]

        # Create token that cancels immediately
        token = AsyncCancellationToken()
        token.cancel()

        # Mock slow acompletion
        async def slow_completion(*args, **kwargs):
            await asyncio.sleep(10)
            return {"choices": [{"message": {"content": "Should not see this"}}]}

        with patch.object(provider, "_acompletion_partial", new_callable=AsyncMock) as mock:
            mock.side_effect = slow_completion

            with pytest.raises(LLMCancellationError):
                await provider.complete_async(messages, cancellation_token=token)

    @pytest.mark.asyncio
    async def test_async_complete_concurrent(self):
        """Test multiple concurrent async completions."""
        config = LLMConfig(model="gpt-4", api_key=SecretStr("test-key"))
        provider = LLMProvider.from_config(config)

        messages = [Message(role=MessageRole.USER, content="Hello")]

        # Mock responses
        mock_responses = [
            {
                "id": f"test-id-{i}",
                "choices": [{"message": {"content": f"Response {i}"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            }
            for i in range(3)
        ]

        call_count = 0

        async def mock_acompletion(*args, **kwargs):
            nonlocal call_count
            response = mock_responses[call_count % len(mock_responses)]
            call_count += 1
            await asyncio.sleep(0.1)  # Simulate network delay
            return response

        with patch.object(provider, "_acompletion_partial", new_callable=AsyncMock) as mock:
            mock.side_effect = mock_acompletion

            # Run 3 requests concurrently
            tasks = [provider.complete_async(messages) for _ in range(3)]
            responses = await asyncio.gather(*tasks)

            assert len(responses) == 3
            for i, response in enumerate(responses):
                assert f"Response {i}" in response.content

    @pytest.mark.asyncio
    async def test_async_stream_basic(self):
        """Test basic async streaming."""
        config = LLMConfig(model="gpt-4", api_key=SecretStr("test-key"))
        provider = LLMProvider.from_config(config)

        messages = [Message(role=MessageRole.USER, content="Count to 3")]

        # Mock streaming response
        async def mock_stream():
            for _i, word in enumerate(["1", " 2", " 3"]):
                mock_chunk = MagicMock()
                mock_chunk.choices = [MagicMock()]
                mock_chunk.choices[0].delta = MagicMock()
                mock_chunk.choices[0].delta.content = word
                yield mock_chunk
                await asyncio.sleep(0.01)

        with patch.object(provider, "_acompletion_partial") as mock:
            mock.return_value = mock_stream()

            chunks = []
            async for chunk in provider.stream_async(messages):
                chunks.append(chunk)

            assert chunks == ["1", " 2", " 3"]

    @pytest.mark.asyncio
    async def test_async_stream_with_cancellation(self):
        """Test async streaming with cancellation."""
        config = LLMConfig(
            model="gpt-4", api_key=SecretStr("test-key"), cancellation_check_interval=0.1
        )
        provider = LLMProvider.from_config(config)

        messages = [Message(role=MessageRole.USER, content="Count to 100")]

        # Create token that will cancel after first chunk
        token = AsyncCancellationToken()

        # Mock streaming response
        async def mock_stream():
            for i in range(100):
                mock_chunk = MagicMock()
                mock_chunk.choices = [MagicMock()]
                mock_chunk.choices[0].delta = MagicMock()
                mock_chunk.choices[0].delta.content = str(i)
                yield mock_chunk
                await asyncio.sleep(0.01)

        with patch.object(provider, "_acompletion_partial") as mock:
            mock.return_value = mock_stream()

            chunks = []
            try:
                async for chunk in provider.stream_async(messages, cancellation_token=token):
                    chunks.append(chunk)
                    if len(chunks) >= 2:
                        # Cancel after receiving 2 chunks
                        token.cancel()
            except LLMCancellationError:
                pass

            # Should have received at least 2 chunks before cancellation
            assert len(chunks) >= 2
            assert len(chunks) < 100  # Should not have received all 100

    @pytest.mark.asyncio
    async def test_provider_cleanup(self):
        """Test provider cleanup cancels active tasks."""
        config = LLMConfig(model="gpt-4", api_key=SecretStr("test-key"))
        provider = LLMProvider.from_config(config)

        messages = [Message(role=MessageRole.USER, content="Hello")]

        # Mock long-running completion
        async def long_completion(*args, **kwargs):
            await asyncio.sleep(10)
            return {"choices": [{"message": {"content": "Done"}}]}

        with patch("nexus.llm.provider.litellm_acompletion", new_callable=AsyncMock) as mock:
            mock.side_effect = long_completion

            # Start task but don't await
            task = asyncio.create_task(provider.complete_async(messages))

            # Give it time to start
            await asyncio.sleep(0.1)

            # Cleanup should cancel the task
            await provider.cleanup()

            # Task should be cancelled or done
            assert task.done() or task.cancelled()

    @pytest.mark.asyncio
    async def test_cancellation_check_interval(self):
        """Test that cancellation check interval is respected."""
        config = LLMConfig(
            model="gpt-4", api_key=SecretStr("test-key"), cancellation_check_interval=0.5
        )

        assert config.cancellation_check_interval == 0.5

    @pytest.mark.asyncio
    async def test_async_complete_with_tools(self):
        """Test async completion with tool/function calling."""
        config = LLMConfig(model="gpt-4", api_key=SecretStr("test-key"))
        provider = LLMProvider.from_config(config)

        messages = [Message(role=MessageRole.USER, content="What's 2+2?")]

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "calculate",
                    "description": "Calculate a math expression",
                    "parameters": {
                        "type": "object",
                        "properties": {"expression": {"type": "string"}},
                        "required": ["expression"],
                    },
                },
            }
        ]

        mock_response = {
            "id": "test-id",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "calculate",
                                    "arguments": '{"expression": "2+2"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

        with patch.object(provider, "_acompletion_partial", new_callable=AsyncMock) as mock:
            mock.return_value = mock_response

            response = await provider.complete_async(messages, tools=tools)

            assert response.tool_calls is not None
            assert len(response.tool_calls) > 0

    @pytest.mark.skip(reason="Requires actual API key and makes real API calls")
    @pytest.mark.asyncio
    async def test_real_async_completion(self):
        """Test real async completion (requires API key)."""
        import os

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            pytest.skip("ANTHROPIC_API_KEY not set")

        config = LLMConfig(
            model="claude-sonnet-4-20250514",
            api_key=SecretStr(api_key),
        )
        provider = LLMProvider.from_config(config)

        messages = [Message(role=MessageRole.USER, content="Say 'test successful' in two words.")]

        response = await provider.complete_async(messages)

        assert response.content is not None
        assert len(response.content) > 0
        assert response.cost >= 0

        # Cleanup
        await provider.cleanup()

    @pytest.mark.skip(reason="Requires actual API key and makes real API calls")
    @pytest.mark.asyncio
    async def test_real_async_streaming(self):
        """Test real async streaming (requires API key)."""
        import os

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            pytest.skip("ANTHROPIC_API_KEY not set")

        config = LLMConfig(
            model="claude-sonnet-4-20250514",
            api_key=SecretStr(api_key),
        )
        provider = LLMProvider.from_config(config)

        messages = [Message(role=MessageRole.USER, content="Count from 1 to 3.")]

        chunks = []
        async for chunk in provider.stream_async(messages):
            chunks.append(chunk)

        assert len(chunks) > 0
        full_response = "".join(chunks)
        assert len(full_response) > 0

        # Cleanup
        await provider.cleanup()

    @pytest.mark.skip(reason="Requires actual API key and makes real API calls")
    @pytest.mark.asyncio
    async def test_real_async_concurrent_requests(self):
        """Test real concurrent async requests (requires API key)."""
        import os

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            pytest.skip("ANTHROPIC_API_KEY not set")

        config = LLMConfig(
            model="claude-sonnet-4-20250514",
            api_key=SecretStr(api_key),
        )
        provider = LLMProvider.from_config(config)

        # Create 3 different requests
        message_sets = [
            [Message(role=MessageRole.USER, content="Say 'one'")],
            [Message(role=MessageRole.USER, content="Say 'two'")],
            [Message(role=MessageRole.USER, content="Say 'three'")],
        ]

        # Run concurrently
        tasks = [provider.complete_async(messages) for messages in message_sets]
        responses = await asyncio.gather(*tasks)

        assert len(responses) == 3
        for response in responses:
            assert response.content is not None
            assert len(response.content) > 0

        # Cleanup
        await provider.cleanup()


class TestShutdownHandling:
    """Tests for shutdown flag handling."""

    def test_should_continue(self):
        """Test should_continue function."""
        reset_shutdown_flag()
        assert should_continue()

        request_shutdown()
        assert not should_continue()

        reset_shutdown_flag()
        assert should_continue()

    def test_reset_shutdown_flag(self):
        """Test resetting shutdown flag."""
        request_shutdown()
        assert not should_continue()

        reset_shutdown_flag()
        assert should_continue()
