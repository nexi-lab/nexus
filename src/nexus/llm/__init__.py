"""LLM provider abstraction layer for Nexus.

Provides a unified interface for multiple LLM providers with:
- Multi-provider support (Anthropic, OpenAI, Google, etc.)
- Function/tool calling
- Vision support
- Token counting
- Cost tracking
- Metrics storage in Nexus metadata database
- Response caching with Nexus CAS
"""

from nexus.llm.config import LLMConfig
from nexus.llm.exceptions import (
    LLMAuthenticationError,
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
from nexus.llm.message import (
    ContentType,
    ImageContent,
    ImageDetail,
    Message,
    MessageRole,
    TextContent,
    ToolCall,
    ToolFunction,
)
from nexus.llm.metrics import LLMMetrics, MetricsStore, ResponseLatency, TokenUsage
from nexus.llm.provider import LiteLLMProvider, LLMProvider, LLMResponse

__all__ = [
    # Config
    "LLMConfig",
    # Providers
    "LLMProvider",
    "LiteLLMProvider",
    "LLMResponse",
    # Messages
    "Message",
    "MessageRole",
    "TextContent",
    "ImageContent",
    "ImageDetail",
    "ContentType",
    "ToolCall",
    "ToolFunction",
    # Metrics
    "LLMMetrics",
    "TokenUsage",
    "ResponseLatency",
    "MetricsStore",
    # Exceptions
    "LLMException",
    "LLMProviderError",
    "LLMRateLimitError",
    "LLMTimeoutError",
    "LLMAuthenticationError",
    "LLMInvalidRequestError",
    "LLMNoResponseError",
    "LLMConfigError",
    "LLMTokenCountError",
    "LLMCostCalculationError",
]
