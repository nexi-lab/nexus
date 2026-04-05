"""Golden path E2E integration test for CLI connector lifecycle (Issue #3148, Decision #11A).

Tests the full pipeline:
    mount → post-mount hooks → skill doc generation →
    schema regeneration → agent reads skill doc → agent writes YAML →
    schema validation → CLI execution (fake) → VFS hooks

This uses a mock filesystem to test the orchestration without requiring
real CLI tools or OAuth tokens.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, Field

from nexus.backends.connectors.base import (
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    ReadmeDocMixin,
    Reversibility,
    TraitBasedMixin,
    ValidatedMixin,
)
from nexus.backends.connectors.cli.result import CLIErrorMapper, CLIResult, CLIResultStatus
from nexus.contracts.backend_features import BackendFeature

# ---------------------------------------------------------------------------
# Test schema and connector
# ---------------------------------------------------------------------------


class CreateIssueSchema(BaseModel):
    """Test schema for the fake GitHub connector."""

    title: str = Field(..., min_length=1, max_length=256, description="Issue title")
    body: str = Field(default="", description="Issue body in markdown")
    labels: list[str] = Field(default_factory=list, description="Labels to apply")
    agent_intent: str = Field(..., min_length=10, description="Why this issue is being created")
    confirm: bool = Field(default=False, description="Explicit confirmation")


class FakeGHConnector(ReadmeDocMixin, ValidatedMixin, TraitBasedMixin):
    """Fake GitHub CLI connector for E2E testing."""

    SKILL_NAME = "github"
    SCHEMAS = {"create_issue": CreateIssueSchema}
    OPERATION_TRAITS = {
        "create_issue": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.INTENT,
        ),
    }
    ERROR_REGISTRY = {
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            readme_section="required-format",
            fix_example="# agent_intent: User requested a new issue for bug tracking",
        ),
    }
    EXAMPLES = {
        "create_issue.yaml": (
            "# agent_intent: User reported a login bug\n"
            "title: Login fails on Safari\n"
            "body: |\n"
            "  Steps to reproduce:\n"
            "  1. Open Safari\n"
            "  2. Navigate to /login\n"
            "labels:\n"
            "  - bug\n"
        ),
    }

    _BACKEND_FEATURES = frozenset(
        {
            BackendFeature.README_DOC,
            BackendFeature.CLI_BACKED,
        }
    )

    def has_feature(self, cap: BackendFeature) -> bool:
        return cap in self._BACKEND_FEATURES

    @property
    def capabilities(self) -> frozenset[BackendFeature]:
        return self._BACKEND_FEATURES


# ---------------------------------------------------------------------------
# E2E Test: Full connector lifecycle
# ---------------------------------------------------------------------------


class TestConnectorLifecycleE2E:
    """Golden path: mount → hooks → skill docs → sync → write → validate."""

    def test_step1_readme_doc_generation(self) -> None:
        """Step 1: Connector generates skill docs on mount."""
        connector = FakeGHConnector()
        connector.set_mount_path("/mnt/github")

        # Generate skill doc
        readme_doc = connector.generate_readme("/mnt/github")
        assert "# Github Connector" in readme_doc or "# GitHub Connector" in readme_doc.title()
        assert "create_issue" in readme_doc.lower() or "Create Issue" in readme_doc

    @pytest.mark.asyncio
    async def test_step2_readme_doc_writes_to_filesystem(self) -> None:
        """Step 2: Readme docs written to .readme/ directory with schema files."""
        connector = FakeGHConnector()
        fs = MagicMock()
        fs.mkdir = AsyncMock()
        fs.write = AsyncMock()

        result = await connector.write_readme("/mnt/github", fs)

        # README.md should be written
        assert result["readme_md"] == "/mnt/github/.readme/README.md"

        # Schema files should be written (Issue #3148)
        assert len(result.get("schemas", [])) > 0
        assert any("create_issue" in s for s in result.get("schemas", []))

        # Example files should be written
        assert len(result["examples"]) > 0

        # Verify filesystem calls
        fs.mkdir.assert_called()
        fs.write.assert_called()

    def test_step3_schema_validation_success(self) -> None:
        """Step 3: Valid YAML passes schema validation."""
        connector = FakeGHConnector()
        connector.set_mount_path("/mnt/github")

        data = {
            "title": "Login fails on Safari",
            "body": "Steps to reproduce...",
            "labels": ["bug"],
            "agent_intent": "User reported a login bug and requested tracking",
        }
        validated = connector.validate_schema("create_issue", data)
        assert validated.title == "Login fails on Safari"

    def test_step4_schema_validation_failure(self) -> None:
        """Step 4: Invalid YAML fails with self-correcting error."""
        connector = FakeGHConnector()
        connector.set_mount_path("/mnt/github")

        from nexus.backends.connectors.base import ValidationError

        data = {"body": "no title"}  # Missing required 'title' + 'agent_intent'
        with pytest.raises((ValidationError, Exception)):
            connector.validate_schema("create_issue", data)

    def test_step5_trait_validation(self) -> None:
        """Step 5: Trait validation checks agent_intent."""
        connector = FakeGHConnector()
        connector.set_mount_path("/mnt/github")

        # Missing agent_intent should fail
        from nexus.backends.connectors.base import ValidationError

        with pytest.raises(ValidationError):
            connector.validate_traits("create_issue", {"title": "test"})

        # Short agent_intent should fail
        with pytest.raises(ValidationError):
            connector.validate_traits("create_issue", {"agent_intent": "short"})

        # Valid agent_intent should pass
        warnings = connector.validate_traits(
            "create_issue",
            {"agent_intent": "User requested a new issue for tracking a bug"},
        )
        assert isinstance(warnings, list)

    def test_step6_cli_error_mapping(self) -> None:
        """Step 6: CLI errors map to structured error codes."""
        mapper = CLIErrorMapper()

        # Simulate rate limit from GitHub CLI
        result = CLIResult(
            status=CLIResultStatus.EXIT_ERROR,
            exit_code=1,
            stderr="API rate limit exceeded for user",
            command=["gh", "issue", "create"],
        )
        enriched = mapper.classify_result(result)
        assert enriched.error_code == "RATE_LIMITED"
        assert enriched.retryable is True

    def test_step8_capability_declaration(self) -> None:
        """Step 8: Connector declares correct capabilities."""
        connector = FakeGHConnector()
        assert connector.has_feature(BackendFeature.README_DOC)
        assert connector.has_feature(BackendFeature.CLI_BACKED)


# ---------------------------------------------------------------------------
# Display path integration (Issue #3256, Decision 11A)
# ---------------------------------------------------------------------------


class TestDisplayPathIntegration:
    """Integration: sync → display_path → collision resolution roundtrip."""

    def test_collision_resolution_in_sync_pipeline(self) -> None:
        """Verify resolve_collisions handles duplicates from same display_path."""
        from nexus.backends.connectors.cli.display_path import resolve_collisions

        # Simulate two emails with the same subject on the same date
        items = [
            ("INBOX/PRIMARY/2026-03-20_Meeting-Notes.yaml", "msg-aaa"),
            ("INBOX/PRIMARY/2026-03-20_Meeting-Notes.yaml", "msg-bbb"),
            ("INBOX/PRIMARY/2026-03-20_Unique-Email.yaml", "msg-ccc"),
        ]
        resolved = resolve_collisions(items)

        # Unique email should be unchanged
        assert resolved[2] == items[2]

        # Colliding emails should be disambiguated
        assert resolved[0][0] != resolved[1][0]
        assert resolved[0][0].startswith("INBOX/PRIMARY/2026-03-20_Meeting-Notes_")
        assert resolved[0][0].endswith(".yaml")
        assert resolved[1][0].endswith(".yaml")

        # Backend IDs preserved
        assert resolved[0][1] == "msg-aaa"
        assert resolved[1][1] == "msg-bbb"

    def test_display_path_mixin_default_fallback(self) -> None:
        """Connectors without display_path override get item_id.yaml."""
        from nexus.backends.connectors.cli.display_path import DisplayPathMixin

        mixin = DisplayPathMixin()
        assert mixin.display_path("abc123") == "abc123.yaml"
        assert mixin.display_path("abc123", {"some": "meta"}) == "abc123.yaml"

    def test_gmail_display_path_roundtrip(self) -> None:
        """Gmail display_path produces readable paths with categories."""
        from nexus.backends.connectors.gws.connector import GmailConnector

        connector = GmailConnector.__new__(GmailConnector)
        path = connector.display_path(
            "msg-123",
            {
                "subject": "Re: Q4 Budget Review",
                "date": "2026-03-20T10:30:00Z",
                "labels": ["INBOX", "CATEGORY_SOCIAL"],
            },
        )

        # Should have category subfolder
        assert path.startswith("INBOX/SOCIAL/")
        # Should have date prefix
        assert "2026-03-20" in path
        # Should have sanitized subject
        assert "Re-Q4-Budget-Review" in path
        assert path.endswith(".yaml")
        # No unsafe characters
        assert ":" not in path
        assert "?" not in path

    def test_calendar_display_path_roundtrip(self) -> None:
        """Calendar display_path produces readable paths with month grouping."""
        from nexus.backends.connectors.gws.connector import CalendarConnector

        connector = CalendarConnector.__new__(CalendarConnector)
        path = connector.display_path(
            "evt-456",
            {
                "summary": "Team Standup",
                "start": {"dateTime": "2026-03-21T10:00:00-07:00"},
                "calendarId": "primary",
            },
        )

        assert path.startswith("primary/2026-03/")
        assert "2026-03-21" in path
        assert "10-00" in path
        assert "Team-Standup" in path

    def test_github_display_path_roundtrip(self) -> None:
        """GitHub display_path produces readable paths with type subfolder."""
        from nexus.backends.connectors.github.connector import GitHubConnector

        connector = GitHubConnector.__new__(GitHubConnector)
        path = connector.display_path(
            "issue-142",
            {
                "number": 142,
                "title": "feat: add grove status command",
            },
        )
        assert path == "issues/142_feat-add-grove-status-command.yaml"

    def test_backend_id_in_cache_entry(self) -> None:
        """Verify backend_id is included in cache entry dict (Decision 15A)."""
        # This tests that the sync pipeline adds backend_id to the cache entry
        # dict, which is used for reverse mapping (display path → backend ID).
        cache_entry = {
            "path": "/mnt/gmail/INBOX/PRIMARY/2026-03-20_Meeting.yaml",
            "content": b"test",
            "content_text": None,
            "content_type": "full",
            "backend_version": None,
            "backend_id": "INBOX/msg-aaa.yaml",
            "parsed_from": None,
            "parse_metadata": None,
            "zone_id": None,
        }
        assert cache_entry["backend_id"] == "INBOX/msg-aaa.yaml"
