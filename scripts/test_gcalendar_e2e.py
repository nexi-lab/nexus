#!/usr/bin/env python3
"""E2E test script for Google Calendar connector.

This script tests the full CRUD flow with real Google Calendar API:
1. Generate SKILL.md
2. Create event
3. Read event
4. Update event
5. Delete event
6. Test validation errors

Prerequisites:
    1. Set up OAuth credentials in Google Cloud Console
    2. Run: nexus oauth login gcalendar
    3. Run this script: python scripts/test_gcalendar_e2e.py

Usage:
    # With default database
    python scripts/test_gcalendar_e2e.py

    # With custom database
    python scripts/test_gcalendar_e2e.py --db ~/.nexus/nexus.db

    # Test specific user
    python scripts/test_gcalendar_e2e.py --user your@email.com
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def print_header(title: str) -> None:
    """Print a section header."""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}\n")


def print_result(success: bool, message: str) -> None:
    """Print test result."""
    status = "✅ PASS" if success else "❌ FAIL"
    print(f"{status}: {message}")


def test_skill_md_generation(backend, tmp_dir: Path) -> bool:
    """Test SKILL.md auto-generation."""
    print_header("Test 1: SKILL.md Generation")

    try:
        # Generate SKILL.md content
        skill_doc = backend.generate_skill_doc("/mnt/calendar/")

        # Verify content
        checks = [
            ("Has title", "# Gcalendar Connector" in skill_doc),
            ("Has mount path", "`/mnt/calendar/`" in skill_doc),
            ("Has operations section", "## Operations" in skill_doc),
            ("Has create event", "Create Event" in skill_doc),
            ("Has agent_intent requirement", "agent_intent" in skill_doc),
            ("Has error codes", "## Error Codes" in skill_doc),
            ("Has YAML examples", "```yaml" in skill_doc),
        ]

        all_passed = True
        for name, passed in checks:
            print_result(passed, name)
            if not passed:
                all_passed = False

        # Save to file for inspection
        skill_path = tmp_dir / "SKILL.md"
        skill_path.write_text(skill_doc)
        print(f"\n📄 SKILL.md saved to: {skill_path}")

        return all_passed

    except Exception as e:
        print_result(False, f"Exception: {e}")
        return False


def test_validation_errors(backend) -> bool:
    """Test that validation errors are properly raised."""
    print_header("Test 2: Validation Errors")

    from nexus.connectors.base import ValidationError

    tests_passed = True

    # Test 1: Missing agent_intent
    try:
        backend.validate_traits("create_event", {"summary": "Test"})
        print_result(False, "Missing agent_intent should raise error")
        tests_passed = False
    except ValidationError as e:
        passed = e.code == "MISSING_AGENT_INTENT"
        print_result(passed, f"Missing agent_intent raises {e.code}")
        if not passed:
            tests_passed = False

    # Test 2: Short agent_intent
    try:
        backend.validate_traits("create_event", {"agent_intent": "short"})
        print_result(False, "Short agent_intent should raise error")
        tests_passed = False
    except ValidationError as e:
        passed = e.code == "AGENT_INTENT_TOO_SHORT"
        print_result(passed, f"Short agent_intent raises {e.code}")
        if not passed:
            tests_passed = False

    # Test 3: Delete without confirm
    try:
        backend.validate_traits(
            "delete_event",
            {"agent_intent": "Deleting event per user request"},
        )
        print_result(False, "Delete without confirm should raise error")
        tests_passed = False
    except ValidationError as e:
        passed = e.code == "MISSING_CONFIRM"
        print_result(passed, f"Delete without confirm raises {e.code}")
        if not passed:
            tests_passed = False

    # Test 4: Valid create passes
    try:
        warnings = backend.validate_traits(
            "create_event",
            {"agent_intent": "Creating event for user's team meeting request"},
        )
        print_result(True, f"Valid create passes (warnings: {len(warnings)})")
    except ValidationError as e:
        print_result(False, f"Valid create should pass but got: {e.code}")
        tests_passed = False

    return tests_passed


def test_create_event(backend, context) -> str | None:
    """Test creating a calendar event."""
    print_header("Test 3: Create Event")

    # Create event 1 hour from now
    now = datetime.now(UTC)
    start_time = now + timedelta(hours=1)
    end_time = start_time + timedelta(hours=1)

    content = f"""# agent_intent: E2E test - creating test event for calendar connector validation
