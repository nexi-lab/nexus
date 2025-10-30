"""Tests for LLM configuration."""

import pytest
from pydantic import SecretStr, ValidationError

from nexus.llm.config import LLMConfig


class TestLLMConfig:
    """Test LLMConfig class."""

    def test_config_with_defaults(self):
        """Test config creation with default values."""
        config = LLMConfig(model="claude-sonnet-4")
        assert config.model == "claude-sonnet-4"
        assert config.api_key is None
        assert config.base_url is None
        assert config.temperature == 0.7
        assert config.max_output_tokens == 4096
        assert config.timeout == 120.0
        assert config.num_retries == 3

    def test_config_with_custom_values(self):
        """Test config with custom values."""
        config = LLMConfig(
            model="gpt-4o",
            api_key=SecretStr("test-key"),
            temperature=0.5,
            max_output_tokens=8192,
            timeout=60.0,
            num_retries=5,
        )
        assert config.model == "gpt-4o"
        assert config.api_key == SecretStr("test-key")
        assert config.temperature == 0.5
        assert config.max_output_tokens == 8192
        assert config.timeout == 60.0
        assert config.num_retries == 5

    def test_config_api_key_secret_str(self):
        """Test that API key is stored as SecretStr."""
        config = LLMConfig(model="test-model", api_key=SecretStr("secret"))
        assert isinstance(config.api_key, SecretStr)
        assert config.api_key.get_secret_value() == "secret"

    def test_config_temperature_validation(self):
        """Test temperature validation (0.0 to 2.0)."""
        # Valid temperatures
        config = LLMConfig(model="test", temperature=0.0)
        assert config.temperature == 0.0

        config = LLMConfig(model="test", temperature=2.0)
        assert config.temperature == 2.0

        # Invalid temperatures
        with pytest.raises(ValidationError):
            LLMConfig(model="test", temperature=-0.1)

        with pytest.raises(ValidationError):
            LLMConfig(model="test", temperature=2.1)

    def test_config_top_p_validation(self):
        """Test top_p validation (0.0 to 1.0)."""
        # Valid top_p
        config = LLMConfig(model="test", top_p=0.0)
        assert config.top_p == 0.0

        config = LLMConfig(model="test", top_p=1.0)
        assert config.top_p == 1.0

        # Invalid top_p
        with pytest.raises(ValidationError):
            LLMConfig(model="test", top_p=-0.1)

        with pytest.raises(ValidationError):
            LLMConfig(model="test", top_p=1.1)

    def test_config_retry_settings(self):
        """Test retry configuration."""
        config = LLMConfig(
            model="test",
            num_retries=5,
            retry_min_wait=2.0,
            retry_max_wait=20.0,
            retry_multiplier=3.0,
        )
        assert config.num_retries == 5
        assert config.retry_min_wait == 2.0
        assert config.retry_max_wait == 20.0
        assert config.retry_multiplier == 3.0

    def test_config_feature_flags(self):
        """Test feature flag settings."""
        config = LLMConfig(
            model="test",
            native_tool_calling=True,
            caching_prompt=True,
            disable_vision=True,
            drop_params=False,
            modify_params=False,
        )
        assert config.native_tool_calling is True
        assert config.caching_prompt is True
        assert config.disable_vision is True
        assert config.drop_params is False
        assert config.modify_params is False

    def test_config_reasoning_effort(self):
        """Test reasoning effort setting."""
        for effort in ["low", "medium", "high"]:
            config = LLMConfig(model="test", reasoning_effort=effort)
            assert config.reasoning_effort == effort

    def test_config_cost_tracking(self):
        """Test cost tracking settings."""
        config = LLMConfig(
            model="test",
            input_cost_per_token=0.001,
            output_cost_per_token=0.002,
        )
        assert config.input_cost_per_token == 0.001
        assert config.output_cost_per_token == 0.002

    def test_config_logging_settings(self):
        """Test logging configuration."""
        config = LLMConfig(
            model="test",
            log_completions=True,
            log_completions_folder="/tmp/logs",
        )
        assert config.log_completions is True
        assert config.log_completions_folder == "/tmp/logs"

    def test_config_cancellation_check_interval(self):
        """Test cancellation check interval."""
        config = LLMConfig(model="test", cancellation_check_interval=0.5)
        assert config.cancellation_check_interval == 0.5

        # Test minimum value (0.1)
        config = LLMConfig(model="test", cancellation_check_interval=0.1)
        assert config.cancellation_check_interval == 0.1

        # Test invalid value below minimum
        with pytest.raises(ValidationError):
            LLMConfig(model="test", cancellation_check_interval=0.05)

    def test_config_azure_specific_fields(self):
        """Test Azure-specific configuration."""
        config = LLMConfig(
            model="gpt-4",
            base_url="https://myresource.openai.azure.com",
            api_version="2024-02-15-preview",
        )
        assert config.base_url == "https://myresource.openai.azure.com"
        assert config.api_version == "2024-02-15-preview"

    def test_config_custom_provider(self):
        """Test custom LLM provider."""
        config = LLMConfig(
            model="custom-model",
            custom_llm_provider="my-provider",
            base_url="https://api.myprovider.com",
        )
        assert config.custom_llm_provider == "my-provider"
        assert config.base_url == "https://api.myprovider.com"

    def test_config_validate_assignment(self):
        """Test that assignment validation is enabled."""
        config = LLMConfig(model="test", temperature=0.5)

        # Valid assignment
        config.temperature = 1.0
        assert config.temperature == 1.0

        # Invalid assignment should raise error
        with pytest.raises(ValidationError):
            config.temperature = 3.0

    def test_config_all_fields(self):
        """Test config with all fields set."""
        config = LLMConfig(
            model="claude-sonnet-4",
            api_key=SecretStr("key"),
            base_url="https://api.anthropic.com",
            api_version="2024-01",
            custom_llm_provider="anthropic",
            temperature=0.8,
            max_output_tokens=2048,
            max_input_tokens=100000,
            top_p=0.9,
            seed=42,
            timeout=90.0,
            num_retries=2,
            retry_min_wait=3.0,
            retry_max_wait=15.0,
            retry_multiplier=2.5,
            native_tool_calling=True,
            caching_prompt=True,
            disable_vision=False,
            drop_params=True,
            modify_params=True,
            custom_tokenizer="custom",
            reasoning_effort="high",
            input_cost_per_token=0.003,
            output_cost_per_token=0.015,
            log_completions=True,
            log_completions_folder="/var/logs",
            cancellation_check_interval=2.0,
        )
        assert config.model == "claude-sonnet-4"
        assert config.seed == 42
        assert config.max_input_tokens == 100000
        assert config.custom_tokenizer == "custom"
