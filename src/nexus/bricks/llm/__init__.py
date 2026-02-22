"""LLM provider abstraction layer for Nexus (LLM Brick).

Provides a unified interface for multiple LLM providers with:
- Multi-provider support (Anthropic, OpenAI, Google, etc.)
- Function/tool calling
- Vision support
- Token counting
- Cost tracking
- Response caching with Nexus CAS

Issue #1521: Orchestration concerns (document_reader, context_builder,
citation) moved to nexus.services.llm_* modules. This brick now only
exports provider primitives.
"""

from nexus.bricks.llm.cancellation import (
    AsyncCancellationToken,
    CancellationToken,
    install_signal_handlers,
    request_shutdown,
    reset_shutdown_flag,
    should_continue,
)
from nexus.bricks.llm.config import LLMConfig
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
from nexus.bricks.llm.manifest import LLMBrickManifest, verify_imports
from nexus.bricks.llm.metrics import LLMMetrics, ResponseLatency, TokenUsage
from nexus.bricks.llm.provider import LiteLLMProvider, LLMProvider, LLMResponse
from nexus.contracts.llm_types import (
    ContentType,
    ImageContent,
    ImageDetail,
    Message,
    MessageRole,
    TextContent,
    ToolCall,
    ToolFunction,
)

__all__ = [
    # Manifest
    "LLMBrickManifest",
    "verify_imports",
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
