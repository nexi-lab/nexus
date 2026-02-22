"""Characterization tests for LLM provider (src/nexus/llm/provider.py).

All litellm calls are mocked at the boundary to test provider behavior
without network access.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from nexus.bricks.llm.cancellation import AsyncCancellationToken
from nexus.bricks.llm.config import LLMConfig
from nexus.bricks.llm.exceptions import LLMCancellationError
from nexus.bricks.llm.provider import (
    CACHE_PROMPT_SUPPORTED_MODELS,
    LiteLLMProvider,
    LiteLLMResponse,
    LLMProvider,
)
from nexus.contracts.llm_types import Message, MessageRole, TextContent


def _make_config(**overrides: Any) -> LLMConfig:
    """Create a test LLMConfig with sensible defaults."""
    defaults: dict[str, Any] = {
        "model": "claude-sonnet-4-20250514",
        "api_key": SecretStr("test-key"),
        "temperature": 0.7,
        "max_output_tokens": 4096,
    }
    defaults.update(overrides)
    return LLMConfig(**defaults)


def _make_messages(text: str = "Hello") -> list[Message]:
    """Create a simple message list for testing."""
    return [Message(role=MessageRole.USER, content=[TextContent(text=text)])]


def _mock_model_response(
    content: str = "Test response",
    response_id: str = "resp_123",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
) -> MagicMock:
    """Create a mock ModelResponse."""
    mock = MagicMock()
    mock.get.side_effect = lambda key, default=None: {
        "id": response_id,
        "choices": [
            MagicMock(
                **{
                    "__getitem__": lambda self, k: {
                        "message": MagicMock(
                            **{
                                "get": lambda k2, d2=None: content if k2 == "content" else d2,
                            }
                        )
                    }[k]
                }
            )
        ],
        "usage": MagicMock(
            **{
                "get": lambda k, d=None: {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "prompt_tokens_details": None,
                    "model_extra": {},
                }.get(k, d),
            }
        ),
    }.get(key, default)
    mock.__getitem__ = lambda self, key: mock.get(key)
    return mock


@pytest.fixture()
def provider() -> LiteLLMProvider:
    """Create a LiteLLMProvider with mocked model info."""
    with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
        mock_litellm.get_model_info.return_value = {
            "max_input_tokens": 200000,
            "max_output_tokens": 4096,
            "supports_vision": True,
            "supports_function_calling": True,
        }
        mock_litellm.supports_function_calling.return_value = True
        mock_litellm.supports_vision.return_value = True
        p = LiteLLMProvider(_make_config())
    return p


class TestLiteLLMProviderInit:
    """Test provider initialization."""

    def test_init_model_info_with_prefix(self) -> None:
        """Test provider init with model that has provider/ prefix."""
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            # First call fails (with prefix), second succeeds (without)
            mock_litellm.get_model_info.side_effect = [
                Exception("not found"),
                {"max_input_tokens": 128000, "max_output_tokens": 4096},
            ]
            mock_litellm.supports_function_calling.return_value = True
            mock_litellm.supports_vision.return_value = True
            p = LiteLLMProvider(_make_config(model="openrouter/claude-sonnet-4-20250514"))
        assert p.model_info is not None
        assert p.config.max_input_tokens == 128000

    def test_init_sets_default_max_input_tokens(self) -> None:
        """Test that max_input_tokens defaults to 4096 when model_info unavailable."""
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.get_model_info.side_effect = Exception("not found")
            mock_litellm.supports_function_calling.return_value = False
            mock_litellm.supports_vision.return_value = False
            p = LiteLLMProvider(_make_config(model="unknown-model"))
        assert p.config.max_input_tokens == 4096


class TestFunctionCallingDetection:
    """Test model capability detection."""

    def test_function_calling_active_for_supported_model(self) -> None:
        """Test that function calling is active for models in the supported list."""
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.get_model_info.return_value = {}
            mock_litellm.supports_function_calling.return_value = True
            mock_litellm.supports_vision.return_value = False
            p = LiteLLMProvider(_make_config(model="gpt-4o"))
        assert p.is_function_calling_active() is True

    def test_function_calling_disabled_when_config_false(self) -> None:
        """Test that function calling is off when native_tool_calling=False."""
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.get_model_info.return_value = {}
            mock_litellm.supports_function_calling.return_value = True
            mock_litellm.supports_vision.return_value = False
            p = LiteLLMProvider(_make_config(model="gpt-4o", native_tool_calling=False))
        assert p.is_function_calling_active() is False


class TestComplete:
    """Test synchronous completion."""

    def test_complete_returns_response(self, provider: LiteLLMProvider) -> None:
        """Test basic sync completion returns LiteLLMResponse."""
        mock_resp = _mock_model_response()
        provider._completion_partial = MagicMock(return_value=mock_resp)

        response = provider.complete(_make_messages())

        assert isinstance(response, LiteLLMResponse)
        provider._completion_partial.assert_called_once()

    def test_complete_records_latency(self, provider: LiteLLMProvider) -> None:
        """Test that complete records latency metrics."""
        mock_resp = _mock_model_response()
        provider._completion_partial = MagicMock(return_value=mock_resp)
        provider.cost_metric_supported = False  # Simplify

        provider.complete(_make_messages())

        assert len(provider.metrics.response_latencies) == 1


class TestCompleteAsync:
    """Test async completion."""

    @pytest.mark.asyncio()
    async def test_complete_async_returns_response(self, provider: LiteLLMProvider) -> None:
        """Test basic async completion returns LiteLLMResponse."""
        mock_resp = _mock_model_response()
        provider._acompletion_partial = AsyncMock(return_value=mock_resp)
        provider.cost_metric_supported = False

        response = await provider.complete_async(_make_messages())

        assert isinstance(response, LiteLLMResponse)

    @pytest.mark.asyncio()
    async def test_cancellation_stops_request(self, provider: LiteLLMProvider) -> None:
        """Test that AsyncCancellationToken cancels in-progress request."""
        cancel_token = AsyncCancellationToken(check_shutdown=False)

        async def slow_completion(**kwargs: Any) -> None:
            await asyncio.sleep(10)

        provider._acompletion_partial = slow_completion  # type: ignore[assignment]

        # Cancel after a short delay
        async def cancel_after_delay() -> None:
            await asyncio.sleep(0.1)
            cancel_token.cancel()

        asyncio.create_task(cancel_after_delay())

        with pytest.raises(LLMCancellationError):
            await provider.complete_async(_make_messages(), cancellation_token=cancel_token)


class TestStream:
    """Test sync streaming."""

    def test_stream_yields_chunks(self, provider: LiteLLMProvider) -> None:
        """Test that sync streaming yields content chunks."""
        # Build mock chunks
        chunk1 = MagicMock()
        chunk1.choices = [MagicMock()]
        chunk1.choices[0].delta = MagicMock(content="Hello")
        chunk2 = MagicMock()
        chunk2.choices = [MagicMock()]
        chunk2.choices[0].delta = MagicMock(content=" world")

        provider._completion_partial = MagicMock(return_value=[chunk1, chunk2])

        chunks = list(provider.stream(_make_messages()))

        assert chunks == ["Hello", " world"]


class TestStreamAsync:
    """Test async streaming."""

    @pytest.mark.asyncio()
    async def test_stream_async_yields_chunks(self, provider: LiteLLMProvider) -> None:
        """Test that async streaming yields content chunks."""
        # Build mock async chunks
        chunk1 = MagicMock()
        chunk1.choices = [MagicMock()]
        chunk1.choices[0].delta = MagicMock(content="Async")
        chunk2 = MagicMock()
        chunk2.choices = [MagicMock()]
        chunk2.choices[0].delta = MagicMock(content=" response")

        async def mock_aiter() -> AsyncIterator[Any]:
            yield chunk1
            yield chunk2

        provider._acompletion_partial = AsyncMock(return_value=mock_aiter())

        result = []
        async for chunk in provider.stream_async(_make_messages()):
            result.append(chunk)

        assert result == ["Async", " response"]


class TestCountTokens:
    """Test token counting with caching."""

    def test_token_count_basic(self, provider: LiteLLMProvider) -> None:
        """Test basic token counting."""
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 42
            count = provider.count_tokens(_make_messages())
        assert count == 42

    def test_token_count_with_cache_hit(self, provider: LiteLLMProvider) -> None:
        """Test that cached token count is returned on subsequent calls."""
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 42
            msgs = _make_messages()
            # First call should invoke litellm
            count1 = provider.count_tokens(msgs)
            # Second call with same messages should use cache
            count2 = provider.count_tokens(msgs)
        assert count1 == 42
        assert count2 == 42
        # litellm.token_counter called only once (second is cached)
        assert mock_litellm.token_counter.call_count == 1

    def test_token_count_cache_eviction(self) -> None:
        """Test that cache evicts oldest entries when full."""
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.get_model_info.return_value = {}
            mock_litellm.supports_function_calling.return_value = False
            mock_litellm.supports_vision.return_value = False

            config = _make_config()
            p = LiteLLMProvider(config)
            # Replace cache with a small one to test eviction
            from cachetools import LRUCache

            p._token_count_cache = LRUCache(maxsize=2)

        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.token_counter.return_value = 10
            # Fill cache with 2 entries
            p.count_tokens(_make_messages("msg1"))
            p.count_tokens(_make_messages("msg2"))
            assert len(p._token_count_cache) == 2
            # Third entry should evict first
            p.count_tokens(_make_messages("msg3"))
            assert len(p._token_count_cache) == 2


class TestCostCalculation:
    """Test cost calculation."""

    def test_cost_calculation_primary(self, provider: LiteLLMProvider) -> None:
        """Test normal cost calculation path."""
        mock_resp = _mock_model_response()
        with patch("nexus.bricks.llm.provider.litellm_completion_cost", return_value=0.005):
            cost = provider._calculate_cost(mock_resp)
        assert cost == 0.005
        assert provider.metrics.accumulated_cost == 0.005

    def test_cost_calculation_fallback_base_model(self) -> None:
        """Test cost calculation falls back to base model name (strips provider prefix)."""
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.get_model_info.return_value = {}
            mock_litellm.supports_function_calling.return_value = False
            mock_litellm.supports_vision.return_value = False
            p = LiteLLMProvider(_make_config(model="openrouter/anthropic/claude-sonnet-4"))

        mock_resp = _mock_model_response()

        with patch("nexus.bricks.llm.provider.litellm_completion_cost") as mock_cost:
            # First call fails, fallback succeeds
            mock_cost.side_effect = [KeyError("not found"), 0.003]
            cost = p._calculate_cost(mock_resp)

        assert cost == 0.003

    def test_cost_calculation_disables_on_failure(self, provider: LiteLLMProvider) -> None:
        """Test that cost tracking is disabled when calculation fails."""
        mock_resp = _mock_model_response()
        with patch(
            "nexus.bricks.llm.provider.litellm_completion_cost", side_effect=KeyError("fail")
        ):
            cost = provider._calculate_cost(mock_resp)
        assert cost == 0.0
        assert provider.cost_metric_supported is False


class TestFormatMessages:
    """Test message formatting."""

    def test_format_messages_basic(self, provider: LiteLLMProvider) -> None:
        """Test basic message formatting."""
        messages = _make_messages("Test")
        formatted = provider._format_messages(messages)
        assert isinstance(formatted, list)
        assert len(formatted) == 1
        assert formatted[0]["role"] == "user"

    def test_format_messages_does_not_mutate_originals(self, provider: LiteLLMProvider) -> None:
        """Test that format_messages does not mutate the original message objects."""
        messages = _make_messages("Test")
        provider._format_messages(messages)
        # Original messages should remain unmutated (Issue #1521: fix mutation)
        for msg in messages:
            assert msg.cache_enabled is False
            assert msg.vision_enabled is False
            assert msg.function_calling_enabled is False


class TestCachingPrompt:
    """Test prompt caching behavior."""

    def test_caching_prompt_active_for_supported_model(self) -> None:
        """Test caching is active for models in the supported list."""
        model = CACHE_PROMPT_SUPPORTED_MODELS[0]
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.get_model_info.return_value = {}
            mock_litellm.supports_function_calling.return_value = False
            mock_litellm.supports_vision.return_value = False
            p = LiteLLMProvider(_make_config(model=model, caching_prompt=True))
        assert p.is_caching_prompt_active() is True

    def test_caching_prompt_inactive_when_disabled(self) -> None:
        """Test caching is inactive when config disables it."""
        model = CACHE_PROMPT_SUPPORTED_MODELS[0]
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.get_model_info.return_value = {}
            mock_litellm.supports_function_calling.return_value = False
            mock_litellm.supports_vision.return_value = False
            p = LiteLLMProvider(_make_config(model=model, caching_prompt=False))
        assert p.is_caching_prompt_active() is False


class TestVision:
    """Test vision support."""

    def test_vision_active(self, provider: LiteLLMProvider) -> None:
        """Test vision is active when model supports it and not disabled."""
        assert provider.vision_is_active() is True

    def test_vision_disabled_by_config(self) -> None:
        """Test vision can be disabled via config."""
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.get_model_info.return_value = {"supports_vision": True}
            mock_litellm.supports_function_calling.return_value = False
            mock_litellm.supports_vision.return_value = True
            p = LiteLLMProvider(_make_config(disable_vision=True))
        assert p.vision_is_active() is False


class TestResetMetrics:
    """Test metrics reset."""

    def test_reset_clears_metrics_and_cache(self, provider: LiteLLMProvider) -> None:
        """Test that reset_metrics clears both metrics and token cache."""
        provider.metrics.add_cost(0.01)
        provider._token_count_cache["key"] = 42
        provider.reset_metrics()
        assert provider.metrics.accumulated_cost == 0.0
        assert len(provider._token_count_cache) == 0


class TestCleanup:
    """Test async cleanup."""

    @pytest.mark.asyncio()
    async def test_cleanup_cancels_tasks(self, provider: LiteLLMProvider) -> None:
        """Test that cleanup cancels active async tasks."""
        # Create a mock task
        task = asyncio.create_task(asyncio.sleep(100))
        provider._active_tasks.add(task)

        await provider.cleanup()

        assert task.cancelled() or task.done()
        assert len(provider._active_tasks) == 0


class TestLiteLLMResponse:
    """Test LiteLLMResponse wrapper."""

    def test_content_extraction(self) -> None:
        """Test content is extracted from response."""
        mock_resp = MagicMock()
        mock_resp.get.side_effect = lambda k, d=None: {
            "choices": [
                MagicMock(
                    **{
                        "__getitem__": lambda s, k2: {
                            "message": MagicMock(
                                **{
                                    "get": lambda k3, d3=None: (
                                        "Test content" if k3 == "content" else d3
                                    )
                                }
                            )
                        }[k2]
                    }
                )
            ],
            "id": "resp_1",
        }.get(k, d)
        mock_resp.__getitem__ = lambda self, key: mock_resp.get(key)
        resp = LiteLLMResponse(mock_resp, 0.005)
        assert resp.content == "Test content"
        assert resp.cost == 0.005
        assert resp.response_id == "resp_1"

    def test_content_none_when_no_choices(self) -> None:
        """Test content is None when there are no choices."""
        mock_resp = MagicMock()
        mock_resp.get.side_effect = lambda k, d=None: {
            "choices": [],
            "id": "resp_2",
        }.get(k, d)
        mock_resp.__getitem__ = lambda self, key: mock_resp.get(key)
        resp = LiteLLMResponse(mock_resp, 0.0)
        assert resp.content is None

    def test_raw_response(self) -> None:
        """Test raw_response returns the original response."""
        mock_resp = MagicMock()
        resp = LiteLLMResponse(mock_resp, 0.0)
        assert resp.raw_response is mock_resp


class TestFromConfig:
    """Test factory method."""

    def test_from_config_returns_litellm_provider(self) -> None:
        """Test that from_config creates a LiteLLMProvider."""
        with patch("nexus.bricks.llm.provider.litellm") as mock_litellm:
            mock_litellm.get_model_info.return_value = {}
            mock_litellm.supports_function_calling.return_value = False
            mock_litellm.supports_vision.return_value = False
            p = LLMProvider.from_config(_make_config())
        assert isinstance(p, LiteLLMProvider)
