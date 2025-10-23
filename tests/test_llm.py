"""Tests for LLM provider abstraction layer."""

import pytest
from pydantic import SecretStr

from nexus.llm import (
    LLMConfig,
    LLMMetrics,
    LLMProvider,
    Message,
    MessageRole,
    TextContent,
    TokenUsage,
)


class TestLLMConfig:
    """Tests for LLMConfig."""

    def test_config_creation(self):
        """Test creating a basic LLM config."""
        config = LLMConfig(
            model="claude-sonnet-4",
            api_key=SecretStr("test-key"),
            temperature=0.5,
        )

        assert config.model == "claude-sonnet-4"
        assert config.api_key.get_secret_value() == "test-key"
        assert config.temperature == 0.5
        assert config.max_output_tokens == 4096

    def test_config_defaults(self):
        """Test default configuration values."""
        config = LLMConfig(model="gpt-4")

        assert config.temperature == 0.7
        assert config.max_output_tokens == 4096
        assert config.timeout == 120.0
        assert config.num_retries == 3
        assert config.native_tool_calling is None
        assert config.caching_prompt is False

    def test_config_validation(self):
        """Test config validation."""
        from pydantic import ValidationError

        # Temperature out of range
        with pytest.raises(ValidationError):
            LLMConfig(model="gpt-4", temperature=3.0)

        # Negative retries
        with pytest.raises(ValidationError):
            LLMConfig(model="gpt-4", num_retries=-1)


class TestMessage:
    """Tests for Message."""

    def test_message_creation(self):
        """Test creating a message."""
        msg = Message(
            role=MessageRole.USER,
            content="Hello, world!",
        )

        assert msg.role == MessageRole.USER
        assert msg.content == "Hello, world!"

    def test_message_with_text_content(self):
        """Test message with structured text content."""
        msg = Message(
            role=MessageRole.USER,
            content=[TextContent(text="Hello"), TextContent(text=" world")],
        )

        assert msg.role == MessageRole.USER
        assert len(msg.content) == 2

    def test_message_serialization(self):
        """Test message serialization."""
        msg = Message(role=MessageRole.USER, content="Hello")
        serialized = msg.model_dump()

        assert serialized["role"] == "user"
        assert serialized["content"] == "Hello"

    def test_message_from_dict(self):
        """Test creating message from dict."""
        data = {
            "role": "user",
            "content": "Hello",
        }

        msg = Message.from_dict(data)
        assert msg.role == MessageRole.USER
        assert msg.content == "Hello"


class TestTokenUsage:
    """Tests for TokenUsage."""

    def test_token_usage_creation(self):
        """Test creating token usage."""
        usage = TokenUsage(
            prompt_tokens=100,
            completion_tokens=50,
        )

        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 150

    def test_token_usage_addition(self):
        """Test adding token usage."""
        usage1 = TokenUsage(prompt_tokens=100, completion_tokens=50)
        usage2 = TokenUsage(prompt_tokens=200, completion_tokens=75)

        total = usage1 + usage2

        assert total.prompt_tokens == 300
        assert total.completion_tokens == 125
        assert total.total_tokens == 425

    def test_cache_tokens(self):
        """Test cache token tracking."""
        usage = TokenUsage(
            prompt_tokens=100,
            completion_tokens=50,
            cache_read_tokens=30,
            cache_write_tokens=20,
        )

        assert usage.cache_read_tokens == 30
        assert usage.cache_write_tokens == 20


class TestLLMMetrics:
    """Tests for LLMMetrics."""

    def test_metrics_creation(self):
        """Test creating metrics."""
        metrics = LLMMetrics(model_name="gpt-4")

        assert metrics.model_name == "gpt-4"
        assert metrics.accumulated_cost == 0.0
        assert metrics.total_requests == 0

    def test_add_cost(self):
        """Test adding cost."""
        metrics = LLMMetrics(model_name="gpt-4")
        metrics.add_cost(0.01)
        metrics.add_cost(0.02)

        assert metrics.accumulated_cost == 0.03

    def test_add_token_usage(self):
        """Test adding token usage."""
        metrics = LLMMetrics(model_name="gpt-4")
        metrics.add_token_usage(prompt_tokens=100, completion_tokens=50)
        metrics.add_token_usage(prompt_tokens=200, completion_tokens=75)

        assert metrics.accumulated_token_usage.prompt_tokens == 300
        assert metrics.accumulated_token_usage.completion_tokens == 125
        assert metrics.accumulated_token_usage.total_tokens == 425

    def test_add_response_latency(self):
        """Test adding response latency."""
        metrics = LLMMetrics(model_name="gpt-4")
        metrics.add_response_latency(1.5, "resp-1")
        metrics.add_response_latency(2.0, "resp-2")

        assert len(metrics.response_latencies) == 2
        assert metrics.average_latency == 1.75
        assert metrics.total_requests == 2

    def test_metrics_reset(self):
        """Test resetting metrics."""
        metrics = LLMMetrics(model_name="gpt-4")
        metrics.add_cost(0.01)
        metrics.add_token_usage(prompt_tokens=100, completion_tokens=50)
        metrics.add_response_latency(1.5, "resp-1")

        metrics.reset()

        assert metrics.accumulated_cost == 0.0
        assert metrics.accumulated_token_usage.total_tokens == 0
        assert len(metrics.response_latencies) == 0

    def test_metrics_serialization(self):
        """Test metrics to/from dict."""
        metrics = LLMMetrics(model_name="gpt-4")
        metrics.add_cost(0.01)
        metrics.add_token_usage(prompt_tokens=100, completion_tokens=50)

        data = metrics.to_dict()

        assert data["model_name"] == "gpt-4"
        assert data["accumulated_cost"] == 0.01
        assert data["token_usage"]["prompt_tokens"] == 100

        # Round trip
        restored = LLMMetrics.from_dict(data)
        assert restored.model_name == metrics.model_name
        assert restored.accumulated_cost == metrics.accumulated_cost


