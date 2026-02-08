#!/usr/bin/env python3
"""Test script for Discord Adapter.

Prerequisites:
    1. Install discord.py: pip install discord.py
    2. Set DISCORD_BOT_TOKEN environment variable
    3. Bot must have MESSAGE_CONTENT intent enabled in Discord Developer Portal

Usage:
    export DISCORD_BOT_TOKEN="your-bot-token"
    python scripts/test_discord_adapter.py
"""

import asyncio
import os
import sys


async def main():
    """Run Discord adapter test."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("Error: DISCORD_BOT_TOKEN environment variable not set")
        print("\nTo get your bot token:")
        print("1. Go to https://discord.com/developers/applications")
        print("2. Select your app")
        print("3. Go to Bot section")
        print("4. Click 'Reset Token' to get a new token")
        print("5. Export it: export DISCORD_BOT_TOKEN='your-token-here'")
        sys.exit(1)

    # Import here to catch ImportError
    try:
        from nexus.message_gateway.adapters.discord import DiscordAdapter
    except ImportError as e:
        print(f"Import error: {e}")
        print("\nMake sure you're running from the nexus directory with:")
        print("  PYTHONPATH=src:$PYTHONPATH python scripts/test_discord_adapter.py")
        sys.exit(1)

    # For testing, we'll create a mock NexusFS and context
    # In production, these would come from the app state
    print("Creating mock NexusFS for testing...")

    class MockNexusFS:
        """Mock NexusFS for testing."""

        def append(self, path: str, content: str, context=None) -> dict:
            print(f"[MockNexusFS] append({path})")
            print(f"  Content: {content[:100]}...")
            return {"path": path, "size": len(content)}

        def read(self, path: str, context=None) -> bytes:
            return b""

        def exists(self, path: str, context=None) -> bool:
            return False

    class MockContext:
        """Mock operation context."""

        pass

    mock_fs = MockNexusFS()
    mock_context = MockContext()

    print("\nStarting Discord adapter...")
    print("Bot will connect and log received messages.")
    print("Press Ctrl+C to stop.\n")

    adapter = DiscordAdapter(
        token=token,
        nexus_fs=mock_fs,
        context=mock_context,
    )

    try:
        await adapter.start()
    except KeyboardInterrupt:
        print("\nStopping adapter...")
        await adapter.stop()
    except Exception as e:
        print(f"\nError: {e}")
        await adapter.stop()


if __name__ == "__main__":
    asyncio.run(main())
