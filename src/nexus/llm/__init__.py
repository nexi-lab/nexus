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

from nexus.llm.cancellation import (
    AsyncCancellationToken,
    CancellationToken,
    install_signal_handlers,
    request_shutdown,
    reset_shutdown_flag,
    should_continue,
)
from nexus.llm.citation import Citation, CitationExtractor, DocumentReadResult
from nexus.llm.config import LLMConfig
from nexus.llm.context_builder import ContextBuilder
from nexus.llm.document_reader import LLMDocumentReader
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
    # Document Reading
    "LLMDocumentReader",
    "DocumentReadResult",
    "Citation",
    "CitationExtractor",
    "ContextBuilder",
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
    # Cancellation
    "CancellationToken",
    "AsyncCancellationToken",
    "should_continue",
    "request_shutdown",
    "reset_shutdown_flag",
    "install_signal_handlers",
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
    "LLMCancellationError",
]
