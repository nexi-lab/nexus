#!/usr/bin/env python3
"""Demo script for Nexus LLM provider abstraction layer.

This script demonstrates:
- Basic LLM completion
- Function calling
- Token counting
- Cost tracking
- Metrics collection
- Multiple models with one API key (OpenRouter)

Usage:
    # Recommended: Use OpenRouter for access to all models with one key
    # Get your key from https://openrouter.ai/keys
    export OPENROUTER_API_KEY="sk-or-v1-..."
    python examples/llm_demo.py

    # Alternative: Use direct provider keys
    export ANTHROPIC_API_KEY="your-key"
    export OPENAI_API_KEY="your-key"
    python examples/llm_demo.py
"""

import os
import sys
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pydantic import SecretStr

from nexus.llm import (
    LLMConfig,
    LLMProvider,
    Message,
    MessageRole,
)


def demo_basic_completion():
    """Demonstrate basic LLM completion."""
    print("\n" + "=" * 60)
    print("DEMO 1: Basic Completion")
    print("=" * 60)

    # Try OpenRouter first (recommended - one key for all models)
    api_key = os.getenv("OPENROUTER_API_KEY")
    # Note: OpenRouter uses different model IDs than direct providers
    model = "openrouter/anthropic/claude-3.5-sonnet"

    if not api_key:
        # Fall back to direct Anthropic key
        api_key = os.getenv("ANTHROPIC_API_KEY")
        model = "claude-sonnet-4-20250514"

    if not api_key:
        print("No API key found. Set OPENROUTER_API_KEY (recommended) or ANTHROPIC_API_KEY")
        return False

    # Create configuration
    config = LLMConfig(
        model=model,
        api_key=SecretStr(api_key),
        temperature=0.7,
        max_output_tokens=1024,
    )

    # Create provider
    provider = LLMProvider.from_config(config)
    print(f"Provider created: {provider.config.model}")
    print(f"Function calling supported: {provider.is_function_calling_active()}")
    print(f"Vision supported: {provider.vision_is_active()}")
    print(f"Prompt caching enabled: {provider.is_caching_prompt_active()}")

    # Create messages
    messages = [
        Message(
            role=MessageRole.SYSTEM,
            content="You are a helpful assistant that provides concise answers.",
        ),
        Message(
            role=MessageRole.USER,
            content="What is the capital of France? Answer in one sentence.",
        ),
    ]

    # Count tokens
    token_count = provider.count_tokens(messages)
    print(f"\nEstimated input tokens: {token_count}")

    try:
        # Send request
        print("\nSending request...")
        response = provider.complete(messages)

        # Display response
        print("\nResponse:")
        print(f"  Content: {response.content}")
        print(f"  Response ID: {response.response_id}")
        print("\nUsage:")
        print(f"  Prompt tokens: {response.usage.get('prompt_tokens', 0)}")
        print(f"  Completion tokens: {response.usage.get('completion_tokens', 0)}")
        print(f"  Total tokens: {response.usage.get('total_tokens', 0)}")
        print(f"  Cost: ${response.cost:.6f}")

        # Display metrics
        print("\nAccumulated Metrics:")
        print(f"  Total cost: ${provider.metrics.accumulated_cost:.6f}")
        print(f"  Total requests: {provider.metrics.total_requests}")
        if provider.metrics.average_latency:
            print(f"  Average latency: {provider.metrics.average_latency:.2f}s")

        return True
    except Exception as e:
        print(f"\nError: {e}")
        print("Note: This demo requires a valid ANTHROPIC_API_KEY environment variable.")
        return False


def demo_function_calling():
    """Demonstrate function calling."""
    print("\n" + "=" * 60)
    print("DEMO 2: Function Calling")
    print("=" * 60)

    api_key = os.getenv("OPENROUTER_API_KEY")
    model = "openrouter/anthropic/claude-3.5-sonnet"

    if not api_key:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        model = "claude-sonnet-4-20250514"

    if not api_key:
        print("Skipping: No API key found")
        return False

    # Create configuration
    config = LLMConfig(
        model=model,
        api_key=SecretStr(api_key),
        temperature=0.7,
        native_tool_calling=True,
    )

    provider = LLMProvider.from_config(config)
    print(f"Function calling active: {provider.is_function_calling_active()}")

    # Define tools
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the current weather for a location",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {
                            "type": "string",
                            "description": "The city and state, e.g. San Francisco, CA",
                        },
                        "unit": {
                            "type": "string",
                            "enum": ["celsius", "fahrenheit"],
                            "description": "The temperature unit",
                        },
                    },
                    "required": ["location"],
                },
            },
        }
    ]

    # Create messages
    messages = [
        Message(
            role=MessageRole.USER,
            content="What's the weather like in Paris?",
        )
    ]

    try:
        print("\nSending request with tools...")
        response = provider.complete(messages, tools=tools)

        print("\nResponse:")
        if response.tool_calls:
            print(f"  Tool calls detected: {len(response.tool_calls)}")
            for i, tool_call in enumerate(response.tool_calls, 1):
                print(f"\n  Tool Call {i}:")
                print(f"    Function: {tool_call['function']['name']}")
                print(f"    Arguments: {tool_call['function']['arguments']}")
        else:
            print(f"  Content: {response.content}")

        print(f"\nCost: ${response.cost:.6f}")
        return True
    except Exception as e:
        print(f"\nError: {e}")
        return False


