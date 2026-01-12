#!/usr/bin/env python3
"""E2E test script for Gmail connector mixins.

This script tests the Gmail connector's mixin functionality:
1. SKILL.md generation from static file
2. Schema validation (SendEmail, Reply, Forward, Draft)
3. Trait-based validation
4. Checkpoint functionality
5. Error formatting with SKILL.md references
6. List and read emails (with real API)

Prerequisites:
    1. Set up OAuth credentials in Google Cloud Console
    2. Run: nexus oauth login gmail
    3. Run this script: python scripts/test_gmail_e2e.py --user your@email.com

Usage:
    # Test with local database
    python scripts/test_gmail_e2e.py --user your@email.com

    # Test with Docker postgres
    python scripts/test_gmail_e2e.py --user your@email.com --docker

    # Skip API calls (test only local functionality)
    python scripts/test_gmail_e2e.py --skip-api
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
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


def get_db_url_from_docker() -> str:
    """Get postgres connection URL from docker container."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=postgres", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        container_name = result.stdout.strip().split("\n")[0]
        if not container_name:
            raise RuntimeError("No postgres container found")

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

        return f"postgresql://{postgres_user}:{postgres_password}@localhost:{port}/{postgres_db}"

    except Exception as e:
        raise RuntimeError(f"Error getting docker postgres URL: {e}") from e


