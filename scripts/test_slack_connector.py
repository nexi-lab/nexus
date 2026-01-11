#!/usr/bin/env python3
"""Test script for Slack connector with real Slack workspace.

This script demonstrates how to:
1. Set up OAuth credentials
2. Initialize the Slack connector
3. List channels
4. Read messages
5. Post messages

Prerequisites:
    1. Create a Slack app at https://api.slack.com/apps
    2. Configure OAuth & Permissions:
       - Add redirect URL: http://localhost:5173/oauth/callback
       - Add scopes: channels:read, channels:history, chat:write, users:read
    3. Install app to your workspace
    4. Set environment variables:
       export NEXUS_OAUTH_SLACK_CLIENT_ID="your-client-id"
       export NEXUS_OAUTH_SLACK_CLIENT_SECRET="your-client-secret"
       export SLACK_USER_EMAIL="your@email.com"
       export SLACK_TOKEN="xoxp-your-user-token"  # For quick testing

Usage:
    # Run all tests
    python scripts/test_slack_connector.py

    # Run specific test
    python scripts/test_slack_connector.py --test list_channels
    python scripts/test_slack_connector.py --test read_messages
    python scripts/test_slack_connector.py --test post_message
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))


def setup_test_environment():
    """Set up test environment and check prerequisites."""
    print("=" * 80)
    print("SLACK CONNECTOR TEST SETUP")
    print("=" * 80)

    # Check environment variables
    required_vars = {
        "NEXUS_OAUTH_SLACK_CLIENT_ID": "Slack app client ID",
        "NEXUS_OAUTH_SLACK_CLIENT_SECRET": "Slack app client secret",
    }

    missing = []
    for var, description in required_vars.items():
        value = os.getenv(var)
        if value:
            print(f"✓ {var}: {value[:10]}...")
        else:
            print(f"✗ {var}: Not set ({description})")
            missing.append(var)

    # Check optional quick-test token
    test_token = os.getenv("SLACK_TOKEN")
    if test_token:
        print(f"✓ SLACK_TOKEN: {test_token[:10]}... (for quick testing)")
    else:
        print("✗ SLACK_TOKEN: Not set (optional, for quick API testing)")

    user_email = os.getenv("SLACK_USER_EMAIL", "test@example.com")
    print(f"✓ User email: {user_email}")

    if missing:
        print("\n⚠️  Missing required environment variables!")
        print("\nTo set them:")
        for var in missing:
            print(f"  export {var}='your-value'")
        print("\nOr use SLACK_TOKEN for quick testing:")
        print("  export SLACK_TOKEN='xoxp-your-token'")
        return False

    print("\n✓ All prerequisites met!")
    return True


def test_slack_client_quick():
    """Quick test using slack-sdk directly with a token."""
    print("\n" + "=" * 80)
    print("QUICK TEST: Slack SDK Direct")
    print("=" * 80)

    token = os.getenv("SLACK_TOKEN")
    if not token:
        print("⚠️  SLACK_TOKEN not set. Skipping quick test.")
        print("   Get a token from: https://api.slack.com/apps -> OAuth & Permissions")
        return False

    try:
        from slack_sdk import WebClient
    except ImportError:
        print("❌ slack-sdk not installed. Install with: pip install slack-sdk")
        return False

    try:
        client = WebClient(token=token)

        # Test auth
        print("\n1. Testing authentication...")
        auth_result = client.auth_test()
        if auth_result.get("ok"):
            print(f"   ✓ Authenticated as: {auth_result['user']}")
            print(f"   ✓ Team: {auth_result['team']}")
        else:
            print(f"   ✗ Auth failed: {auth_result.get('error')}")
            return False

        # List channels
        print("\n2. Listing channels...")
        channels_result = client.conversations_list(types="public_channel,private_channel", limit=5)
        if channels_result.get("ok"):
            channels = channels_result.get("channels", [])
            print(f"   ✓ Found {len(channels)} channels:")
            for channel in channels[:5]:
                print(f"      - #{channel['name']} ({channel['id']})")
        else:
            print(f"   ✗ Failed: {channels_result.get('error')}")

        # Get recent messages from first channel
        if channels:
            channel = channels[0]
            print(f"\n3. Reading messages from #{channel['name']}...")
            messages_result = client.conversations_history(channel=channel["id"], limit=3)
            if messages_result.get("ok"):
                messages = messages_result.get("messages", [])
                print(f"   ✓ Found {len(messages)} recent messages:")
                for msg in messages[:3]:
                    text = msg.get("text", "")[:50]
                    ts = msg.get("ts", "")
                    print(f"      - [{ts}] {text}...")
            else:
                print(f"   ✗ Failed: {messages_result.get('error')}")

        print("\n✅ Quick test passed!")
        return True

    except Exception as e:
        print(f"❌ Quick test failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_slack_connector_initialization():
    """Test Slack connector initialization."""
    print("\n" + "=" * 80)
    print("TEST 1: Slack Connector Initialization")
    print("=" * 80)

    try:
        from nexus.backends.slack_connector import SlackConnectorBackend

        # Initialize connector
        db_path = os.path.expanduser("~/.nexus/nexus.db")
        user_email = os.getenv("SLACK_USER_EMAIL", "test@example.com")

        print("\nInitializing connector:")
        print(f"  - Database: {db_path}")
        print(f"  - User: {user_email}")

        connector = SlackConnectorBackend(
            token_manager_db=db_path,
            user_email=user_email,
            provider="slack",
            max_messages_per_channel=10,
        )

        print("\n✓ Connector initialized:")
        print(f"  - Name: {connector.name}")
        print(f"  - User scoped: {connector.user_scoped}")
        print(f"  - Provider: {connector.provider}")

        return connector

    except Exception as e:
        print(f"❌ Initialization failed: {e}")
        import traceback

        traceback.print_exc()
        return None


def test_list_channels(connector):
    """Test listing channels through connector."""
    print("\n" + "=" * 80)
    print("TEST 2: List Channels")
    print("=" * 80)

    if not connector:
        print("⚠️  Connector not initialized. Skipping.")
        return False

    try:
        from nexus.core.permissions import OperationContext

        # Create context
        user_email = os.getenv("SLACK_USER_EMAIL", "test@example.com")
        context = OperationContext(user=user_email, groups=[])

        # List root directory (should show channel types)
        print("\n1. Listing root directory...")
        root_dirs = connector.list_dir("", context)
        print(f"   ✓ Found {len(root_dirs)} folder types:")
        for dir_name in root_dirs:
            print(f"      - {dir_name}")

        # List channels folder
        print("\n2. Listing public channels...")
        channels = connector.list_dir("channels", context)
        print(f"   ✓ Found {len(channels)} channels:")
        for channel in channels[:10]:
            print(f"      - {channel}")

        # List private channels (if any)
        print("\n3. Listing private channels...")
        try:
            private_channels = connector.list_dir("private-channels", context)
            print(f"   ✓ Found {len(private_channels)} private channels:")
            for channel in private_channels[:5]:
                print(f"      - {channel}")
        except Exception as e:
            print(f"   ℹ️  No private channels or permission denied: {e}")

        print("\n✅ Channel listing test passed!")
        return True

    except Exception as e:
        print(f"❌ Channel listing failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_read_messages(connector):
    """Test reading messages through connector."""
    print("\n" + "=" * 80)
    print("TEST 3: Read Messages")
    print("=" * 80)

    if not connector:
        print("⚠️  Connector not initialized. Skipping.")
        return False

    try:
        from nexus.core.permissions import OperationContext

        # Create context
        user_email = os.getenv("SLACK_USER_EMAIL", "test@example.com")
        context = OperationContext(user=user_email, groups=[])

        # Get first channel
        channels = connector.list_dir("channels", context)
        if not channels:
            print("⚠️  No channels found. Skipping message test.")
            return False

        # Get first channel name (remove trailing /)
        channel_name = channels[0].rstrip("/")
        print(f"\n1. Testing channel: {channel_name}")

        # List messages in channel
        print(f"\n2. Listing messages in #{channel_name}...")
        messages = connector.list_dir(f"channels/{channel_name}", context)
        print(f"   ✓ Found {len(messages)} messages:")
        for msg_file in messages[:5]:
            print(f"      - {msg_file}")

        if not messages:
            print("   ℹ️  No messages in this channel")
            return True

        # Read first message
        first_message = messages[0]
        message_path = f"channels/{channel_name}/{first_message}"
        print(f"\n3. Reading message: {first_message}")

        # Set backend_path for reading
        context.backend_path = message_path

        # Read message content
        content = connector.read_content("", context)
        message_data = json.loads(content.decode("utf-8"))

        print("   ✓ Message data:")
        print(f"      - Type: {message_data.get('type')}")
        print(f"      - User: {message_data.get('user')}")
        print(f"      - Timestamp: {message_data.get('ts')}")
        print(f"      - Text: {message_data.get('text', '')[:100]}...")

        print("\n✅ Message reading test passed!")
        return True

    except Exception as e:
        print(f"❌ Message reading failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_post_message(connector):
    """Test posting a message through connector."""
    print("\n" + "=" * 80)
    print("TEST 4: Post Message")
    print("=" * 80)

    if not connector:
        print("⚠️  Connector not initialized. Skipping.")
        return False

    # Ask for confirmation
    print("\n⚠️  This will post a test message to your Slack workspace.")
    response = input("   Continue? (y/N): ")
    if response.lower() != "y":
        print("   Skipped.")
        return True

    try:
        from nexus.core.permissions import OperationContext

        # Create context
        user_email = os.getenv("SLACK_USER_EMAIL", "test@example.com")
        context = OperationContext(user=user_email, groups=[])

        # Get first channel
        channels = connector.list_dir("channels", context)
        if not channels:
            print("⚠️  No channels found. Cannot post message.")
            return False

        # Get channel ID from first channel
        channel_name = channels[0].rstrip("/")
        print(f"\n1. Target channel: #{channel_name}")

        # Get channel info to find ID
        channel_info = connector._get_channel_by_name(channel_name, context)
        if not channel_info:
            print(f"   ✗ Could not find channel info for {channel_name}")
            return False

        channel_id = channel_info["id"]
        print(f"   ✓ Channel ID: {channel_id}")

        # Create message
        message_data = {
            "channel": channel_id,
            "text": "🤖 Test message from Nexus Slack connector! "
            "(Testing connector implementation)",
        }

        # Set backend_path
        context.backend_path = f"channels/{channel_name}/test-message.json"

        # Post message
        print("\n2. Posting message...")
        content = json.dumps(message_data).encode("utf-8")
        result_ts = connector.write_content(content, context)

        print("   ✓ Message posted successfully!")
        print(f"   ✓ Message timestamp: {result_ts}")

        print("\n✅ Message posting test passed!")
        return True

    except Exception as e:
        print(f"❌ Message posting failed: {e}")
        import traceback

        traceback.print_exc()
        return False


def main():
    """Main test runner."""
    parser = argparse.ArgumentParser(description="Test Slack connector")
    parser.add_argument(
        "--test",
        choices=["quick", "init", "list_channels", "read_messages", "post_message", "all"],
        default="all",
        help="Which test to run",
    )
    args = parser.parse_args()

    # Setup
    if not setup_test_environment():
        print("\n❌ Setup failed. Please configure environment variables.")
        sys.exit(1)

    results = {}

    # Quick test with SDK
    if args.test in ["quick", "all"]:
        results["quick"] = test_slack_client_quick()

    # Initialize connector
    connector = None
    if args.test in ["init", "list_channels", "read_messages", "post_message", "all"]:
        connector = test_slack_connector_initialization()
        results["init"] = connector is not None

    # Run tests
    if args.test in ["list_channels", "all"] and connector:
        results["list_channels"] = test_list_channels(connector)

    if args.test in ["read_messages", "all"] and connector:
        results["read_messages"] = test_read_messages(connector)

    if args.test in ["post_message", "all"] and connector:
        results["post_message"] = test_post_message(connector)

    # Summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)

    for test_name, passed in results.items():
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"{status}: {test_name}")

    all_passed = all(results.values())
    if all_passed:
        print("\n🎉 All tests passed!")
        sys.exit(0)
    else:
        print("\n⚠️  Some tests failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
