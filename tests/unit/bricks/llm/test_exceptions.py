"""Tests for exception hierarchy (src/nexus/llm/exceptions.py)."""

import pytest

from nexus.bricks.llm.exceptions import (
    LLMAuthenticationError,
    LLMCancellationError,
    LLMConfigError,
    LLMCostCalculationError,
    LLMException,
    LLMInvalidRequestError,
    LLMNoResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTokenCountError,
)


class TestLLMExceptions:
    """Tests verifying the LLM exception hierarchy."""

    def test_llm_exception_is_exception(self) -> None:
        """LLMException should be a subclass of Exception."""
        exc = LLMException("test error")
        assert isinstance(exc, Exception)
        assert str(exc) == "test error"

    def test_llm_provider_error_is_llm_exception(self) -> None:
        """LLMProviderError should be both LLMProviderError and LLMException."""
        exc = LLMProviderError("provider error")
        assert isinstance(exc, LLMProviderError)
        assert isinstance(exc, LLMException)
        assert isinstance(exc, Exception)

    def test_llm_rate_limit_error(self) -> None:
        """LLMRateLimitError should inherit from LLMProviderError and LLMException."""
        exc = LLMRateLimitError("rate limited")
        assert isinstance(exc, LLMRateLimitError)
        assert isinstance(exc, LLMProviderError)
        assert isinstance(exc, LLMException)

    def test_llm_timeout_error(self) -> None:
        """LLMTimeoutError should inherit from LLMProviderError and LLMException."""
        exc = LLMTimeoutError("request timed out")
        assert isinstance(exc, LLMTimeoutError)
        assert isinstance(exc, LLMProviderError)
        assert isinstance(exc, LLMException)

    def test_llm_authentication_error(self) -> None:
        """LLMAuthenticationError should inherit from LLMProviderError and LLMException."""
        exc = LLMAuthenticationError("bad api key")
        assert isinstance(exc, LLMAuthenticationError)
        assert isinstance(exc, LLMProviderError)
        assert isinstance(exc, LLMException)

    def test_llm_invalid_request_error(self) -> None:
        """LLMInvalidRequestError should inherit from LLMProviderError and LLMException."""
        exc = LLMInvalidRequestError("invalid parameters")
        assert isinstance(exc, LLMInvalidRequestError)
        assert isinstance(exc, LLMProviderError)
        assert isinstance(exc, LLMException)

    def test_llm_no_response_error(self) -> None:
        """LLMNoResponseError should inherit from LLMProviderError and LLMException."""
        exc = LLMNoResponseError("no response received")
        assert isinstance(exc, LLMNoResponseError)
        assert isinstance(exc, LLMProviderError)
        assert isinstance(exc, LLMException)

    def test_llm_config_error(self) -> None:
        """LLMConfigError should be LLMException but NOT LLMProviderError."""
        exc = LLMConfigError("bad config")
        assert isinstance(exc, LLMConfigError)
        assert isinstance(exc, LLMException)
        assert not isinstance(exc, LLMProviderError)

    def test_llm_token_count_error(self) -> None:
        """LLMTokenCountError should be LLMException but NOT LLMProviderError."""
        exc = LLMTokenCountError("token count failed")
        assert isinstance(exc, LLMTokenCountError)
        assert isinstance(exc, LLMException)
        assert not isinstance(exc, LLMProviderError)

    def test_llm_cost_calculation_error(self) -> None:
        """LLMCostCalculationError should be LLMException but NOT LLMProviderError."""
        exc = LLMCostCalculationError("cost calc failed")
        assert isinstance(exc, LLMCostCalculationError)
        assert isinstance(exc, LLMException)
        assert not isinstance(exc, LLMProviderError)

    def test_llm_cancellation_error(self) -> None:
        """LLMCancellationError should be LLMException but NOT LLMProviderError."""
        exc = LLMCancellationError("cancelled by user")
        assert isinstance(exc, LLMCancellationError)
        assert isinstance(exc, LLMException)
        assert not isinstance(exc, LLMProviderError)

    def test_exceptions_can_be_raised_and_caught(self) -> None:
        """Catching LLMException should catch all subtypes."""
        subtypes = [
            LLMProviderError("provider"),
            LLMRateLimitError("rate limit"),
            LLMTimeoutError("timeout"),
            LLMAuthenticationError("auth"),
            LLMInvalidRequestError("invalid"),
            LLMNoResponseError("no response"),
            LLMConfigError("config"),
            LLMTokenCountError("token"),
            LLMCostCalculationError("cost"),
            LLMCancellationError("cancelled"),
        ]

        for exc in subtypes:
            with pytest.raises(LLMException) as exc_info:
                raise exc
            assert exc_info.value is exc