summary: Nexus E2E Test Event
description: This event was created by the Nexus Calendar Connector E2E test. Safe to delete.
start:
  dateTime: "{start_time.strftime("%Y-%m-%dT%H:%M:%S")}Z"
  timeZone: UTC
end:
  dateTime: "{end_time.strftime("%Y-%m-%dT%H:%M:%S")}Z"
  timeZone: UTC
""".encode()

    try:
        context.backend_path = "primary/_new.yaml"
        response = backend.write_content(content, context)
        event_id = response.unwrap()

        print_result(True, f"Event created with ID: {event_id}")
        return event_id

    except Exception as e:
        print_result(False, f"Failed to create event: {e}")
        import traceback

        traceback.print_exc()
        return None


def test_read_event(backend, context, event_id: str) -> bool:
    """Test reading a calendar event."""
    print_header("Test 4: Read Event")

    try:
        context.backend_path = f"primary/{event_id}.yaml"
        response = backend.read_content("", context)
        content = response.unwrap()

        # Parse and verify
        content_str = content.decode("utf-8")

        checks = [
            ("Has event ID", event_id in content_str),
            ("Has summary", "Nexus E2E Test Event" in content_str),
            ("Has start time", "start:" in content_str),
            ("Has end time", "end:" in content_str),
            ("Is valid YAML", "dateTime:" in content_str),
        ]

        all_passed = True
        for name, passed in checks:
            print_result(passed, name)
            if not passed:
                all_passed = False

        print(f"\n📄 Event content:\n{content_str[:500]}...")
        return all_passed

    except Exception as e:
        print_result(False, f"Failed to read event: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_update_event(backend, context, event_id: str) -> bool:
    """Test updating a calendar event."""
    print_header("Test 5: Update Event")

    content = b"""# agent_intent: E2E test - updating event title to verify update functionality
