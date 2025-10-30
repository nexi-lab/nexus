"""Tests for LLM exceptions."""

import pytest

from nexus.llm.exceptions import (
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
    """Test LLM exception classes."""

    def test_llm_exception_is_exception(self):
        """Test that LLMException is an Exception."""
        exc = LLMException("test error")
        assert isinstance(exc, Exception)
        assert str(exc) == "test error"

    def test_llm_provider_error_is_llm_exception(self):
        """Test that LLMProviderError inherits from LLMException."""
        exc = LLMProviderError("provider error")
        assert isinstance(exc, LLMException)
        assert isinstance(exc, Exception)
        assert str(exc) == "provider error"

    def test_llm_rate_limit_error(self):
        """Test LLMRateLimitError."""
        exc = LLMRateLimitError("rate limited")
        assert isinstance(exc, LLMProviderError)
        assert isinstance(exc, LLMException)
        assert str(exc) == "rate limited"

    def test_llm_timeout_error(self):
        """Test LLMTimeoutError."""
        exc = LLMTimeoutError("request timed out")
        assert isinstance(exc, LLMProviderError)
        assert isinstance(exc, LLMException)
        assert str(exc) == "request timed out"

    def test_llm_authentication_error(self):
        """Test LLMAuthenticationError."""
        exc = LLMAuthenticationError("auth failed")
        assert isinstance(exc, LLMProviderError)
        assert isinstance(exc, LLMException)
        assert str(exc) == "auth failed"

    def test_llm_invalid_request_error(self):
        """Test LLMInvalidRequestError."""
        exc = LLMInvalidRequestError("invalid params")
        assert isinstance(exc, LLMProviderError)
        assert isinstance(exc, LLMException)
        assert str(exc) == "invalid params"

    def test_llm_no_response_error(self):
        """Test LLMNoResponseError."""
        exc = LLMNoResponseError("no response")
        assert isinstance(exc, LLMProviderError)
        assert isinstance(exc, LLMException)
        assert str(exc) == "no response"

    def test_llm_config_error(self):
        """Test LLMConfigError."""
        exc = LLMConfigError("config error")
        assert isinstance(exc, LLMException)
        assert not isinstance(exc, LLMProviderError)
        assert str(exc) == "config error"

    def test_llm_token_count_error(self):
        """Test LLMTokenCountError."""
        exc = LLMTokenCountError("token count error")
        assert isinstance(exc, LLMException)
        assert not isinstance(exc, LLMProviderError)
        assert str(exc) == "token count error"

    def test_llm_cost_calculation_error(self):
        """Test LLMCostCalculationError."""
        exc = LLMCostCalculationError("cost error")
        assert isinstance(exc, LLMException)
        assert not isinstance(exc, LLMProviderError)
        assert str(exc) == "cost error"

    def test_llm_cancellation_error(self):
        """Test LLMCancellationError."""
        exc = LLMCancellationError("cancelled")
        assert isinstance(exc, LLMException)
        assert not isinstance(exc, LLMProviderError)
        assert str(exc) == "cancelled"

    def test_exceptions_can_be_raised_and_caught(self):
        """Test that exceptions can be raised and caught."""
        with pytest.raises(LLMException):
            raise LLMException("test")

        with pytest.raises(LLMProviderError):
            raise LLMProviderError("test")

        with pytest.raises(LLMRateLimitError):
            raise LLMRateLimitError("test")

        # Test catching base exception
        with pytest.raises(LLMException):
            raise LLMRateLimitError("test")

        # Test catching provider error
        with pytest.raises(LLMProviderError):
            raise LLMTimeoutError("test")