def demo_streaming():
    """Demonstrate streaming responses."""
    print("\n" + "=" * 60)
    print("DEMO 3: Streaming")
    print("=" * 60)

    api_key = os.getenv("OPENROUTER_API_KEY")
    model = "openrouter/anthropic/claude-3.5-sonnet"

    if not api_key:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        model = "claude-sonnet-4-20250514"

    if not api_key:
        print("Skipping: No API key found")
        return False

    config = LLMConfig(
        model=model,
        api_key=SecretStr(api_key),
        temperature=0.7,
    )

    provider = LLMProvider.from_config(config)

    messages = [
        Message(
            role=MessageRole.USER,
            content="Count from 1 to 5, one number per line.",
        )
    ]

    try:
        print("\nStreaming response:")
        print("-" * 40)
        for chunk in provider.stream(messages):
            print(chunk, end="", flush=True)
        print("\n" + "-" * 40)
        return True
    except Exception as e:
        print(f"\nError: {e}")
        return False


def demo_multiple_providers():
    """Demonstrate using multiple models."""
    print("\n" + "=" * 60)
    print("DEMO 4: Multiple Models")
    print("=" * 60)

    # Check for OpenRouter key (recommended)
    openrouter_key = os.getenv("OPENROUTER_API_KEY")

    if openrouter_key:
        print("Using OpenRouter - one key for all models!\n")
        # With OpenRouter, you can access all models with one key
        # IMPORTANT: Prefix with "openrouter/" to route through OpenRouter
        # Note: OpenRouter model IDs may differ from direct provider IDs
        models = [
            ("Claude 3.5 Sonnet", "openrouter/anthropic/claude-3.5-sonnet"),
            ("GPT-4o", "openrouter/openai/gpt-4o"),
            ("Llama 3.3 70B", "openrouter/meta-llama/llama-3.3-70b-instruct"),
        ]

        providers = [
            (
                name,
                LLMProvider.from_config(
                    LLMConfig(
                        model=model,
                        api_key=SecretStr(openrouter_key),
                    )
                ),
            )
            for name, model in models
        ]
    else:
        # Fall back to individual provider keys
        print("Using individual provider keys\n")
        providers = []

        # Anthropic Claude
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            providers.append(
                (
                    "Anthropic Claude",
                    LLMProvider.from_config(
                        LLMConfig(
                            model="claude-sonnet-4-20250514",
                            api_key=SecretStr(anthropic_key),
                        )
                    ),
                )
            )

        # OpenAI
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            providers.append(
                (
                    "OpenAI GPT-4",
                    LLMProvider.from_config(
                        LLMConfig(
                            model="gpt-4o",
                            api_key=SecretStr(openai_key),
                        )
                    ),
                )
            )

        # Google Gemini
        google_key = os.getenv("GOOGLE_API_KEY")
        if google_key:
            providers.append(
                (
                    "Google Gemini",
                    LLMProvider.from_config(
                        LLMConfig(
                            model="gemini-pro",
                            api_key=SecretStr(google_key),
                        )
                    ),
                )
            )

    if not providers:
        print("No API keys found.")
        print("Recommended: Set OPENROUTER_API_KEY for access to all models")
        print("Alternative: Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY")
        return False

    # Test same prompt with all providers
    messages = [
        Message(
            role=MessageRole.USER,
            content="Say 'Hello from' followed by your model name.",
        )
    ]

    print(f"\nTesting {len(providers)} provider(s):\n")

    for name, provider in providers:
        try:
            print(f"{name}:")
            response = provider.complete(messages)
            print(f"  Response: {response.content}")
            print(f"  Cost: ${response.cost:.6f}")
            print()
        except Exception as e:
            print(f"  Error: {e}\n")

    return True


def main():
    """Run all demos."""
    print("Nexus LLM Provider Abstraction Layer Demo")
    print("==========================================")

    # Check for API keys
    has_openrouter = bool(os.getenv("OPENROUTER_API_KEY"))
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_openai = bool(os.getenv("OPENAI_API_KEY"))
    has_google = bool(os.getenv("GOOGLE_API_KEY"))

    print("\nAPI Keys detected:")
    print(
        f"  OPENROUTER_API_KEY: {'✓' if has_openrouter else '✗'} (recommended - one key for all models)"
    )
    print(f"  ANTHROPIC_API_KEY: {'✓' if has_anthropic else '✗'}")
    print(f"  OPENAI_API_KEY: {'✓' if has_openai else '✗'}")
    print(f"  GOOGLE_API_KEY: {'✓' if has_google else '✗'}")

    if not (has_openrouter or has_anthropic or has_openai or has_google):
        print("\nNo API keys found!")
        print("\nRecommended (simplest):")
        print("  1. Get an OpenRouter API key: https://openrouter.ai/keys")
        print("  2. export OPENROUTER_API_KEY='sk-or-v1-...'")
        print("  3. Access Claude, GPT-4, Gemini, and 200+ models with one key!")
        print("\nAlternative:")
        print("  export ANTHROPIC_API_KEY='your-key'")
        print("  export OPENAI_API_KEY='your-key'")
        print("  export GOOGLE_API_KEY='your-key'")

    # Run demos
    results = []
    results.append(("Basic Completion", demo_basic_completion()))
    results.append(("Function Calling", demo_function_calling()))
    results.append(("Streaming", demo_streaming()))
    results.append(("Multiple Models", demo_multiple_providers()))

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, success in results:
        status = "✓ PASS" if success else "✗ SKIP/FAIL"
        print(f"{name}: {status}")

    if has_openrouter:
        print("\n💡 You're using OpenRouter - great choice!")
        print("   Switch between Claude, GPT, Gemini by changing the model name.")
    elif has_anthropic or has_openai or has_google:
        print("\n💡 Tip: Consider using OpenRouter for easier model switching!")
        print("   Get your key at https://openrouter.ai/keys")


if __name__ == "__main__":
    main()