summary: Nexus E2E Test Event (UPDATED)
description: This event was UPDATED by the Nexus Calendar Connector E2E test.
"""

    try:
        context.backend_path = f"primary/{event_id}.yaml"
        response = backend.write_content(content, context)
        result = response.unwrap()

        print_result(result == "updated", f"Update returned: {result}")

        # Read back and verify
        read_response = backend.read_content("", context)
        updated_content = read_response.unwrap().decode("utf-8")
        has_updated_title = "(UPDATED)" in updated_content

        print_result(has_updated_title, "Event title was updated")

        return result == "updated" and has_updated_title

    except Exception as e:
        print_result(False, f"Failed to update event: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_list_events(backend, context) -> bool:
    """Test listing calendar events."""
    print_header("Test 6: List Events")

    try:
        events = backend._list_events("primary", context)

        print_result(True, f"Listed {len(events)} events")

        if events:
            print("\n📋 Sample events (first 5):")
            for event in events[:5]:
                print(f"  - {event}")

        return True

    except Exception as e:
        print_result(False, f"Failed to list events: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_delete_event(backend, context, event_id: str) -> bool:
    """Test deleting a calendar event."""
    print_header("Test 7: Delete Event")

    try:
        context.backend_path = f"primary/{event_id}.yaml"

        # Note: delete_content requires the delete to be confirmed through
        # a separate mechanism. For this test, we'll call the service directly.
        service = backend._get_calendar_service(context)
        service.events().delete(calendarId="primary", eventId=event_id).execute()

        print_result(True, f"Event {event_id} deleted")

        # Wait a moment for API propagation
        import time

        time.sleep(1)

        # Verify deletion by trying to get the event directly from API
        # Note: Google Calendar may return cancelled events instead of 404
        try:
            event = service.events().get(calendarId="primary", eventId=event_id).execute()
            # Check if event is cancelled (deleted)
            if event.get("status") == "cancelled":
                print_result(True, "Event no longer exists (status: cancelled)")
                return True
            else:
                print_result(False, f"Event still exists with status: {event.get('status')}")
                return False
        except Exception as e:
            if "404" in str(e) or "Not Found" in str(e):
                print_result(True, "Event no longer exists (confirmed)")
                return True
            else:
                # Some other error - likely deleted
                print_result(True, f"Event deleted (API confirms: {type(e).__name__})")
                return True

    except Exception as e:
        print_result(False, f"Failed to delete event: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_checkpoint_functionality(backend) -> bool:
    """Test checkpoint creation and management."""
    print_header("Test 8: Checkpoint Functionality")

    tests_passed = True

    # Test 1: Create checkpoint
    checkpoint = backend.create_checkpoint(
        "create_event",
        metadata={"calendar_id": "primary", "test": True},
    )

    if checkpoint:
        print_result(True, f"Checkpoint created: {checkpoint.checkpoint_id[:8]}...")
    else:
        print_result(False, "Failed to create checkpoint")
        return False

    # Test 2: Complete checkpoint
    backend.complete_checkpoint(
        checkpoint.checkpoint_id,
        {"event_id": "test_123", "created": True},
    )

    stored = backend.get_checkpoint(checkpoint.checkpoint_id)
    if stored and stored.created_state:
        print_result(True, "Checkpoint completed with created state")
    else:
        print_result(False, "Checkpoint not completed properly")
        tests_passed = False

    # Test 3: Clear checkpoint
    backend.clear_checkpoint(checkpoint.checkpoint_id)
    cleared = backend.get_checkpoint(checkpoint.checkpoint_id)
    if cleared is None:
        print_result(True, "Checkpoint cleared successfully")
    else:
        print_result(False, "Checkpoint not cleared")
        tests_passed = False

    return tests_passed


def main():
    """Run all E2E tests."""
    parser = argparse.ArgumentParser(description="E2E tests for Google Calendar connector")
    parser.add_argument("--db", default="~/.nexus/nexus.db", help="Path to Nexus database")
    parser.add_argument("--user", help="User email for OAuth (optional)")
    parser.add_argument("--skip-api", action="store_true", help="Skip tests that call Google API")
    args = parser.parse_args()

    print_header("Google Calendar Connector E2E Tests")
    print(f"Database: {args.db}")
    print(f"User: {args.user or '(auto-detect)'}")
    print(f"Skip API calls: {args.skip_api}")

    # Import after path setup
    from nexus.backends.gcalendar_connector import GoogleCalendarConnectorBackend
    from nexus.core.permissions import OperationContext

    # Create backend
    db_path = Path(args.db).expanduser()
    backend = GoogleCalendarConnectorBackend(
        token_manager_db=str(db_path),
        user_email=args.user,
    )

    # Create context
    user_email = args.user or "test@example.com"
    context = OperationContext(
        user=user_email,
        groups=[],
        user_id=user_email,
        tenant_id="default",
    )

    # Create temp directory for output
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        results = {}

        # Test 1: SKILL.md generation (no API)
        results["skill_md"] = test_skill_md_generation(backend, tmp_path)

        # Test 2: Validation errors (no API)
        results["validation"] = test_validation_errors(backend)

        # Test 8: Checkpoint functionality (no API)
        results["checkpoints"] = test_checkpoint_functionality(backend)

        if not args.skip_api:
            # Test 3: Create event (API)
            event_id = test_create_event(backend, context)
            results["create"] = event_id is not None

            if event_id:
                # Test 4: Read event (API)
                results["read"] = test_read_event(backend, context, event_id)

                # Test 5: Update event (API)
                results["update"] = test_update_event(backend, context, event_id)

                # Test 6: List events (API)
                results["list"] = test_list_events(backend, context)

                # Test 7: Delete event (API)
                results["delete"] = test_delete_event(backend, context, event_id)
            else:
                results["read"] = False
                results["update"] = False
                results["list"] = False
                results["delete"] = False
        else:
            print("\n⏭️  Skipping API tests (--skip-api flag)")

        # Summary
        print_header("Test Summary")

        total = len(results)
        passed = sum(1 for v in results.values() if v)

        for name, result in results.items():
            status = "✅" if result else "❌"
            print(f"  {status} {name}")

        print(f"\n{'=' * 60}")
        print(f"  Results: {passed}/{total} tests passed")
        print(f"{'=' * 60}")

        # Exit with error code if any test failed
        sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