def test_skill_md_generation(backend, tmp_dir: Path) -> bool:
    """Test SKILL.md loading from static file."""
    print_header("Test 1: SKILL.md Generation")

    try:
        # Generate SKILL.md content
        skill_doc = backend.generate_skill_doc("/mnt/gmail/")

        # Verify content
        checks = [
            ("Has title", "# Gmail Connector" in skill_doc),
            ("Has mount path", "`/mnt/gmail/`" in skill_doc),
            ("Has operations section", "## Operations" in skill_doc),
            ("Has Send Email", "Send Email" in skill_doc),
            ("Has Reply", "Reply" in skill_doc),
            ("Has Forward", "Forward" in skill_doc),
            ("Has Draft", "Draft" in skill_doc),
            ("Has agent_intent requirement", "agent_intent" in skill_doc),
            ("Has confirm requirement", "confirm: true" in skill_doc),
            ("Has error codes", "## Error Codes" in skill_doc),
            ("Has MISSING_AGENT_INTENT", "MISSING_AGENT_INTENT" in skill_doc),
            ("Has MISSING_CONFIRM", "MISSING_CONFIRM" in skill_doc),
            ("Has YAML examples", "```yaml" in skill_doc),
        ]

        all_passed = True
        for name, passed in checks:
            print_result(passed, name)
            if not passed:
                all_passed = False

        # Test mount path replacement
        custom_doc = backend.generate_skill_doc("/custom/path/")
        mount_replaced = "/custom/path/" in custom_doc and "/mnt/gmail/" not in custom_doc
        print_result(mount_replaced, "Mount path correctly replaced")
        if not mount_replaced:
            all_passed = False

        # Save to file for inspection
        skill_path = tmp_dir / "SKILL.md"
        skill_path.write_text(skill_doc)
        print(f"\n📄 SKILL.md saved to: {skill_path}")

        return all_passed

    except Exception as e:
        print_result(False, f"Exception: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_schema_validation() -> bool:
    """Test Pydantic schema validation."""
    print_header("Test 2: Schema Validation")

    from pydantic import ValidationError as PydanticValidationError

    from nexus.connectors.gmail.schemas import (
        DraftEmailSchema,
        ForwardEmailSchema,
        ReplyEmailSchema,
        SendEmailSchema,
    )

    tests_passed = True

    # Test 1: Valid SendEmailSchema
    try:
        email = SendEmailSchema(
            agent_intent="User requested to send project update",
            to=["alice@example.com"],
            subject="Test",
            body="Body content",
            confirm=True,
        )
        print_result(True, f"Valid SendEmailSchema: to={email.to}")
    except Exception as e:
        print_result(False, f"Valid SendEmailSchema failed: {e}")
        tests_passed = False

    # Test 2: SendEmailSchema without confirm fails
    try:
        SendEmailSchema(
            agent_intent="User requested to send email",
            to=["alice@example.com"],
            subject="Test",
            body="Body",
            # Missing confirm=True
        )
        print_result(False, "SendEmailSchema without confirm should fail")
        tests_passed = False
    except PydanticValidationError:
        print_result(True, "SendEmailSchema requires confirm=True")

    # Test 3: Email addresses normalized to lowercase
    try:
        email = SendEmailSchema(
            agent_intent="User requested to send email",
            to=["ALICE@EXAMPLE.COM"],
            subject="Test",
            body="Body",
            confirm=True,
        )
        normalized = email.to[0] == "alice@example.com"
        print_result(normalized, f"Email normalized: {email.to[0]}")
        if not normalized:
            tests_passed = False
    except Exception as e:
        print_result(False, f"Email normalization failed: {e}")
        tests_passed = False

    # Test 4: Invalid email format rejected
    try:
        SendEmailSchema(
            agent_intent="User requested to send email",
            to=["not-an-email"],
            subject="Test",
            body="Body",
            confirm=True,
        )
        print_result(False, "Invalid email should be rejected")
        tests_passed = False
    except PydanticValidationError:
        print_result(True, "Invalid email format rejected")

    # Test 5: ReplyEmailSchema requires thread_id and message_id
    try:
        ReplyEmailSchema(
            agent_intent="User wants to reply",
            thread_id="abc123",
            message_id="xyz789",
            body="Reply body",
            confirm=True,
        )
        print_result(True, "ReplyEmailSchema with thread_id and message_id")
    except Exception as e:
        print_result(False, f"ReplyEmailSchema failed: {e}")
        tests_passed = False

    # Test 6: ForwardEmailSchema
    try:
        ForwardEmailSchema(
            agent_intent="User wants to forward email",
            message_id="abc123",
            to=["partner@example.com"],
            confirm=True,
        )
        print_result(True, "ForwardEmailSchema valid")
    except Exception as e:
        print_result(False, f"ForwardEmailSchema failed: {e}")
        tests_passed = False

    # Test 7: DraftEmailSchema doesn't require confirm
    try:
        draft = DraftEmailSchema(
            agent_intent="User wants to create draft",
            body="Draft content",
        )
        print_result(True, f"DraftEmailSchema (no confirm needed): {len(draft.body)} chars")
    except Exception as e:
        print_result(False, f"DraftEmailSchema failed: {e}")
        tests_passed = False

    return tests_passed


def test_trait_validation(backend) -> bool:
    """Test trait-based validation."""
    print_header("Test 3: Trait Validation")

    from nexus.connectors.base import ValidationError

    tests_passed = True

    # Test 1: Missing agent_intent
    try:
        backend.validate_traits("send_email", {"to": ["test@example.com"]})
        print_result(False, "Missing agent_intent should raise error")
        tests_passed = False
    except ValidationError as e:
        passed = e.code == "MISSING_AGENT_INTENT"
        print_result(passed, f"Missing agent_intent raises {e.code}")
        # Verify SKILL.md reference
        has_skill_ref = "SKILL.md" in str(e)
        print_result(has_skill_ref, "Error includes SKILL.md reference")
        if not passed or not has_skill_ref:
            tests_passed = False

    # Test 2: Short agent_intent
    try:
        backend.validate_traits("send_email", {"agent_intent": "short"})
        print_result(False, "Short agent_intent should raise error")
        tests_passed = False
    except ValidationError as e:
        passed = e.code == "AGENT_INTENT_TOO_SHORT"
        print_result(passed, f"Short agent_intent raises {e.code}")
        if not passed:
            tests_passed = False

    # Test 3: Send without confirm
    try:
        backend.validate_traits(
            "send_email",
            {"agent_intent": "Sending email as requested by user"},
        )
        print_result(False, "Send without confirm should raise error")
        tests_passed = False
    except ValidationError as e:
        passed = e.code == "MISSING_CONFIRM"
        print_result(passed, f"Send without confirm raises {e.code}")
        if not passed:
            tests_passed = False

    # Test 4: Valid send passes
    try:
        warnings = backend.validate_traits(
            "send_email",
            {"agent_intent": "Sending email as requested by user", "confirm": True},
        )
        print_result(True, f"Valid send passes (warnings: {len(warnings)})")
    except ValidationError as e:
        print_result(False, f"Valid send should pass but got: {e.code}")
        tests_passed = False

    # Test 5: Draft only needs intent (no confirm)
    try:
        warnings = backend.validate_traits(
            "create_draft",
            {"agent_intent": "Creating draft for user to review later"},
        )
        print_result(True, f"Draft only needs intent (warnings: {len(warnings)})")
    except ValidationError as e:
        print_result(False, f"Draft should pass with just intent but got: {e.code}")
        tests_passed = False

    return tests_passed


def test_operation_traits(backend) -> bool:
    """Test operation trait configuration."""
    print_header("Test 4: Operation Traits")

    from nexus.connectors.base import ConfirmLevel, Reversibility

    tests_passed = True

    # Test send_email traits
    traits = backend.get_operation_traits("send_email")
    if traits:
        checks = [
            ("send_email: NONE reversibility", traits.reversibility == Reversibility.NONE),
            ("send_email: EXPLICIT confirm", traits.confirm == ConfirmLevel.EXPLICIT),
            ("send_email: checkpoint enabled", traits.checkpoint is True),
        ]
        for name, passed in checks:
            print_result(passed, name)
            if not passed:
                tests_passed = False
    else:
        print_result(False, "send_email traits not found")
        tests_passed = False

    # Test reply_email traits
    traits = backend.get_operation_traits("reply_email")
    if traits:
        print_result(traits.reversibility == Reversibility.NONE, "reply_email: NONE reversibility")
        print_result(traits.confirm == ConfirmLevel.EXPLICIT, "reply_email: EXPLICIT confirm")
    else:
        print_result(False, "reply_email traits not found")
        tests_passed = False

    # Test forward_email traits
    traits = backend.get_operation_traits("forward_email")
    if traits:
        print_result(
            traits.reversibility == Reversibility.NONE, "forward_email: NONE reversibility"
        )
    else:
        print_result(False, "forward_email traits not found")
        tests_passed = False

    # Test create_draft traits (different from send)
    traits = backend.get_operation_traits("create_draft")
    if traits:
        checks = [
            ("create_draft: FULL reversibility", traits.reversibility == Reversibility.FULL),
            ("create_draft: INTENT confirm", traits.confirm == ConfirmLevel.INTENT),
        ]
        for name, passed in checks:
            print_result(passed, name)
            if not passed:
                tests_passed = False
    else:
        print_result(False, "create_draft traits not found")
        tests_passed = False

    return tests_passed


def test_checkpoint_functionality(backend) -> bool:
    """Test checkpoint creation and management."""
    print_header("Test 5: Checkpoint Functionality")

    tests_passed = True

    # Test 1: Create checkpoint for send_email
    checkpoint = backend.create_checkpoint(
        "send_email",
        metadata={"to": ["test@example.com"], "subject": "Test"},
    )

    if checkpoint:
        print_result(True, f"Checkpoint created: {checkpoint.checkpoint_id[:8]}...")
    else:
        print_result(False, "Failed to create checkpoint")
        return False

    # Test 2: Complete checkpoint
    backend.complete_checkpoint(
        checkpoint.checkpoint_id,
        {"message_id": "sent_123", "thread_id": "thread_abc"},
    )

    stored = backend.get_checkpoint(checkpoint.checkpoint_id)
    if stored and stored.created_state:
        print_result(
            True, f"Checkpoint completed: message_id={stored.created_state.get('message_id')}"
        )
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

    # Test 4: Draft also supports checkpoints
    draft_checkpoint = backend.create_checkpoint("create_draft")
    if draft_checkpoint:
        print_result(True, "Draft checkpoint created (can be deleted)")
        backend.clear_checkpoint(draft_checkpoint.checkpoint_id)
    else:
        print_result(False, "Draft checkpoint creation failed")
        tests_passed = False

    return tests_passed


def test_error_formatting(backend) -> bool:
    """Test error message formatting."""
    print_header("Test 6: Error Formatting")

    tests_passed = True

    # Set mount path
    backend.set_mount_path("/mnt/gmail")

    # Test error with skill reference
    error = backend.format_error_with_skill_ref(
        code="MISSING_AGENT_INTENT",
        message="Missing required field",
    )

    checks = [
        ("Error has code", error.code == "MISSING_AGENT_INTENT"),
        ("Error has SKILL.md path", "/mnt/gmail/.skill/SKILL.md" in str(error)),
        ("Error has section anchor", "#" in str(error)),
    ]

    for name, passed in checks:
        print_result(passed, name)
        if not passed:
            tests_passed = False

    # Test error from registry
    error = backend.format_error_with_skill_ref(
        code="MISSING_CONFIRM",
        message="",
    )

    has_fix = "confirm" in str(error).lower()
    print_result(has_fix, "Error includes fix example from registry")
    if not has_fix:
        tests_passed = False

    return tests_passed


def test_list_emails(backend, context) -> bool:
    """Test listing emails from Gmail."""
    print_header("Test 7: List Emails (API)")

    try:
        # List INBOX
        emails = backend.list_dir("INBOX", context)
        print_result(True, f"Listed {len(emails)} emails in INBOX")

        if emails:
            print("\n📧 Sample emails (first 5):")
            for email in emails[:5]:
                print(f"  - {email}")

        # List other folders
        for folder in ["SENT", "STARRED", "IMPORTANT"]:
            try:
                folder_emails = backend.list_dir(folder, context)
                print_result(True, f"{folder}: {len(folder_emails)} emails")
            except Exception as e:
                print_result(False, f"{folder}: {e}")

        return True

    except Exception as e:
        print_result(False, f"Failed to list emails: {e}")
        import traceback

        traceback.print_exc()
        return False


def test_read_email(backend, context) -> bool:
    """Test reading an email."""
    print_header("Test 8: Read Email (API)")

    try:
        # First list INBOX to get an email
        emails = backend.list_dir("INBOX", context)
        if not emails:
            print_result(False, "No emails in INBOX to read")
            return False

        # Read the first email
        email_file = emails[0]
        context.backend_path = f"INBOX/{email_file}"

        response = backend.read_content("", context)
        content = response.unwrap()

        # Verify content
        content_str = content.decode("utf-8")

        checks = [
            ("Has subject", "subject:" in content_str),
            ("Has from", "from:" in content_str),
            ("Has date", "date:" in content_str),
            ("Has body_text", "body_text:" in content_str),
            ("Is valid YAML", "id:" in content_str),
        ]

        all_passed = True
        for name, passed in checks:
            print_result(passed, name)
            if not passed:
                all_passed = False

        print(f"\n📄 Email content (first 500 chars):\n{content_str[:500]}...")

        return all_passed

    except Exception as e:
        print_result(False, f"Failed to read email: {e}")
        import traceback

        traceback.print_exc()
        return False


def main():
    """Run all E2E tests."""
    parser = argparse.ArgumentParser(description="E2E tests for Gmail connector mixins")
    parser.add_argument("--db", default="~/.nexus/nexus.db", help="Path to Nexus database")
    parser.add_argument("--user", help="User email for OAuth")
    parser.add_argument("--docker", action="store_true", help="Use Docker postgres database")
    parser.add_argument("--skip-api", action="store_true", help="Skip tests that call Gmail API")
    args = parser.parse_args()

    print_header("Gmail Connector E2E Tests")

    # Get database URL
    if args.docker:
        try:
            db_url = get_db_url_from_docker()
            print("Database: Docker postgres")
        except Exception as e:
            print(f"❌ Failed to get Docker database: {e}")
            sys.exit(1)
    else:
        db_url = str(Path(args.db).expanduser())
        print(f"Database: {db_url}")

    print(f"User: {args.user or '(required for API tests)'}")
    print(f"Skip API calls: {args.skip_api}")

    # Import after path setup
    from nexus.backends.gmail_connector import GmailConnectorBackend
    from nexus.core.permissions import OperationContext

    # Create backend
    backend = GmailConnectorBackend(
        token_manager_db=db_url,
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

        # Test 2: Schema validation (no API)
        results["schema"] = test_schema_validation()

        # Test 3: Trait validation (no API)
        results["traits"] = test_trait_validation(backend)

        # Test 4: Operation traits (no API)
        results["op_traits"] = test_operation_traits(backend)

        # Test 5: Checkpoint functionality (no API)
        results["checkpoints"] = test_checkpoint_functionality(backend)

        # Test 6: Error formatting (no API)
        results["errors"] = test_error_formatting(backend)

        if not args.skip_api:
            if not args.user:
                print("\n⚠️  Skipping API tests (--user not provided)")
            else:
                # Test 7: List emails (API)
                results["list"] = test_list_emails(backend, context)

                # Test 8: Read email (API)
                results["read"] = test_read_email(backend, context)
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
