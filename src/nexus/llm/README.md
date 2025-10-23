# Nexus LLM Provider Abstraction

Unified interface for multiple LLM providers with automatic cost tracking, token counting, and metrics.

## Quick Start (Recommended: OpenRouter)

```python
import os
from pydantic import SecretStr
from nexus.llm import LLMConfig, LLMProvider, Message, MessageRole

# One API key for all models! (Claude, GPT-4, Gemini, etc.)
# IMPORTANT: Prefix with "openrouter/" to route through OpenRouter
config = LLMConfig(
    model="openrouter/anthropic/claude-3.5-sonnet",
    api_key=SecretStr(os.getenv("OPENROUTER_API_KEY")),
)
# Other models: openrouter/openai/gpt-4o, openrouter/google/gemini-pro
# Check available models: https://openrouter.ai/models

provider = LLMProvider.from_config(config)

messages = [
    Message(role=MessageRole.USER, content="What is 2+2?"),
]

response = provider.complete(messages)
print(response.content)
print(f"Cost: ${response.cost:.6f}")
```

## Why OpenRouter?

**One key = 200+ models**

Instead of managing multiple API keys:
- ❌ ANTHROPIC_API_KEY → only Claude models
- ❌ OPENAI_API_KEY → only GPT models
- ❌ GOOGLE_API_KEY → only Gemini models

Use OpenRouter:
- ✅ OPENROUTER_API_KEY → Claude, GPT, Gemini, Llama, and 200+ more models

Get your key: https://openrouter.ai/keys

## Features

- **Multi-provider support**: Claude, GPT, Gemini, Llama, and more
- **Function calling**: Unified tool/function calling across providers
- **Token counting**: Accurate token estimation before sending requests
- **Cost tracking**: Automatic cost calculation and accumulation
- **Retry logic**: Exponential backoff with configurable parameters
- **Streaming**: Support for streaming responses
- **Vision**: Image input for vision-capable models
- **Prompt caching**: Claude prompt caching support

## Examples

See `/examples/llm_demo.py` for complete examples of:
- Basic completion
- Function calling
- Streaming
- Multiple models
- Cost tracking

## Documentation

Full documentation: `/docs/llm_provider.md`
