"""LLM exceptions."""


class LLMException(Exception):
    """Base exception for all LLM errors."""

    pass


class LLMProviderError(LLMException):
    """Error from the LLM provider (e.g., API error)."""

    pass


class LLMRateLimitError(LLMProviderError):
    """Rate limit error from the LLM provider."""

    pass


class LLMTimeoutError(LLMProviderError):
    """Timeout error when calling the LLM provider."""

    pass


class LLMAuthenticationError(LLMProviderError):
    """Authentication error with the LLM provider."""

    pass


class LLMInvalidRequestError(LLMProviderError):
    """Invalid request error (e.g., bad parameters)."""

    pass


class LLMNoResponseError(LLMProviderError):
    """No response from the LLM provider."""

    pass


class LLMConfigError(LLMException):
    """Configuration error for LLM."""

    pass


class LLMTokenCountError(LLMException):
    """Error when counting tokens."""

    pass


class LLMCostCalculationError(LLMException):
    """Error when calculating cost."""

    pass


class LLMCancellationError(LLMException):
    """LLM request was cancelled by user or system."""

    pass
