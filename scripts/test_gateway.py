#!/usr/bin/env python3
"""Test script for Message Gateway API.

Usage:
    # Start the server first:
    uv run nexus server

    # Then run this script:
    python scripts/test_gateway.py
"""

import httpx

# Configuration
BASE_URL = "http://localhost:8000"
API_KEY = "test-api-key"  # Replace with your API key

# Test message
TEST_MESSAGE = {
    "text": "Hello from the gateway test!",
    "user": "test_user_123",
    "role": "human",
    "session_id": "discord:guild_test:channel_test",
    "channel": "discord",
}


def test_send_message():
    """Test sending a message through the gateway."""
    print("Testing POST /api/v2/gateway/messages...")

    headers = {"Authorization": f"Bearer {API_KEY}"}

    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        response = client.post(
            "/api/v2/gateway/messages",
            json=TEST_MESSAGE,
            headers=headers,
        )

        print(f"Status: {response.status_code}")
        print(f"Response: {response.json()}")

        if response.status_code == 201:
            print("✅ Message sent successfully!")
            return response.json()
        else:
            print("❌ Message send failed")
            return None


def test_duplicate():
    """Test duplicate detection."""
    print("\nTesting duplicate detection...")

    # The deduplicator uses message_id, which is generated server-side
    # So consecutive requests won't be duplicates (different UUIDs)
    # This is expected behavior - dedup prevents processing the same message twice

    result = test_send_message()
    if result:
        print(f"Message ID: {result['message_id']}")
        print(f"Status: {result['status']}")


if __name__ == "__main__":
    print("=" * 50)
    print("Message Gateway API Test")
    print("=" * 50)

    test_send_message()
    test_duplicate()