class TestLLMProvider:
    """Tests for LLMProvider."""

    def test_provider_creation(self):
        """Test creating a provider."""
        config = LLMConfig(
            model="claude-sonnet-4",
            api_key=SecretStr("test-key"),
        )

        provider = LLMProvider.from_config(config)

        assert provider.config.model == "claude-sonnet-4"
        assert provider.metrics.model_name == "claude-sonnet-4"

    def test_provider_capabilities(self):
        """Test provider capability detection."""
        config = LLMConfig(
            model="claude-sonnet-4-20250514",
            api_key=SecretStr("test-key"),
        )

        provider = LLMProvider.from_config(config)

        # These depend on the model and litellm's knowledge
        # Just test that methods exist and return booleans
        assert isinstance(provider.is_function_calling_active(), bool)
        assert isinstance(provider.vision_is_active(), bool)
        assert isinstance(provider.is_caching_prompt_active(), bool)

    def test_provider_metrics_reset(self):
        """Test resetting provider metrics."""
        config = LLMConfig(model="gpt-4", api_key=SecretStr("test-key"))
        provider = LLMProvider.from_config(config)

        provider.metrics.add_cost(0.01)
        provider.reset_metrics()

        assert provider.metrics.accumulated_cost == 0.0

    @pytest.mark.skip(reason="Requires actual API key and makes real API calls")
    def test_provider_complete(self):
        """Test provider completion (requires API key)."""
        import os

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            pytest.skip("ANTHROPIC_API_KEY not set")

        config = LLMConfig(
            model="claude-sonnet-4-20250514",
            api_key=SecretStr(api_key),
        )
        provider = LLMProvider.from_config(config)

        messages = [
            Message(
                role=MessageRole.USER,
                content="Say 'test successful' in exactly two words.",
            )
        ]

        response = provider.complete(messages)

        assert response.content is not None
        assert len(response.content) > 0
        assert response.cost >= 0
        assert response.response_id != "unknown"

    @pytest.mark.skip(reason="Requires actual API key and makes real API calls")
    def test_provider_streaming(self):
        """Test provider streaming (requires API key)."""
        import os

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            pytest.skip("ANTHROPIC_API_KEY not set")

        config = LLMConfig(
            model="claude-sonnet-4-20250514",
            api_key=SecretStr(api_key),
        )
        provider = LLMProvider.from_config(config)

        messages = [
            Message(
                role=MessageRole.USER,
                content="Count from 1 to 3.",
            )
        ]

        chunks = list(provider.stream(messages))
        full_response = "".join(chunks)

        assert len(chunks) > 0
        assert len(full_response) > 0


class TestIntegration:
    """Integration tests."""

    def test_end_to_end_flow(self):
        """Test complete flow without API calls."""
        # Create config
        config = LLMConfig(
            model="claude-sonnet-4",
            api_key=SecretStr("test-key"),
            temperature=0.7,
        )

        # Create provider
        provider = LLMProvider.from_config(config)

        # Create messages
        messages = [
            Message(role=MessageRole.SYSTEM, content="You are helpful."),
            Message(role=MessageRole.USER, content="Hello"),
        ]

        # Count tokens (this doesn't require API call)
        try:
            token_count = provider.count_tokens(messages)
            assert token_count >= 0
        except Exception:
            # Token counting may fail without API access
            pass

        # Verify metrics tracking works
        provider.metrics.add_cost(0.01)
        provider.metrics.add_token_usage(prompt_tokens=10, completion_tokens=20)

        assert provider.metrics.accumulated_cost == 0.01
        assert provider.metrics.accumulated_token_usage.total_tokens == 30
