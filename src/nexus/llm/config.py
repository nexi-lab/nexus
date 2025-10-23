"""LLM configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class LLMConfig(BaseModel):
    """Configuration for an LLM provider."""

    # Model configuration
    model: str = Field(description="Model name (e.g., claude-sonnet-4, gpt-4o)")
    api_key: SecretStr | None = Field(default=None, description="API key for the provider")
    base_url: str | None = Field(default=None, description="Base URL for the API endpoint")
    api_version: str | None = Field(default=None, description="API version (e.g., for Azure)")
    custom_llm_provider: str | None = Field(default=None, description="Custom LLM provider name")

    # Generation parameters
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="Sampling temperature")
    max_output_tokens: int | None = Field(default=4096, description="Maximum output tokens")
    max_input_tokens: int | None = Field(default=None, description="Maximum input tokens")
    top_p: float = Field(default=1.0, ge=0.0, le=1.0, description="Nucleus sampling parameter")
    seed: int | None = Field(default=None, description="Random seed for reproducibility")

    # Timeout and retry
    timeout: float = Field(default=120.0, description="Timeout in seconds")
    num_retries: int = Field(default=3, ge=0, description="Number of retries on failure")
    retry_min_wait: float = Field(
        default=4.0, description="Minimum wait time between retries (seconds)"
    )
    retry_max_wait: float = Field(
        default=10.0, description="Maximum wait time between retries (seconds)"
    )
    retry_multiplier: float = Field(default=2.0, description="Exponential backoff multiplier")

    # Feature flags
    native_tool_calling: bool | None = Field(
        default=None, description="Enable native tool/function calling"
    )
    caching_prompt: bool = Field(default=False, description="Enable prompt caching (Claude models)")
    disable_vision: bool = Field(default=False, description="Disable vision capabilities")
    drop_params: bool = Field(default=True, description="Drop unsupported parameters")
    modify_params: bool = Field(default=True, description="Allow litellm to modify parameters")

    # Advanced configuration
    custom_tokenizer: str | None = Field(default=None, description="Custom tokenizer name")
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(
        default=None, description="Reasoning effort for o1/o3 models"
    )

    # Cost tracking
    input_cost_per_token: float | None = Field(
        default=None, description="Custom input cost per token (USD)"
    )
    output_cost_per_token: float | None = Field(
        default=None, description="Custom output cost per token (USD)"
    )

    # Logging
    log_completions: bool = Field(default=False, description="Log all completions to disk")
    log_completions_folder: str | None = Field(
        default=None, description="Folder for completion logs"
    )

    # Async/cancellation configuration
    cancellation_check_interval: float = Field(
        default=1.0, ge=0.1, description="Interval in seconds to check for cancellation"
    )

    class Config:
        """Pydantic config."""

        validate_assignment = True
