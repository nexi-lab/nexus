"""Golden path E2E integration test for CLI connector lifecycle (Issue #3148, Decision #11A).

Tests the full pipeline:
    mount → post-mount hooks → skill doc generation → sync →
    schema regeneration → agent reads skill doc → agent writes YAML →
    schema validation → CLI execution (fake) → VFS hooks

This uses the FakeConnectorSyncProvider and mock filesystem to test
the orchestration without requiring real CLI tools or OAuth tokens.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, Field

from nexus.backends.connectors.base import (
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    Reversibility,
    SkillDocMixin,
    TraitBasedMixin,
    ValidatedMixin,
)
from nexus.backends.connectors.cli.protocol import (
    ConnectorSyncProvider,
    FetchResult,
    RemoteItem,
    SyncPage,
)
from nexus.backends.connectors.cli.result import CLIErrorMapper, CLIResult, CLIResultStatus
from nexus.contracts.capabilities import ConnectorCapability

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


class FakeGHConnector(SkillDocMixin, ValidatedMixin, TraitBasedMixin):
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
            skill_section="required-format",
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

    _CAPABILITIES = frozenset(
        {
            ConnectorCapability.SKILL_DOC,
            ConnectorCapability.WRITE_BACK,
            ConnectorCapability.CLI_BACKED,
        }
    )

    def has_capability(self, cap: ConnectorCapability) -> bool:
        return cap in self._CAPABILITIES

    @property
    def capabilities(self) -> frozenset[ConnectorCapability]:
        return self._CAPABILITIES


class FakeGHSyncProvider:
    """Fake sync provider for GitHub issues."""

    async def list_remote_items(
        self,
        path: str,
        *,
        since: str | None = None,
        page_token: str | None = None,
        page_size: int = 100,
    ) -> SyncPage:
        return SyncPage(
            items=[
                RemoteItem(
                    item_id="issue-1",
                    relative_path="issues/1.yaml",
                    size=256,
                    metadata={"title": "Test issue"},
                ),
            ],
            state_token="sync-tok-1",
        )

    async def fetch_item(self, item_id: str) -> FetchResult:
        return FetchResult(
            relative_path="issues/1.yaml",
            content=b"title: Test issue\nbody: This is a test\n",
        )


# ---------------------------------------------------------------------------
# E2E Test: Full connector lifecycle
# ---------------------------------------------------------------------------


class TestConnectorLifecycleE2E:
    """Golden path: mount → hooks → skill docs → sync → write → validate."""

    def test_step1_skill_doc_generation(self) -> None:
        """Step 1: Connector generates skill docs on mount."""
        connector = FakeGHConnector()
        connector.set_mount_path("/mnt/github")

        # Generate skill doc
        skill_doc = connector.generate_skill_doc("/mnt/github")
        assert "# Github Connector" in skill_doc or "# GitHub Connector" in skill_doc.title()
        assert "create_issue" in skill_doc.lower() or "Create Issue" in skill_doc

    @pytest.mark.asyncio
    async def test_step2_skill_doc_writes_to_filesystem(self) -> None:
        """Step 2: Skill docs written to .skill/ directory with schema files."""
        connector = FakeGHConnector()
        fs = MagicMock()
        fs.mkdir = AsyncMock()
        fs.write = AsyncMock()

        result = await connector.write_skill_docs("/mnt/github", fs)

        # SKILL.md should be written
        assert result["skill_md"] == "/mnt/github/.skill/SKILL.md"

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

    @pytest.mark.asyncio
    async def test_step7_sync_with_fake_provider(self) -> None:
        """Step 7: Sync provider returns items for indexing."""
        provider = FakeGHSyncProvider()

        # List remote items
        page = await provider.list_remote_items("/")
        assert len(page.items) == 1
        assert page.state_token == "sync-tok-1"

        # Fetch item content
        fetched = await provider.fetch_item("issue-1")
        assert fetched.content is not None
        assert b"title: Test issue" in fetched.content

    def test_step8_capability_declaration(self) -> None:
        """Step 8: Connector declares correct capabilities."""
        connector = FakeGHConnector()
        assert connector.has_capability(ConnectorCapability.SKILL_DOC)
        assert connector.has_capability(ConnectorCapability.WRITE_BACK)
        assert connector.has_capability(ConnectorCapability.CLI_BACKED)

    @pytest.mark.asyncio
    async def test_step9_sync_provider_satisfies_protocol(self) -> None:
        """Step 9: Fake sync provider satisfies ConnectorSyncProvider protocol."""
        provider = FakeGHSyncProvider()
        assert isinstance(provider, ConnectorSyncProvider)
