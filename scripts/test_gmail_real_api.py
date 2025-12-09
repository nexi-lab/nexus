#!/usr/bin/env python3
"""
Real Gmail API testing script that fetches OAuth tokens from docker postgres.

This script tests the Gmail connector's recursive multipart message parsing
with real Gmail API calls using actual OAuth tokens stored in the database.

Usage:
    python scripts/test_gmail_real_api.py [--user EMAIL] [--max-messages N]

Prerequisites:
    - Docker postgres container running with nexus database
    - OAuth tokens stored in database for the specified user
    - Gmail API credentials configured
"""

import argparse
import json
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))


def get_db_url_from_docker():
    """Get postgres connection URL from docker container."""
    import subprocess

    try:
        # Get postgres container name
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=postgres", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        container_name = result.stdout.strip().split("\n")[0]
        if not container_name:
            raise RuntimeError("No postgres container found")

        print(f"Found postgres container: {container_name}")

        # Get postgres port
        result = subprocess.run(
            [
                "docker",
                "inspect",
                container_name,
                "--format",
                '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        port = result.stdout.strip()

        # Get postgres credentials from environment
        result = subprocess.run(
            ["docker", "inspect", container_name, "--format", "{{json .Config.Env}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        env_vars = json.loads(result.stdout.strip())

        postgres_user = "nexus"
        postgres_password = "nexus"
        postgres_db = "nexus"

        for env_var in env_vars:
            if env_var.startswith("POSTGRES_USER="):
                postgres_user = env_var.split("=", 1)[1]
            elif env_var.startswith("POSTGRES_PASSWORD="):
                postgres_password = env_var.split("=", 1)[1]
            elif env_var.startswith("POSTGRES_DB="):
                postgres_db = env_var.split("=", 1)[1]

        db_url = f"postgresql://{postgres_user}:{postgres_password}@localhost:{port}/{postgres_db}"
        print(f"Database URL: {db_url.replace(postgres_password, '***')}")
        return db_url

    except Exception as e:
        print(f"Error getting docker postgres URL: {e}")
        raise


def test_gmail_multipart_parsing(user_email: str, max_messages: int = 5):
    """Test Gmail connector with real API calls."""
    from nexus.backends.gmail_connector import GmailConnectorBackend
    from nexus.core.permissions import OperationContext

    try:
        # Get database URL from docker
        db_url = get_db_url_from_docker()

        # Initialize Gmail connector
        print("\n=== Initializing Gmail Connector ===")
        print(f"User: {user_email}")
        print(f"Max messages: {max_messages}")

        backend = GmailConnectorBackend(
            token_manager_db=db_url,
            user_email=user_email,
            provider="gmail",
            max_message_per_label=max_messages,
        )

        # Create operation context
        context = OperationContext(user=user_email, groups=[], tenant_id="default", backend_path="")

        # Get Gmail service
        print("\n=== Fetching OAuth Tokens ===")
        service = backend._get_gmail_service(context)
        print("✓ Successfully authenticated with Gmail API")

        # List messages from INBOX
        print(f"\n=== Listing up to {max_messages} messages from INBOX ===")
        response = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_messages)
            .execute()
        )

        messages = response.get("messages", [])
        if not messages:
            print("No messages found in INBOX")
            return

        print(f"Found {len(messages)} messages\n")

        # Test each message
        stats = {
            "total": len(messages),
            "simple": 0,
            "multipart": 0,
            "nested_multipart": 0,
            "with_text": 0,
            "with_html": 0,
            "with_both": 0,
        }

        for i, msg in enumerate(messages, 1):
            msg_id = msg["id"]
            print(f"\n{'=' * 80}")
            print(f"Message {i}/{len(messages)}: {msg_id}")
            print(f"{'=' * 80}")

            # Fetch full message
            message = (
                service.users().messages().get(userId="me", id=msg_id, format="full").execute()
            )

            # Parse with our method
            email_data = backend._parse_gmail_message(message)

            # Display message details
            print(f"From: {email_data['from']}")
            print(f"To: {email_data['to']}")
            print(f"Subject: {email_data['subject']}")
            print(f"Date: {email_data['date']}")
            print(f"Labels: {', '.join(email_data['labelIds'])}")
            print(f"Snippet: {email_data['snippet'][:100]}...")

            # Analyze structure
            payload = message.get("payload", {})
            has_parts = "parts" in payload
            parts = payload.get("parts", [])

            print("\n--- Structure Analysis ---")
            print(f"Has multipart: {has_parts}")
            if has_parts:
                print(f"Number of parts: {len(parts)}")

                # Check for nested multipart
                def has_nested_parts(parts_list):
                    return any("parts" in part for part in parts_list)

                is_nested = has_nested_parts(parts)
                print(f"Has nested multipart: {is_nested}")

                if is_nested:
                    stats["nested_multipart"] += 1
                    print("🎯 NESTED MULTIPART MESSAGE (Testing new functionality!)")
                else:
                    stats["multipart"] += 1
            else:
                stats["simple"] += 1
                print("Simple non-multipart message")

            # Display body content
            body_text = email_data["body_text"]
            body_html = email_data["body_html"]

            print("\n--- Body Content ---")
            if body_text:
                stats["with_text"] += 1
                print(f"Plain text: {len(body_text)} chars")
                print(f"Preview: {body_text[:200]}...")
            else:
                print("Plain text: None")

            if body_html:
                stats["with_html"] += 1
                print(f"HTML: {len(body_html)} chars")
                print(f"Preview: {body_html[:200]}...")
            else:
                print("HTML: None")

            if body_text and body_html:
                stats["with_both"] += 1

            # Verify we got content (not just snippet)
            if body_text:
                # Check if body is longer than snippet (indicates full content)
                snippet = email_data["snippet"]
                if len(body_text) > len(snippet):
                    print(
                        f"✓ Full body retrieved (body: {len(body_text)} > snippet: {len(snippet)})"
                    )
                else:
                    print(
                        f"⚠ Body might be truncated (body: {len(body_text)} ≈ snippet: {len(snippet)})"
                    )

        # Print summary statistics
        print(f"\n{'=' * 80}")
        print("=== SUMMARY STATISTICS ===")
        print(f"{'=' * 80}")
        print(f"Total messages analyzed: {stats['total']}")
        print("\nMessage structure:")
        print(f"  Simple (non-multipart): {stats['simple']}")
        print(f"  Multipart (flat): {stats['multipart']}")
        print(f"  Nested multipart: {stats['nested_multipart']} 🎯")
        print("\nBody content:")
        print(f"  With plain text: {stats['with_text']}")
        print(f"  With HTML: {stats['with_html']}")
        print(f"  With both: {stats['with_both']}")

        if stats["nested_multipart"] > 0:
            print(f"\n✅ SUCCESS: Found {stats['nested_multipart']} nested multipart messages!")
            print(
                "   The new recursive parsing functionality is being tested with real Gmail messages."
            )
        else:
            print(
                "\n⚠ Note: No nested multipart messages found in this sample. Try with more messages."
            )

        return stats

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
        raise


def main():
    parser = argparse.ArgumentParser(description="Test Gmail connector with real API")
    parser.add_argument("--user", type=str, help="Gmail user email", required=True)
    parser.add_argument(
        "--max-messages", type=int, default=10, help="Maximum messages to fetch (default: 10)"
    )

    args = parser.parse_args()

    print("=" * 80)
    print("Gmail Connector Real API Test")
    print("Testing recursive multipart message parsing with real Gmail API")
    print("=" * 80)

    try:
        test_gmail_multipart_parsing(args.user, args.max_messages)
        print("\n✅ Test completed successfully!")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
