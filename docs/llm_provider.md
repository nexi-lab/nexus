# LLM Provider Abstraction Layer

Nexus v0.4.0 introduces a unified LLM provider abstraction layer that supports multiple AI providers with a consistent interface.

## Overview

The LLM provider abstraction layer provides:

- **Multi-provider support**: Anthropic Claude, OpenAI GPT, Google Gemini, and more via litellm
- **Unified interface**: Consistent API across all providers
- **Function calling**: Tool/function calling support across providers
- **Vision support**: Image input handling for vision-capable models
- **Token counting**: Accurate token counting with caching
- **Cost tracking**: Automatic cost calculation and tracking
- **Metrics storage**: Integration with Nexus metadata database
- **Response caching**: CAS-based caching for LLM responses (planned)
- **Retry logic**: Exponential backoff with configurable retry parameters

## Installation

The LLM provider dependencies are included in the base Nexus installation:

```bash
pip install nexus-ai-fs
```

Dependencies installed:
- `litellm>=1.0` - Multi-provider LLM support
- `tiktoken>=0.5` - Token counting for OpenAI models
- `anthropic>=0.40` - Native Anthropic SDK

## Quick Start

### Recommended: OpenRouter (One Key, All Models)

The simplest way to use multiple LLM providers is through [OpenRouter](https://openrouter.ai/), which gives you access to Claude, GPT-4, Gemini, and 200+ other models with a single API key.

```bash
# Get your OpenRouter API key from https://openrouter.ai/keys
export OPENROUTER_API_KEY="sk-or-v1-..."
```

```python
import os
from pydantic import SecretStr
from nexus.llm import LLMConfig, LLMProvider, Message, MessageRole

# One API key for all models!
api_key = SecretStr(os.getenv("OPENROUTER_API_KEY"))

# Use Claude (prefix with "openrouter/" for OpenRouter routing)
config = LLMConfig(
    model="openrouter/anthropic/claude-3.5-sonnet",
    api_key=api_key,
    temperature=0.7,
)

# Or use GPT-4
# config = LLMConfig(
#     model="openrouter/openai/gpt-4o",
#     api_key=api_key,
# )

# Or use Gemini
# config = LLMConfig(
#     model="openrouter/google/gemini-pro",
#     api_key=api_key,
# )

# Note: OpenRouter model IDs may differ from direct provider IDs.
# Check available models at https://openrouter.ai/models

provider = LLMProvider.from_config(config)

# Create messages
messages = [
    Message(role=MessageRole.SYSTEM, content="You are a helpful assistant."),
    Message(role=MessageRole.USER, content="What is the capital of France?"),
]

# Send request
response = provider.complete(messages)

print(response.content)
print(f"Cost: ${response.cost:.6f}")
```

### Alternative: Direct Provider Keys

You can also use provider-specific API keys directly:

```python
# Anthropic direct
config = LLMConfig(
    model="claude-sonnet-4-20250514",
    api_key=SecretStr(os.getenv("ANTHROPIC_API_KEY")),
)

# OpenAI direct
config = LLMConfig(
    model="gpt-4o",
    api_key=SecretStr(os.getenv("OPENAI_API_KEY")),
)
```

### Function Calling

```python
# Define tools
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City and state, e.g. San Francisco, CA",
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                    },
                },
                "required": ["location"],
            },
        },
    }
]

# Request with tools
response = provider.complete(messages, tools=tools)

# Handle tool calls
if response.tool_calls:
    for tool_call in response.tool_calls:
        function_name = tool_call["function"]["name"]
        arguments = tool_call["function"]["arguments"]
        print(f"Function: {function_name}")
        print(f"Arguments: {arguments}")
```

### Streaming

```python
# Stream response
for chunk in provider.stream(messages):
    print(chunk, end="", flush=True)
```

### Token Counting

```python
# Count tokens before sending
token_count = provider.count_tokens(messages)
print(f"Estimated tokens: {token_count}")
```

## Configuration

### LLMConfig Options

```python
config = LLMConfig(
    # Model configuration
    model="claude-sonnet-4-20250514",  # Model name
    api_key=SecretStr("your-key"),     # API key
    base_url=None,                      # Custom API endpoint
    api_version=None,                   # API version (Azure)
    custom_llm_provider=None,           # Custom provider name

    # Generation parameters
    temperature=0.7,                    # 0.0-2.0
    max_output_tokens=4096,             # Max tokens to generate
    max_input_tokens=None,              # Max input tokens (auto-detected)
    top_p=1.0,                          # Nucleus sampling
    seed=None,                          # Random seed

    # Timeout and retry
    timeout=120.0,                      # Timeout in seconds
    num_retries=3,                      # Number of retries
    retry_min_wait=4.0,                 # Min wait between retries
    retry_max_wait=10.0,                # Max wait between retries
    retry_multiplier=2.0,               # Exponential backoff multiplier

    # Features
    native_tool_calling=None,           # Enable function calling (auto-detect)
    caching_prompt=False,               # Enable prompt caching (Claude)
    disable_vision=False,               # Disable vision capabilities

    # Advanced
    custom_tokenizer=None,              # Custom tokenizer name
    reasoning_effort=None,              # "low"/"medium"/"high" (o1/o3 models)
    input_cost_per_token=None,          # Custom input cost
    output_cost_per_token=None,         # Custom output cost
)
```

## Why OpenRouter?

| Approach | API Keys Needed | Supported Models | Key Management |
|----------|----------------|------------------|----------------|
| **OpenRouter** (Recommended) | 1 key | 200+ models (Claude, GPT, Gemini, Llama, etc.) | Single key to manage |
| Direct providers | 3+ keys | Only provider's own models | Multiple keys, multiple accounts |

**Benefits of OpenRouter:**
- ✅ One API key for all models
- ✅ Easy model switching (just change model name)
- ✅ Unified billing and analytics
- ✅ No need for multiple provider accounts
- ✅ Access to 200+ models including latest releases
- ✅ Free tier available for testing

Get your OpenRouter key: https://openrouter.ai/keys

## Supported Providers

The abstraction layer supports all providers available through litellm:

### Anthropic Claude

```python
config = LLMConfig(
    model="claude-sonnet-4-20250514",
    api_key=SecretStr(os.getenv("ANTHROPIC_API_KEY")),
    caching_prompt=True,  # Enable prompt caching
)
```

Models:
- `claude-opus-4-20250514`
- `claude-sonnet-4-20250514`
- `claude-3-7-sonnet-20250219`
- `claude-3-5-sonnet-20241022`
- `claude-3-5-haiku-20241022`
- `claude-3-opus-20240229`

### OpenAI

```python
config = LLMConfig(
    model="gpt-4o",
    api_key=SecretStr(os.getenv("OPENAI_API_KEY")),
)
```

Models:
- `gpt-4o`
- `gpt-4o-mini`
- `o1-2024-12-17`
- `o3-mini`
- `o3`

### Google Gemini

```python
config = LLMConfig(
    model="gemini-pro",
    api_key=SecretStr(os.getenv("GOOGLE_API_KEY")),
)
```

Models:
- `gemini-2.5-pro`
- `gemini-pro`

### OpenRouter (Recommended)

Access 200+ models with one API key. Get your key at https://openrouter.ai/keys

```python
config = LLMConfig(
    model="openrouter/anthropic/claude-3.5-sonnet",
    api_key=SecretStr(os.getenv("OPENROUTER_API_KEY")),
)

# Other popular models via OpenRouter:
# model="openrouter/openai/gpt-4o"
# model="openrouter/google/gemini-pro"
# model="openrouter/meta-llama/llama-3.3-70b-instruct"
# model="openrouter/qwen/qwen-2.5-72b-instruct"
```

**Notes:**
- The `openrouter/` prefix tells litellm to route through OpenRouter
- OpenRouter model IDs may differ from direct provider IDs
- Check available models at https://openrouter.ai/models

### Azure OpenAI

```python
config = LLMConfig(
    model="azure/gpt-4o",
    api_key=SecretStr(os.getenv("AZURE_API_KEY")),
    base_url="https://your-resource.openai.azure.com",
    api_version="2024-02-15-preview",
)
```

## Metrics Tracking

The provider automatically tracks metrics:

```python
# Access metrics
print(f"Total cost: ${provider.metrics.accumulated_cost:.6f}")
print(f"Total requests: {provider.metrics.total_requests}")
print(f"Average latency: {provider.metrics.average_latency:.2f}s")

# Token usage
usage = provider.metrics.accumulated_token_usage
print(f"Prompt tokens: {usage.prompt_tokens}")
print(f"Completion tokens: {usage.completion_tokens}")
print(f"Cache read tokens: {usage.cache_read_tokens}")
print(f"Cache write tokens: {usage.cache_write_tokens}")

# Reset metrics
provider.reset_metrics()
```

### Persistent Metrics

Metrics can be saved to Nexus metadata database:

```python
from nexus.llm import MetricsStore

store = MetricsStore(metadata_path="/path/to/nexus/metadata.db")

# Save metrics
store.save_metrics(provider.metrics, session_id="session-123")

# Load metrics
metrics = store.load_metrics(session_id="session-123")
```

## Response Caching

Response caching using Nexus CAS (planned for future release):

```python
from nexus.llm import CachedLLMProvider

provider = CachedLLMProvider.from_config(
    config,
    cache_backend=nexus_cas,  # Nexus CAS instance
    cache_ttl=3600,           # Cache TTL in seconds
)

# Responses are automatically cached by message hash
response = provider.complete(messages)  # Cache miss
response = provider.complete(messages)  # Cache hit (faster, free)
```

## Error Handling

```python
from nexus.llm import (
    LLMRateLimitError,
    LLMTimeoutError,
    LLMAuthenticationError,
    LLMNoResponseError,
)

try:
    response = provider.complete(messages)
except LLMRateLimitError as e:
    print("Rate limit exceeded, retrying...")
except LLMTimeoutError as e:
    print("Request timed out")
except LLMAuthenticationError as e:
    print("Invalid API key")
except LLMNoResponseError as e:
    print("No response from provider")
```

## Best Practices

### 1. Use Environment Variables for API Keys

```python
import os
from pydantic import SecretStr

config = LLMConfig(
    model="claude-sonnet-4",
    api_key=SecretStr(os.getenv("ANTHROPIC_API_KEY")),
)
```

### 2. Enable Prompt Caching for Claude

```python
config = LLMConfig(
    model="claude-sonnet-4-20250514",
    api_key=SecretStr(api_key),
    caching_prompt=True,  # Reduces costs for repeated context
)
```

### 3. Monitor Token Usage

```python
# Check tokens before sending
tokens = provider.count_tokens(messages)
if tokens > provider.config.max_input_tokens:
    print("Warning: Message too long, truncating...")
```

### 4. Use Appropriate Timeouts

```python
config = LLMConfig(
    model="claude-sonnet-4",
    api_key=SecretStr(api_key),
    timeout=60.0,      # Shorter timeout for simple requests
    num_retries=5,     # More retries for important requests
)
```

### 5. Track Costs

```python
# Set cost alerts
if provider.metrics.accumulated_cost > 1.0:
    print("Warning: Costs exceed $1.00")
```

## Examples

See `examples/llm_demo.py` for complete examples:

```bash
# Set API key
export ANTHROPIC_API_KEY="your-key"

# Run demo
python examples/llm_demo.py
```

## Testing

Run tests:

```bash
# Unit tests (no API calls)
pytest tests/test_llm.py

# Integration tests (requires API keys)
export ANTHROPIC_API_KEY="your-key"
pytest tests/test_llm.py --run-integration
```

## Roadmap

### v0.4.0 (Current)
- ✅ Multi-provider support (Anthropic, OpenAI, Google)
- ✅ Function calling
- ✅ Token counting
- ✅ Cost tracking
- ✅ Metrics collection
- ✅ Retry logic

### v0.4.1 (Planned)
- ⏳ Response caching with Nexus CAS
- ⏳ Persistent metrics storage
- ⏳ Async support improvements
- ⏳ Better streaming error handling

### v0.5.0 (Planned)
- ⏳ LLM-powered document analysis
- ⏳ Semantic search integration
- ⏳ RAG (Retrieval Augmented Generation)
- ⏳ Agent workflows

## Architecture

The LLM provider abstraction is built on top of litellm with Nexus-specific enhancements:

```
┌─────────────────────────────────────────┐
│         Nexus Application Code           │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│      LLMProvider (nexus.llm)            │
│  - Unified interface                     │
│  - Metrics tracking                      │
│  - Response caching (CAS)                │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│         LiteLLM Library                  │
│  - Multi-provider routing                │
│  - Token counting                        │
│  - Cost calculation                      │
└──────────────────┬──────────────────────┘
                   │
        ┌──────────┼──────────┐
        │          │          │
┌───────▼────┐ ┌──▼───────┐ ┌▼─────────┐
│ Anthropic  │ │ OpenAI   │ │  Google  │
│   Claude   │ │   GPT    │ │  Gemini  │
└────────────┘ └──────────┘ └──────────┘
```

## Contributing

Contributions are welcome! Areas for improvement:

1. **Provider-specific optimizations**: Better handling of provider-specific features
2. **Enhanced caching**: More sophisticated cache invalidation strategies
3. **Metrics visualization**: Dashboard for tracking LLM usage
4. **Cost optimization**: Automatic model selection based on cost/quality tradeoffs
5. **Testing**: More comprehensive test coverage

See [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.

## License

Apache 2.0 - See [LICENSE](../LICENSE) for details.
