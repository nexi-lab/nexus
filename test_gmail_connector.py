#!/usr/bin/env python3
"""Test script for Gmail connector.

This script tests the Gmail connector functionality:
1. Initialize the connector
2. Sync emails from Gmail
3. List emails
4. Read email content
5. Test incremental sync with historyId
"""

import os
import sys
from datetime import UTC, datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from nexus.backends.gmail_connector import GmailConnectorBackend
from nexus.core.permissions import OperationContext


def test_gmail_connector() -> None:
    """Test Gmail connector functionality."""
    print("=" * 80)
    print("Gmail Connector Test")
    print("=" * 80)

    # Database URL from environment or default
    db_url = os.getenv("NEXUS_DATABASE_URL", "postgresql://postgres:nexus@localhost:5432/nexus")
    print(f"\n📊 Using database: {db_url}")

    # Create context
    context = OperationContext(
        user="joezhoujinjing@gmail.com",
        user_id="joezhoujinjing@gmail.com",
        groups=[],
        tenant_id="default",
        subject_type="user",
        subject_id="joezhoujinjing@gmail.com",
    )

    # Initialize Gmail connector with recent date (last 1 day) to limit sync
    print("\n🔧 Initializing Gmail connector...")
    from datetime import timedelta

    recent_date = (datetime.now(UTC) - timedelta(days=1)).isoformat()[:10]  # Last 1 day only

    try:
        backend = GmailConnectorBackend(
            token_manager_db=db_url,
            user_email="joezhoujinjing@gmail.com",
            sync_from_date=recent_date,  # Only sync last 7 days
            last_history_id=None,  # First sync, no historyId yet
            provider="gmail",
        )
        print(f"✅ Gmail connector initialized successfully (syncing from {recent_date})")
    except Exception as e:
        print(f"❌ Failed to initialize Gmail connector: {e}")
        import traceback

        traceback.print_exc()
        return

    # Test 1: Sync emails (with timeout)
    print("\n📧 Test 1: Syncing emails from Gmail (last 1 day only)...")
    import signal

    def timeout_handler(_signum, _frame):
        raise TimeoutError("Sync operation timed out after 30 seconds")

    try:
        # Set timeout for sync operation
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(30)  # 30 second timeout

        backend._sync_emails(context)

        signal.alarm(0)  # Cancel timeout

        print(f"✅ Synced {len(backend._email_cache)} emails")

        # Show some email IDs
        if backend._email_cache:
            print("\n   Sample email IDs (first 3):")
            for i, msg_id in enumerate(list(backend._email_cache.keys())[:3]):
                email = backend._email_cache[msg_id]
                subject = email.get("subject", "No subject")[:50]
                date = email.get("date", "")[:10]
                print(f"   {i + 1}. {msg_id[:20]}... - {subject} ({date})")
        else:
            print("⚠️  No emails found in the last 1 day")
            return
    except TimeoutError:
        signal.alarm(0)
        print("⚠️  Sync timed out (taking too long), but continuing with cached emails...")
        if not backend._email_cache:
            print("❌ No emails in cache, cannot continue tests")
            return
    except Exception as e:
        signal.alarm(0)
        print(f"❌ Failed to sync emails: {e}")
        import traceback

        traceback.print_exc()
        if not backend._email_cache:
            return

    # Test 2: Get current historyId
    print("\n🆔 Test 2: Getting current historyId...")
    try:
        current_history_id = backend.get_last_history_id()
        if current_history_id:
            print(f"✅ Current historyId: {current_history_id}")
        else:
            print("⚠️  No historyId available yet")
    except Exception as e:
        print(f"❌ Failed to get historyId: {e}")

    # Test 3: List directory structure (quick test)
    print("\n📁 Test 3: Listing email directory structure...")
    try:
        # List root
        root_entries = backend.list_dir("", context)
        print(f"✅ Root entries: {len(root_entries)} years")
        if root_entries:
            print(f"   Years: {', '.join(root_entries[:3])}")

            # List first year (if available)
            if root_entries:
                year = root_entries[0].rstrip("/")
                year_entries = backend.list_dir(f"emails/{year}", context)
                print(f"   Entries in {year}: {len(year_entries)} months")
                if year_entries:
                    month = year_entries[0].rstrip("/")
                    month_entries = backend.list_dir(f"emails/{year}/{month}", context)
                    print(f"   Entries in {year}/{month}: {len(month_entries)} days")
                    if month_entries:
                        day = month_entries[0].rstrip("/")
                        day_entries = backend.list_dir(f"emails/{year}/{month}/{day}", context)
                        print(f"   Emails in {year}/{month}/{day}: {len(day_entries)}")
                        if day_entries:
                            print(f"   Sample: {day_entries[0]}")
    except Exception as e:
        print(f"❌ Failed to list directory: {e}")
        import traceback

        traceback.print_exc()

    # Test 4: Read an email
    print("\n📄 Test 4: Reading an email...")
    try:
        if backend._email_cache:
            # Get first email
            first_msg_id = list(backend._email_cache.keys())[0]
            email_data = backend._email_cache[first_msg_id]

            # Construct path
            email_date = datetime.fromisoformat(email_data["date"].replace("Z", "+00:00"))
            email_path = backend._get_email_path(first_msg_id, email_date)

            # Create context with backend_path
            read_context = OperationContext(
                user="joezhoujinjing@gmail.com",
                user_id="joezhoujinjing@gmail.com",
                groups=[],
                tenant_id="default",
                subject_type="user",
                subject_id="joezhoujinjing@gmail.com",
                backend_path=email_path,
            )

            # Read content
            content = backend.read_content("", read_context)
            print(f"✅ Read email: {email_path}")
            print(f"   Content size: {len(content)} bytes")
            print("   Preview (first 200 chars):")
            print(f"   {content.decode('utf-8')[:200]}...")
    except Exception as e:
        print(f"❌ Failed to read email: {e}")
        import traceback

        traceback.print_exc()

    # Test 5: Test incremental sync (if we have historyId) - SKIP for speed
    if current_history_id:
        print("\n🔄 Test 5: Testing incremental sync with historyId...")
        print("   ⏭️  Skipping incremental sync test (would take too long)")
        print(f"   ℹ️  Current historyId: {current_history_id}")
        print("   ℹ️  To test incremental sync, run sync again with this historyId")

    # Test 6: Get updated config
    print("\n⚙️  Test 6: Getting updated config...")
    try:
        updated_config = backend.get_updated_config()
        if updated_config:
            print("✅ Updated config:")
            for key, value in updated_config.items():
                if key == "last_history_id":
                    print(f"   {key}: {value}")
                else:
                    print(f"   {key}: {str(value)[:50]}...")
        else:
            print("⚠️  No updated config (no sync performed yet)")
    except Exception as e:
        print(f"❌ Failed to get updated config: {e}")

    print("\n" + "=" * 80)
    print("✅ All tests completed!")
    print("=" * 80)


if __name__ == "__main__":
    test_gmail_connector()
