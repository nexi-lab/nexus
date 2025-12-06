"""Unit tests for NexusFSSkillsMixin.

Tests cover skills management operations:
- skills_create: Create a new skill from template
- skills_create_from_content: Create skill from custom content
- skills_create_from_file: Create skill from file or URL
- skills_list: List all skills
- skills_info: Get detailed skill information
- skills_fork: Fork an existing skill
- skills_publish: Publish skill to another tier
- skills_search: Search skills by description
- skills_submit_approval: Submit skill for approval
- skills_approve: Approve a skill
- skills_reject: Reject a skill
- skills_list_approvals: List approval requests
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from datetime import UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus import LocalBackend, NexusFS


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance for testing."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=False,
    )
    yield nx
    nx.close()


class TestRunAsyncSkillOperation:
    """Tests for _run_async_skill_operation helper."""

    def test_run_async_skill_operation_basic(self, nx: NexusFS) -> None:
        """Test running a basic async operation."""

        async def simple_coro() -> dict[str, str]:
            return {"result": "success"}

        result = nx._run_async_skill_operation(simple_coro())
        assert result == {"result": "success"}

    def test_run_async_skill_operation_with_exception(self, nx: NexusFS) -> None:
        """Test that exceptions are propagated."""

        async def failing_coro() -> dict[str, str]:
            raise ValueError("Test error")

        with pytest.raises(ValueError, match="Test error"):
            nx._run_async_skill_operation(failing_coro())


class TestGetSkillRegistry:
    """Tests for _get_skill_registry helper."""

    def test_get_skill_registry_returns_registry(self, nx: NexusFS) -> None:
        """Test that _get_skill_registry returns a SkillRegistry."""
        registry = nx._get_skill_registry()
        assert registry is not None
        # Verify it's the right type
        from nexus.skills import SkillRegistry

        assert isinstance(registry, SkillRegistry)


class TestGetSkillManager:
    """Tests for _get_skill_manager helper."""

    def test_get_skill_manager_returns_manager(self, nx: NexusFS) -> None:
        """Test that _get_skill_manager returns a SkillManager."""
        manager = nx._get_skill_manager()
        assert manager is not None
        from nexus.skills import SkillManager

        assert isinstance(manager, SkillManager)


class TestGetSkillGovernance:
    """Tests for _get_skill_governance helper."""

    def test_get_skill_governance_returns_governance(self, nx: NexusFS) -> None:
        """Test that _get_skill_governance returns a SkillGovernance."""
        governance = nx._get_skill_governance()
        assert governance is not None
        from nexus.skills import SkillGovernance

        assert isinstance(governance, SkillGovernance)


class TestSkillsCreate:
    """Tests for skills_create method."""

    def test_skills_create_basic(self, nx: NexusFS) -> None:
        """Test creating a basic skill."""
        with patch.object(nx, "_get_skill_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.create_skill = AsyncMock(return_value="/skills/agent/test-skill.md")
            mock_get_manager.return_value = mock_manager

            result = nx.skills_create(
                name="test-skill",
                description="A test skill",
                template="basic",
                tier="agent",
            )

            assert result["skill_path"] == "/skills/agent/test-skill.md"
            assert result["name"] == "test-skill"
            assert result["tier"] == "agent"
            assert result["template"] == "basic"

    def test_skills_create_with_author(self, nx: NexusFS) -> None:
        """Test creating a skill with author."""
        with patch.object(nx, "_get_skill_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.create_skill = AsyncMock(return_value="/skills/agent/my-skill.md")
            mock_get_manager.return_value = mock_manager

            result = nx.skills_create(
                name="my-skill",
                description="My skill",
                author="alice",
            )

            assert result["name"] == "my-skill"
            mock_manager.create_skill.assert_called_once()
            call_kwargs = mock_manager.create_skill.call_args[1]
            assert call_kwargs["author"] == "alice"

    def test_skills_create_different_tiers(self, nx: NexusFS) -> None:
        """Test creating skills in different tiers."""
        for tier in ["agent", "tenant", "system"]:
            with patch.object(nx, "_get_skill_manager") as mock_get_manager:
                mock_manager = MagicMock()
                mock_manager.create_skill = AsyncMock(return_value=f"/skills/{tier}/test.md")
                mock_get_manager.return_value = mock_manager

                result = nx.skills_create(
                    name="test",
                    description="Test",
                    tier=tier,
                )

                assert result["tier"] == tier


class TestSkillsCreateFromContent:
    """Tests for skills_create_from_content method."""

    def test_skills_create_from_content_basic(self, nx: NexusFS) -> None:
        """Test creating a skill from custom content."""
        with patch.object(nx, "_get_skill_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.create_skill_from_content = AsyncMock(
                return_value="/skills/agent/custom-skill.md"
            )
            mock_get_manager.return_value = mock_manager

            result = nx.skills_create_from_content(
                name="custom-skill",
                description="A custom skill",
                content="# Custom Skill\n\nThis is custom content.",
            )

            assert result["skill_path"] == "/skills/agent/custom-skill.md"
            assert result["name"] == "custom-skill"

    def test_skills_create_from_content_with_source_url(self, nx: NexusFS) -> None:
        """Test creating a skill with source URL."""
        with patch.object(nx, "_get_skill_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.create_skill_from_content = AsyncMock(
                return_value="/skills/agent/web-skill.md"
            )
            mock_get_manager.return_value = mock_manager

            result = nx.skills_create_from_content(
                name="web-skill",
                description="Skill from web",
                content="# Web Skill",
                source_url="https://example.com/docs",
            )

            assert result["source_url"] == "https://example.com/docs"


class TestSkillsCreateFromFile:
    """Tests for skills_create_from_file method."""

    def test_skills_create_from_file_no_plugin_raises_error(self, nx: NexusFS) -> None:
        """Test that missing plugin raises RuntimeError."""
        with (
            patch.dict("sys.modules", {"nexus_skill_seekers": None}),
            pytest.raises(RuntimeError, match="skill-seekers plugin not installed"),
        ):
            nx.skills_create_from_file(
                source="test.pdf",
            )

    def test_skills_create_from_file_url(self, nx: NexusFS) -> None:
        """Test creating skill from URL when plugin is available."""
        # Skip if nexus_skill_seekers is not installed
        pytest.importorskip("nexus_skill_seekers")

        with patch("nexus_skill_seekers.plugin.SkillSeekersPlugin") as MockPlugin:
            mock_plugin = MagicMock()
            mock_plugin.generate_skill = AsyncMock(return_value="/skills/agent/url-skill.md")
            MockPlugin.return_value = mock_plugin

            result = nx.skills_create_from_file(
                source="https://example.com/docs",
                name="url-skill",
            )

            assert result["skill_path"] == "/skills/agent/url-skill.md"
            assert result["source"] == "https://example.com/docs"

    def test_skills_create_from_file_auto_name_generation(self, nx: NexusFS) -> None:
        """Test auto-generation of skill name from source."""
        # Skip if nexus_skill_seekers is not installed
        pytest.importorskip("nexus_skill_seekers")

        with patch("nexus_skill_seekers.plugin.SkillSeekersPlugin") as MockPlugin:
            mock_plugin = MagicMock()
            mock_plugin.generate_skill = AsyncMock(return_value="/skills/agent/example-com.md")
            MockPlugin.return_value = mock_plugin

            result = nx.skills_create_from_file(
                source="https://example.com/docs",
                # name not provided - should be auto-generated
            )

            # Name should be derived from URL
            assert "name" in result


class TestSkillsList:
    """Tests for skills_list method."""

    def test_skills_list_all(self, nx: NexusFS) -> None:
        """Test listing all skills."""
        with patch.object(nx, "_get_skill_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.discover = AsyncMock()
            mock_registry.list_skills.return_value = [
                MagicMock(
                    name="skill1",
                    description="Skill 1",
                    version="1.0.0",
                    author="alice",
                    tier="agent",
                    file_path="/skills/agent/skill1.md",
                    requires=[],
                    created_at=None,
                    modified_at=None,
                ),
                MagicMock(
                    name="skill2",
                    description="Skill 2",
                    version="1.0.0",
                    author="bob",
                    tier="tenant",
                    file_path="/skills/tenant/skill2.md",
                    requires=[],
                    created_at=None,
                    modified_at=None,
                ),
            ]
            mock_get_registry.return_value = mock_registry

            result = nx.skills_list()

            assert "skills" in result
            assert "count" in result
            assert result["count"] == 2
            assert len(result["skills"]) == 2

    def test_skills_list_by_tier(self, nx: NexusFS) -> None:
        """Test listing skills filtered by tier."""
        with patch.object(nx, "_get_skill_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.discover = AsyncMock()
            mock_registry.list_skills.return_value = [
                MagicMock(
                    name="agent-skill",
                    description="Agent skill",
                    version="1.0.0",
                    author="alice",
                    tier="agent",
                    file_path="/skills/agent/agent-skill.md",
                    requires=[],
                    created_at=None,
                    modified_at=None,
                ),
            ]
            mock_get_registry.return_value = mock_registry

            nx.skills_list(tier="agent")

            mock_registry.list_skills.assert_called_once_with(tier="agent", include_metadata=True)


class TestSkillsInfo:
    """Tests for skills_info method."""

    def test_skills_info_basic(self, nx: NexusFS) -> None:
        """Test getting skill information."""
        with patch.object(nx, "_get_skill_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.discover = AsyncMock()

            # Create a proper mock metadata object
            mock_metadata = MagicMock()
            mock_metadata.name = "test-skill"
            mock_metadata.description = "A test skill"
            mock_metadata.version = "1.0.0"
            mock_metadata.author = "alice"
            mock_metadata.tier = "agent"
            mock_metadata.file_path = "/skills/agent/test-skill.md"
            mock_metadata.requires = ["dep1"]
            mock_metadata.created_at = None
            mock_metadata.modified_at = None

            mock_registry.get_metadata.return_value = mock_metadata
            mock_registry.resolve_dependencies = AsyncMock(return_value=["dep1"])
            mock_get_registry.return_value = mock_registry

            result = nx.skills_info("test-skill")

            assert result["name"] == "test-skill"
            assert result["description"] == "A test skill"
            assert result["version"] == "1.0.0"
            assert result["author"] == "alice"
            assert result["tier"] == "agent"
            assert "resolved_dependencies" in result


class TestSkillsFork:
    """Tests for skills_fork method."""

    def test_skills_fork_basic(self, nx: NexusFS) -> None:
        """Test forking a skill."""
        with (
            patch.object(nx, "_get_skill_manager") as mock_get_manager,
            patch.object(nx, "_get_skill_registry") as mock_get_registry,
        ):
            mock_manager = MagicMock()
            mock_manager.fork_skill = AsyncMock(return_value="/skills/agent/forked-skill.md")
            mock_get_manager.return_value = mock_manager

            mock_registry = MagicMock()
            mock_registry.discover = AsyncMock()
            mock_get_registry.return_value = mock_registry

            result = nx.skills_fork(
                source_name="original-skill",
                target_name="forked-skill",
            )

            assert result["forked_path"] == "/skills/agent/forked-skill.md"
            assert result["source_name"] == "original-skill"
            assert result["target_name"] == "forked-skill"


class TestSkillsPublish:
    """Tests for skills_publish method."""

    def test_skills_publish_basic(self, nx: NexusFS) -> None:
        """Test publishing a skill."""
        with patch.object(nx, "_get_skill_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.publish_skill = AsyncMock(return_value="/skills/tenant/published-skill.md")
            mock_get_manager.return_value = mock_manager

            result = nx.skills_publish(
                skill_name="my-skill",
                source_tier="agent",
                target_tier="tenant",
            )

            assert result["published_path"] == "/skills/tenant/published-skill.md"
            assert result["skill_name"] == "my-skill"
            assert result["source_tier"] == "agent"
            assert result["target_tier"] == "tenant"


class TestSkillsSearch:
    """Tests for skills_search method."""

    def test_skills_search_basic(self, nx: NexusFS) -> None:
        """Test searching skills."""
        with patch.object(nx, "_get_skill_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.search_skills = AsyncMock(
                return_value=[("skill1", 0.95), ("skill2", 0.85)]
            )
            mock_get_manager.return_value = mock_manager

            result = nx.skills_search(query="data processing")

            assert result["query"] == "data processing"
            assert result["count"] == 2
            assert len(result["results"]) == 2
            assert result["results"][0]["skill_name"] == "skill1"
            assert result["results"][0]["score"] == 0.95

    def test_skills_search_with_filters(self, nx: NexusFS) -> None:
        """Test searching skills with filters."""
        with patch.object(nx, "_get_skill_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.search_skills = AsyncMock(return_value=[])
            mock_get_manager.return_value = mock_manager

            nx.skills_search(
                query="test",
                tier="agent",
                limit=5,
            )

            mock_manager.search_skills.assert_called_once_with(query="test", tier="agent", limit=5)


class TestSkillsApproval:
    """Tests for skill approval workflow."""

    def test_skills_submit_approval(self, nx: NexusFS) -> None:
        """Test submitting a skill for approval."""
        with patch.object(nx, "_get_skill_governance") as mock_get_gov:
            mock_gov = MagicMock()
            mock_gov.submit_for_approval = AsyncMock(return_value="approval-123")
            mock_get_gov.return_value = mock_gov

            result = nx.skills_submit_approval(
                skill_name="my-skill",
                submitted_by="alice",
                reviewers=["bob", "charlie"],
                comments="Please review",
            )

            assert result["approval_id"] == "approval-123"
            assert result["skill_name"] == "my-skill"
            assert result["submitted_by"] == "alice"
            assert result["reviewers"] == ["bob", "charlie"]

    def test_skills_approve(self, nx: NexusFS) -> None:
        """Test approving a skill."""
        with patch.object(nx, "_get_skill_governance") as mock_get_gov:
            mock_gov = MagicMock()
            mock_gov.approve_skill = AsyncMock()
            mock_get_gov.return_value = mock_gov

            result = nx.skills_approve(
                approval_id="approval-123",
                reviewed_by="bob",
                comments="Looks good!",
            )

            assert result["approval_id"] == "approval-123"
            assert result["reviewed_by"] == "bob"
            assert result["status"] == "approved"

    def test_skills_reject(self, nx: NexusFS) -> None:
        """Test rejecting a skill."""
        with patch.object(nx, "_get_skill_governance") as mock_get_gov:
            mock_gov = MagicMock()
            mock_gov.reject_skill = AsyncMock()
            mock_get_gov.return_value = mock_gov

            result = nx.skills_reject(
                approval_id="approval-123",
                reviewed_by="charlie",
                comments="Needs more work",
            )

            assert result["approval_id"] == "approval-123"
            assert result["reviewed_by"] == "charlie"
            assert result["status"] == "rejected"

    def test_skills_list_approvals(self, nx: NexusFS) -> None:
        """Test listing approval requests."""
        from enum import Enum

        class MockStatus(Enum):
            PENDING = "pending"
            APPROVED = "approved"
            REJECTED = "rejected"

        with patch.object(nx, "_get_skill_governance") as mock_get_gov:
            mock_gov = MagicMock()
            mock_gov.list_approvals = AsyncMock(
                return_value=[
                    MagicMock(
                        approval_id="approval-1",
                        skill_name="skill1",
                        status=MockStatus.PENDING,
                        submitted_by="alice",
                        submitted_at=None,
                        reviewed_by=None,
                        reviewed_at=None,
                        comments=None,
                    ),
                    MagicMock(
                        approval_id="approval-2",
                        skill_name="skill2",
                        status=MockStatus.APPROVED,
                        submitted_by="bob",
                        submitted_at=None,
                        reviewed_by="charlie",
                        reviewed_at=None,
                        comments="Approved",
                    ),
                ]
            )
            mock_get_gov.return_value = mock_gov

            result = nx.skills_list_approvals()

            assert "approvals" in result
            assert result["count"] == 2

    def test_skills_list_approvals_filter_by_status(self, nx: NexusFS) -> None:
        """Test filtering approvals by status."""
        from enum import Enum

        class MockStatus(Enum):
            PENDING = "pending"

        with patch.object(nx, "_get_skill_governance") as mock_get_gov:
            mock_gov = MagicMock()
            mock_gov.list_approvals = AsyncMock(
                return_value=[
                    MagicMock(
                        approval_id="approval-1",
                        skill_name="skill1",
                        status=MockStatus.PENDING,
                        submitted_by="alice",
                        submitted_at=None,
                        reviewed_by=None,
                        reviewed_at=None,
                        comments=None,
                    ),
                ]
            )
            mock_get_gov.return_value = mock_gov

            nx.skills_list_approvals(status="pending")

            mock_gov.list_approvals.assert_called_once_with(status="pending", skill_name=None)


class TestSkillsApprovalReviewerTypes:
    """Tests for different reviewer types in approval workflow."""

    def test_skills_approve_user_reviewer(self, nx: NexusFS) -> None:
        """Test approval by user reviewer."""
        with patch.object(nx, "_get_skill_governance") as mock_get_gov:
            mock_gov = MagicMock()
            mock_gov.approve_skill = AsyncMock()
            mock_get_gov.return_value = mock_gov

            result = nx.skills_approve(
                approval_id="approval-123",
                reviewed_by="human-reviewer",
                reviewer_type="user",
            )

            assert result["reviewer_type"] == "user"
            mock_gov.approve_skill.assert_called_once()
            call_kwargs = mock_gov.approve_skill.call_args[1]
            assert call_kwargs["reviewer_type"] == "user"

    def test_skills_approve_agent_reviewer(self, nx: NexusFS) -> None:
        """Test approval by agent reviewer."""
        with patch.object(nx, "_get_skill_governance") as mock_get_gov:
            mock_gov = MagicMock()
            mock_gov.approve_skill = AsyncMock()
            mock_get_gov.return_value = mock_gov

            result = nx.skills_approve(
                approval_id="approval-123",
                reviewed_by="review-bot",
                reviewer_type="agent",
            )

            assert result["reviewer_type"] == "agent"


class TestSkillsEdgeCases:
    """Tests for edge cases in skills operations."""

    def test_skills_list_empty(self, nx: NexusFS) -> None:
        """Test listing when no skills exist."""
        with patch.object(nx, "_get_skill_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.discover = AsyncMock()
            mock_registry.list_skills.return_value = []
            mock_get_registry.return_value = mock_registry

            result = nx.skills_list()

            assert result["count"] == 0
            assert result["skills"] == []

    def test_skills_search_empty_results(self, nx: NexusFS) -> None:
        """Test searching with no results."""
        with patch.object(nx, "_get_skill_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.search_skills = AsyncMock(return_value=[])
            mock_get_manager.return_value = mock_manager

            result = nx.skills_search(query="nonexistent")

            assert result["count"] == 0
            assert result["results"] == []

    def test_skills_list_with_timestamps(self, nx: NexusFS) -> None:
        """Test listing skills with timestamps."""
        from datetime import datetime

        with patch.object(nx, "_get_skill_registry") as mock_get_registry:
            mock_registry = MagicMock()
            mock_registry.discover = AsyncMock()
            mock_registry.list_skills.return_value = [
                MagicMock(
                    name="skill-with-dates",
                    description="Skill with dates",
                    version="1.0.0",
                    author="alice",
                    tier="agent",
                    file_path="/skills/agent/skill-with-dates.md",
                    requires=[],
                    created_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
                    modified_at=datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC),
                ),
            ]
            mock_get_registry.return_value = mock_registry

            result = nx.skills_list()

            skill = result["skills"][0]
            assert "created_at" in skill
            assert "modified_at" in skill
