"""Tests for LLMConfig (src/nexus/llm/config.py)."""


import pytest
from pydantic import SecretStr, ValidationError

from nexus.llm.config import LLMConfig


class TestLLMConfig:
    """Tests for LLMConfig dataclass defaults, validation, and field behavior."""

    def test_config_with_defaults(self) -> None:
        """Create LLMConfig with only required model field and verify all defaults."""
        config = LLMConfig(model="claude-sonnet-4")

        assert config.model == "claude-sonnet-4"
        assert config.temperature == 0.7
        assert config.max_output_tokens == 4096
        assert config.max_input_tokens is None
        assert config.top_p == 1.0
        assert config.seed is None
        assert config.timeout == 120.0
        assert config.num_retries == 3
        assert config.retry_min_wait == 4.0
        assert config.retry_max_wait == 10.0
        assert config.retry_multiplier == 2.0
        assert config.api_key is None
        assert config.base_url is None
        assert config.api_version is None
        assert config.custom_llm_provider is None
        assert config.native_tool_calling is None
        assert config.caching_prompt is False
        assert config.disable_vision is False
        assert config.drop_params is True
        assert config.modify_params is True
        assert config.custom_tokenizer is None
        assert config.reasoning_effort is None
        assert config.input_cost_per_token is None
        assert config.output_cost_per_token is None
        assert config.log_completions is False
        assert config.log_completions_folder is None
        assert config.cancellation_check_interval == 1.0

    def test_config_with_custom_values(self) -> None:
        """Create LLMConfig with custom values and verify they are stored correctly."""
        config = LLMConfig(
            model="gpt-4o",
            api_key=SecretStr("test-key"),
            temperature=0.5,
            max_output_tokens=2048,
            top_p=0.9,
            timeout=60.0,
        )

        assert config.model == "gpt-4o"
        assert config.api_key is not None
        assert config.api_key.get_secret_value() == "test-key"
        assert config.temperature == 0.5
        assert config.max_output_tokens == 2048
        assert config.top_p == 0.9
        assert config.timeout == 60.0

    def test_config_api_key_secret_str(self) -> None:
        """Verify api_key is stored as SecretStr and not exposed in repr."""
        config = LLMConfig(model="claude-sonnet-4", api_key=SecretStr("super-secret-key"))

        assert isinstance(config.api_key, SecretStr)
        assert config.api_key.get_secret_value() == "super-secret-key"
        # SecretStr should mask the value in string representation
        assert "super-secret-key" not in str(config.api_key)

    def test_config_temperature_validation(self) -> None:
        """Test temperature bounds: valid at 0.0 and 2.0, invalid outside."""
        # Valid boundary values
        config_low = LLMConfig(model="test", temperature=0.0)
        assert config_low.temperature == 0.0

        config_high = LLMConfig(model="test", temperature=2.0)
        assert config_high.temperature == 2.0

        config_mid = LLMConfig(model="test", temperature=1.0)
        assert config_mid.temperature == 1.0

        # Invalid: below 0.0
        with pytest.raises(ValidationError):
            LLMConfig(model="test", temperature=-0.1)

        # Invalid: above 2.0
        with pytest.raises(ValidationError):
            LLMConfig(model="test", temperature=2.1)

    def test_config_top_p_validation(self) -> None:
        """Test top_p bounds: valid at 0.0 and 1.0, invalid outside."""
        config_low = LLMConfig(model="test", top_p=0.0)
        assert config_low.top_p == 0.0

        config_high = LLMConfig(model="test", top_p=1.0)
        assert config_high.top_p == 1.0

        with pytest.raises(ValidationError):
            LLMConfig(model="test", top_p=-0.1)

        with pytest.raises(ValidationError):
            LLMConfig(model="test", top_p=1.1)

    def test_config_retry_settings(self) -> None:
        """Test retry-related configuration fields."""
        config = LLMConfig(
            model="test",
            num_retries=5,
            retry_min_wait=2.0,
            retry_max_wait=30.0,
            retry_multiplier=3.0,
        )

        assert config.num_retries == 5
        assert config.retry_min_wait == 2.0
        assert config.retry_max_wait == 30.0
        assert config.retry_multiplier == 3.0

    def test_config_feature_flags(self) -> None:
        """Test feature flag fields with non-default values."""
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

    def test_config_reasoning_effort(self) -> None:
        """Test reasoning_effort with valid literal values."""
        for effort in ("low", "medium", "high"):
            config = LLMConfig(model="o1-mini", reasoning_effort=effort)
            assert config.reasoning_effort == effort

        # None is also valid (default)
        config_none = LLMConfig(model="test")
        assert config_none.reasoning_effort is None

    def test_config_cost_tracking(self) -> None:
        """Test custom cost-per-token fields."""
        config = LLMConfig(
            model="test",
            input_cost_per_token=0.00001,
            output_cost_per_token=0.00003,
        )

        assert config.input_cost_per_token == 0.00001
        assert config.output_cost_per_token == 0.00003

    def test_config_logging_settings(self) -> None:
        """Test log_completions and log_completions_folder."""
        config = LLMConfig(
            model="test",
            log_completions=True,
            log_completions_folder="/tmp/logs",
        )

        assert config.log_completions is True
        assert config.log_completions_folder == "/tmp/logs"

    def test_config_cancellation_check_interval(self) -> None:
        """Test cancellation_check_interval with a custom value."""
        config = LLMConfig(model="test", cancellation_check_interval=2.0)
        assert config.cancellation_check_interval == 2.0

    def test_config_azure_specific_fields(self) -> None:
        """Test Azure-specific configuration: base_url and api_version."""
        config = LLMConfig(
            model="gpt-4",
            base_url="https://myresource.openai.azure.com",
            api_version="2024-02-15-preview",
        )

        assert config.model == "gpt-4"
        assert config.base_url == "https://myresource.openai.azure.com"
        assert config.api_version == "2024-02-15-preview"

    def test_config_custom_provider(self) -> None:
        """Test custom_llm_provider with a custom base_url."""
        config = LLMConfig(
            model="my-model",
            custom_llm_provider="my-provider",
            base_url="https://my-provider.example.com/v1",
        )

        assert config.custom_llm_provider == "my-provider"
        assert config.base_url == "https://my-provider.example.com/v1"

    def test_config_validate_assignment(self) -> None:
        """Test that LLMConfig has validate_assignment=True so field changes are validated."""
        config = LLMConfig(model="test")

        # Valid assignment
        config.temperature = 1.5
        assert config.temperature == 1.5

        # Invalid assignment should raise ValidationError
        with pytest.raises(ValidationError):
            config.temperature = 3.0
