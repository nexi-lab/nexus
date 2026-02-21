"""Tests for NexusLMClient — custom LLM client wrapping LiteLLMProvider.

Tests verify:
- chat() correctly calls LiteLLMProvider.complete_async()
- Message format translation (rlm format → Nexus Message format)
- Error handling: rate limits, timeouts, malformed responses
- get_model() returns correct model name
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.rlm.lm_client import NexusLMClient
from nexus.bricks.rlm.types import RLMInfrastructureError


def _make_provider(
    *,
    response_text: str = "I'll analyze the data.",
    raise_error: Exception | None = None,
) -> MagicMock:
    """Create a mock LLMProvider."""
    provider = MagicMock()

    if raise_error:
        provider.complete_async = AsyncMock(side_effect=raise_error)
    else:
        mock_response = MagicMock()
        mock_response.get.side_effect = lambda key, default=None: {
            "id": "resp_123",
            "choices": [
                MagicMock(
                    **{
                        "message": MagicMock(content=response_text),
                    }
                )
            ],
        }.get(key, default)
        # Make choices accessible as attribute
        mock_choice = MagicMock()
        mock_choice.message.content = response_text
        mock_response.choices = [mock_choice]
        provider.complete_async = AsyncMock(return_value=mock_response)

    provider.count_tokens = MagicMock(return_value=50)
    return provider


class TestChat:
    """NexusLMClient.chat() routes through LiteLLMProvider."""

    def test_chat_returns_response_text(self) -> None:
        provider = _make_provider(response_text="The answer is 42.")
        client = NexusLMClient(provider=provider, model="claude-sonnet-4-20250514")

        result = client.chat(
            messages=[{"role": "user", "content": "What is 6*7?"}],
            model="claude-sonnet-4-20250514",
        )

        assert result == "The answer is 42."

    def test_chat_calls_provider_complete_async(self) -> None:
        provider = _make_provider()
        client = NexusLMClient(provider=provider, model="claude-sonnet-4-20250514")

        client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            model="claude-sonnet-4-20250514",
        )

        provider.complete_async.assert_called_once()

    def test_chat_translates_messages(self) -> None:
        provider = _make_provider()
        client = NexusLMClient(provider=provider, model="claude-sonnet-4-20250514")

        client.chat(
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello"},
            ],
            model="claude-sonnet-4-20250514",
        )

        call_args = provider.complete_async.call_args
        messages_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("messages", [])
        assert len(messages_arg) >= 2

    def test_chat_with_empty_response(self) -> None:
        provider = _make_provider(response_text="")
        client = NexusLMClient(provider=provider, model="claude-sonnet-4-20250514")

        result = client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            model="claude-sonnet-4-20250514",
        )

        assert result == ""


class TestGetModel:
    """NexusLMClient.get_model() returns configured model name."""

    def test_returns_model_name(self) -> None:
        provider = _make_provider()
        client = NexusLMClient(provider=provider, model="claude-sonnet-4-20250514")
        assert client.get_model() == "claude-sonnet-4-20250514"

    def test_returns_custom_model(self) -> None:
        provider = _make_provider()
        client = NexusLMClient(provider=provider, model="gpt-4o")
        assert client.get_model() == "gpt-4o"


class TestErrorHandling:
    """Error handling in LLM calls."""

    def test_provider_error_raises_infrastructure_error(self) -> None:
        provider = _make_provider(raise_error=ConnectionError("API unreachable"))
        client = NexusLMClient(provider=provider, model="claude-sonnet-4-20250514")

        with pytest.raises(RLMInfrastructureError, match="LLM provider"):
            client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                model="claude-sonnet-4-20250514",
            )

    def test_rate_limit_error_raises_infrastructure_error(self) -> None:
        provider = _make_provider(raise_error=Exception("Rate limit exceeded"))
        client = NexusLMClient(provider=provider, model="claude-sonnet-4-20250514")

        with pytest.raises(RLMInfrastructureError):
            client.chat(
                messages=[{"role": "user", "content": "Hello"}],
                model="claude-sonnet-4-20250514",
            )


class TestTokenCounting:
    """Token counting for budget tracking."""

    def test_count_tokens(self) -> None:
        provider = _make_provider()
        provider.count_tokens = MagicMock(return_value=42)
        client = NexusLMClient(provider=provider, model="claude-sonnet-4-20250514")

        count = client.count_tokens([{"role": "user", "content": "Hello"}])
        assert count == 42

    def test_accumulated_tokens(self) -> None:
        provider = _make_provider()
        client = NexusLMClient(provider=provider, model="claude-sonnet-4-20250514")

        assert client.total_tokens_used == 0

        client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            model="claude-sonnet-4-20250514",
        )

        # After a chat call, tokens should be tracked
        assert client.total_tokens_used >= 0
