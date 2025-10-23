#!/usr/bin/env python3
"""Demo script for Nexus Async LLM provider with cancellation support.

This script demonstrates:
- Basic async completion
- Async streaming
- Concurrent requests
- Cancellation handling
- Custom cancellation callbacks
- Task cleanup

Usage:
    # Recommended: Use OpenRouter for access to all models with one key
    # Get your key from https://openrouter.ai/keys
    export OPENROUTER_API_KEY="sk-or-v1-..."
    python examples/llm_async_demo.py

    # Alternative: Use direct provider keys
    export ANTHROPIC_API_KEY="your-key"
    python examples/llm_async_demo.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pydantic import SecretStr

from nexus.llm import (
    AsyncCancellationToken,
    LLMCancellationError,
    LLMConfig,
    LLMProvider,
    Message,
    MessageRole,
)


async def demo_basic_async_completion():
    """Demonstrate basic async LLM completion."""
    print("\n" + "=" * 60)
    print("DEMO 1: Basic Async Completion")
    print("=" * 60)

    # Try OpenRouter first (recommended - one key for all models)
    api_key = os.getenv("OPENROUTER_API_KEY")
    model = "openrouter/anthropic/claude-3.5-sonnet"

    if not api_key:
        # Fall back to direct Anthropic key
        api_key = os.getenv("ANTHROPIC_API_KEY")
        model = "claude-sonnet-4-20250514"

    if not api_key:
        print("No API key found. Set OPENROUTER_API_KEY or ANTHROPIC_API_KEY")
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

    try:
        # Send async request
        print("\nSending async request...")
        start_time = time.time()
        response = await provider.complete_async(messages)
        elapsed = time.time() - start_time

        # Display response
        print(f"\nResponse (took {elapsed:.2f}s):")
        print(f"  Content: {response.content}")
        print(f"  Response ID: {response.response_id}")
        print(f"  Cost: ${response.cost:.6f}")

        # Cleanup
        await provider.cleanup()
        return True
    except Exception as e:
        print(f"\nError: {e}")
        return False


async def demo_async_streaming():
    """Demonstrate async streaming."""
    print("\n" + "=" * 60)
    print("DEMO 2: Async Streaming")
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
        print("\nStreaming async response:")
        print("-" * 40)
        async for chunk in provider.stream_async(messages):
            print(chunk, end="", flush=True)
        print("\n" + "-" * 40)

        # Cleanup
        await provider.cleanup()
        return True
    except Exception as e:
        print(f"\nError: {e}")
        return False


async def demo_concurrent_requests():
    """Demonstrate concurrent async requests."""
    print("\n" + "=" * 60)
    print("DEMO 3: Concurrent Async Requests")
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
        max_output_tokens=512,
    )

    provider = LLMProvider.from_config(config)

    # Create 3 different requests
    message_sets = [
        [Message(role=MessageRole.USER, content="What is the capital of France?")],
        [Message(role=MessageRole.USER, content="What is the capital of Germany?")],
        [Message(role=MessageRole.USER, content="What is the capital of Italy?")],
    ]

    try:
        print("\nSending 3 concurrent requests...")
        start_time = time.time()

        # Run all requests concurrently
        tasks = [provider.complete_async(messages) for messages in message_sets]
        responses = await asyncio.gather(*tasks)

        elapsed = time.time() - start_time

        print(f"\nAll requests completed in {elapsed:.2f}s")
        print(f"Average time per request: {elapsed / len(responses):.2f}s\n")

        for i, response in enumerate(responses, 1):
            print(f"Response {i}: {response.content}")

        print(f"\nTotal cost: ${sum(r.cost for r in responses):.6f}")

        # Cleanup
        await provider.cleanup()
        return True
    except Exception as e:
        print(f"\nError: {e}")
        return False


async def demo_cancellation():
    """Demonstrate request cancellation."""
    print("\n" + "=" * 60)
    print("DEMO 4: Request Cancellation")
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
        cancellation_check_interval=0.5,  # Check every 0.5s
    )

    provider = LLMProvider.from_config(config)

    messages = [
        Message(
            role=MessageRole.USER,
            content="Write a long essay about artificial intelligence (at least 500 words).",
        )
    ]

    # Create cancellation token
    token = AsyncCancellationToken()

    # Schedule cancellation after 2 seconds
    async def cancel_after_delay():
        await asyncio.sleep(2.0)
        print("\n[Cancelling request after 2 seconds...]")
        token.cancel()

    try:
        print("\nSending request with cancellation token...")
        print("(Will be cancelled after 2 seconds)")

        # Start cancellation task
        cancel_task = asyncio.create_task(cancel_after_delay())

        # This should be cancelled
        start_time = time.time()
        response = await provider.complete_async(messages, cancellation_token=token)
        elapsed = time.time() - start_time

        # If we get here, request completed before cancellation
        print(f"\nRequest completed before cancellation ({elapsed:.2f}s)")
        print(f"Response length: {len(response.content or '')} chars")

        await cancel_task  # Wait for cancel task to finish
        await provider.cleanup()
        return True

    except LLMCancellationError:
        elapsed = time.time() - start_time
        print(f"\n✓ Request successfully cancelled after {elapsed:.2f}s")
        await cancel_task  # Wait for cancel task to finish
        await provider.cleanup()
        return True
    except Exception as e:
        print(f"\nError: {e}")
        await provider.cleanup()
        return False


async def demo_custom_cancellation_callback():
    """Demonstrate custom cancellation callback."""
    print("\n" + "=" * 60)
    print("DEMO 5: Custom Cancellation Callback")
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
        cancellation_check_interval=0.5,
    )

    provider = LLMProvider.from_config(config)

    messages = [
        Message(
            role=MessageRole.USER,
            content="Write a short story about a robot.",
        )
    ]

    # Create a flag that will be set by external logic
    should_cancel = False

    # Define async cancellation callback
    async def check_cancel():
        return should_cancel

    # Create token with custom callback
    token = AsyncCancellationToken(on_cancel_async_fn=check_cancel)

    # Simulate external cancellation trigger
    async def trigger_cancel():
        await asyncio.sleep(1.5)
        print("\n[External event triggered cancellation]")
        nonlocal should_cancel
        should_cancel = True

    try:
        print("\nSending request with custom cancellation callback...")
        print("(Will be triggered by external event after 1.5 seconds)")

        # Start trigger task
        trigger_task = asyncio.create_task(trigger_cancel())

        start_time = time.time()
        response = await provider.complete_async(messages, cancellation_token=token)
        elapsed = time.time() - start_time

        print(f"\nRequest completed before cancellation ({elapsed:.2f}s)")
        print(f"Response length: {len(response.content or '')} chars")
        await trigger_task
        await provider.cleanup()
        return True

    except LLMCancellationError:
        elapsed = time.time() - start_time
        print(f"\n✓ Request cancelled via custom callback after {elapsed:.2f}s")
        await trigger_task
        await provider.cleanup()
        return True
    except Exception as e:
        print(f"\nError: {e}")
        await provider.cleanup()
        return False


async def demo_streaming_with_cancellation():
    """Demonstrate streaming with cancellation."""
    print("\n" + "=" * 60)
    print("DEMO 6: Streaming with Cancellation")
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
        cancellation_check_interval=0.5,
    )

    provider = LLMProvider.from_config(config)

    messages = [
        Message(
            role=MessageRole.USER,
            content="Write a long poem about the ocean (at least 20 lines).",
        )
    ]

    # Create cancellation token
    token = AsyncCancellationToken()

    # Cancel after receiving some chunks
    chunks_received = 0
    cancel_after_chunks = 10

    try:
        print(f"\nStreaming with cancellation after {cancel_after_chunks} chunks:")
        print("-" * 40)

        async for chunk in provider.stream_async(messages, cancellation_token=token):
            print(chunk, end="", flush=True)
            chunks_received += 1

            if chunks_received >= cancel_after_chunks:
                print("\n[Cancelling stream...]")
                token.cancel()

        print("\n" + "-" * 40)
        print(f"Stream completed ({chunks_received} chunks)")
        await provider.cleanup()
        return True

    except LLMCancellationError:
        print("\n" + "-" * 40)
        print(f"✓ Stream cancelled after {chunks_received} chunks")
        await provider.cleanup()
        return True
    except Exception as e:
        print(f"\nError: {e}")
        await provider.cleanup()
        return False


async def main():
    """Run all async demos."""
    print("Nexus Async LLM Provider Demo")
    print("=" * 60)

    # Check for API keys
    has_openrouter = bool(os.getenv("OPENROUTER_API_KEY"))
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))

    print("\nAPI Keys detected:")
    print(
        f"  OPENROUTER_API_KEY: {'✓' if has_openrouter else '✗'} (recommended - one key for all models)"
    )
    print(f"  ANTHROPIC_API_KEY: {'✓' if has_anthropic else '✗'}")

    if not (has_openrouter or has_anthropic):
        print("\nNo API keys found!")
        print("\nRecommended:")
        print("  1. Get an OpenRouter API key: https://openrouter.ai/keys")
        print("  2. export OPENROUTER_API_KEY='sk-or-v1-...'")
        print("\nAlternative:")
        print("  export ANTHROPIC_API_KEY='your-key'")
        return

    # Run demos
    results = []
    results.append(("Basic Async Completion", await demo_basic_async_completion()))
    results.append(("Async Streaming", await demo_async_streaming()))
    results.append(("Concurrent Requests", await demo_concurrent_requests()))
    results.append(("Request Cancellation", await demo_cancellation()))
    results.append(("Custom Cancellation Callback", await demo_custom_cancellation_callback()))
    results.append(("Streaming with Cancellation", await demo_streaming_with_cancellation()))

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, success in results:
        status = "✓ PASS" if success else "✗ SKIP/FAIL"
        print(f"{name}: {status}")

    if has_openrouter:
        print("\n💡 You're using OpenRouter - great choice!")
        print("   Access Claude, GPT-4, Gemini with one key.")


if __name__ == "__main__":
    asyncio.run(main())
