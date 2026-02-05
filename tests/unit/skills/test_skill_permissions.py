"""Unit tests for permission-based skill APIs.

Tests cover the new permission-based skill management operations:
- skills_share: Grant read permission on a skill
- skills_unshare: Revoke read permission on a skill
- skills_discover: List skills user has permission to see
- skills_subscribe: Add skill to user's library
- skills_unsubscribe: Remove skill from user's library
- skills_get_prompt_context: Get skill metadata for system prompt
- skills_load: Load full skill content on-demand
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_rebac() -> MagicMock:
    """Create a mock ReBAC manager."""
    from nexus.core.rebac_manager_enhanced import WriteResult

    rebac = MagicMock()
    rebac.rebac_check = MagicMock(return_value=True)
    # NexusFS.rebac_create internally calls _rebac_manager.rebac_write
    # Return a WriteResult object since rebac_create accesses its attributes
    rebac.rebac_write = MagicMock(
        return_value=WriteResult(
            tuple_id="tuple-123", revision=0, consistency_token="v0", written_at_ms=0.0
        )
    )
    rebac.rebac_delete = MagicMock(return_value=True)
    rebac.rebac_list_tuples = MagicMock(return_value=[])
    return rebac


@pytest.fixture
def nx(temp_dir: Path, mock_rebac: MagicMock) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance for testing.

    Permissions are disabled to allow unit tests to focus on skill logic.
    Permission enforcement is tested separately through mocked rebac methods.
    """
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=False,  # Disable for unit tests
    )
    # Inject mock rebac
    nx._rebac_manager = mock_rebac
    yield nx
    nx.close()


@pytest.fixture
def context() -> OperationContext:
    """Create an operation context for testing."""
    return OperationContext(
        user="alice",
        groups=["developers"],
        zone_id="acme",
        user_id="alice",
        is_admin=False,
        is_system=False,
    )


class TestSkillsShare:
    """Tests for skills_share method."""

    def test_skills_share_requires_context(self, nx: NexusFS) -> None:
        """Test skills_share requires context."""
        from nexus.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            nx.skills_share("/zone/acme/user:alice/skill/test/", "public", context=None)

    def test_skills_share_public(
        self, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test sharing skill publicly."""
        skill_path = "/zone/acme/user:alice/skill/test/"

        result = nx.skills_share(skill_path, "public", context=context)

        assert result["success"] is True
        assert result["tuple_id"] == "tuple-123"
        assert result["share_with"] == "public"

        # Verify rebac_write was called with correct subject
        mock_rebac.rebac_write.assert_called_once()
        call_kwargs = mock_rebac.rebac_write.call_args[1]
        assert call_kwargs["subject"] == ("role", "public")
        assert call_kwargs["relation"] == "direct_viewer"

    def test_skills_share_group(
        self, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test sharing skill with a group."""
        skill_path = "/zone/acme/user:alice/skill/test/"

        result = nx.skills_share(skill_path, "group:engineering", context=context)

        assert result["success"] is True
        assert result["share_with"] == "group:engineering"

        # Verify userset-as-subject pattern
        call_kwargs = mock_rebac.rebac_write.call_args[1]
        assert call_kwargs["subject"] == ("group", "engineering", "member")

    def test_skills_share_user(
        self, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test sharing skill with a specific user."""
        skill_path = "/zone/acme/user:alice/skill/test/"

        result = nx.skills_share(skill_path, "user:bob", context=context)

        assert result["success"] is True
        call_kwargs = mock_rebac.rebac_write.call_args[1]
        assert call_kwargs["subject"] == ("user", "bob")

    def test_skills_share_zone(
        self, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test sharing skill with zone."""
        skill_path = "/zone/acme/user:alice/skill/test/"

        result = nx.skills_share(skill_path, "zone", context=context)

        assert result["success"] is True
        call_kwargs = mock_rebac.rebac_write.call_args[1]
        assert call_kwargs["subject"] == ("zone", "acme", "member")

    def test_skills_share_agent(
        self, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test sharing skill with an agent."""
        skill_path = "/zone/acme/user:alice/skill/test/"

        result = nx.skills_share(skill_path, "agent:agent-123", context=context)

        assert result["success"] is True
        call_kwargs = mock_rebac.rebac_write.call_args[1]
        assert call_kwargs["subject"] == ("agent", "agent-123")

    def test_skills_share_invalid_format(self, nx: NexusFS, context: OperationContext) -> None:
        """Test skills_share rejects invalid share_with format."""
        from nexus.core.exceptions import ValidationError

        with pytest.raises(ValidationError, match="Invalid share_with format"):
            nx.skills_share("/skill/test/", "invalid-format", context=context)

    def test_skills_share_permission_denied(
        self, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test skills_share fails without ownership."""
        from nexus.core.exceptions import PermissionDeniedError

        mock_rebac.rebac_check.return_value = False

        with pytest.raises(PermissionDeniedError, match="does not own"):
            nx.skills_share("/skill/other-user/", "public", context=context)


class TestSkillsUnshare:
    """Tests for skills_unshare method."""

    def test_skills_unshare_requires_context(self, nx: NexusFS) -> None:
        """Test skills_unshare requires context."""
        from nexus.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            nx.skills_unshare("/skill/test/", "public", context=None)

    def test_skills_unshare_success(
        self, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test unsharing a skill."""
        skill_path = "/zone/acme/user:alice/skill/test/"
        # NexusFS.rebac_list_tuples does direct SQL, so we patch the method on the instance
        with patch.object(nx, "rebac_list_tuples", return_value=[{"tuple_id": "tuple-123"}]):
            result = nx.skills_unshare(skill_path, "public", context=context)

        assert result["success"] is True
        mock_rebac.rebac_delete.assert_called_once_with("tuple-123")

    def test_skills_unshare_no_matching_share(
        self, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test unsharing when no matching share exists."""
        # NexusFS.rebac_list_tuples does direct SQL, so we patch the method on the instance
        with patch.object(nx, "rebac_list_tuples", return_value=[]):
            result = nx.skills_unshare("/skill/test/", "public", context=context)

        assert result["success"] is False


class TestSkillsDiscover:
    """Tests for skills_discover method."""

    def test_skills_discover_requires_context(self, nx: NexusFS) -> None:
        """Test skills_discover requires context."""
        from nexus.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            nx.skills_discover(context=None)

    def test_skills_discover_returns_empty_when_no_skills(
        self, nx: NexusFS, context: OperationContext
    ) -> None:
        """Test discover returns empty list when no skills."""
        result = nx.skills_discover(context=context)

        assert result["count"] == 0
        assert result["skills"] == []

    def test_skills_discover_with_skills(self, nx: NexusFS, context: OperationContext) -> None:
        """Test discover returns skills with permissions."""
        # Create skill using NexusFS
        skill_path = "/zone/acme/user:alice/skill/test-skill/SKILL.md"
        nx.write(
            skill_path,
            b"---\nname: Test Skill\ndescription: A test\nauthor: alice\n---\nContent",
            context=context,
        )

        result = nx.skills_discover(context=context)

        assert result["count"] == 1
        assert result["skills"][0]["name"] == "Test Skill"
        assert result["skills"][0]["is_subscribed"] is False

    def test_skills_discover_filter_subscribed(
        self, nx: NexusFS, context: OperationContext
    ) -> None:
        """Test discover filters by subscribed status."""
        # Create skill using NexusFS
        skill_path = "/zone/acme/user:alice/skill/subscribed-skill/"
        nx.write(
            f"{skill_path}SKILL.md",
            b"---\nname: Subscribed\n---\nContent",
            context=context,
        )

        # Subscribe using the service
        service = nx._get_skill_service()
        service.subscribe(skill_path, context)

        result = nx.skills_discover(filter="subscribed", context=context)

        assert result["count"] == 1
        assert result["skills"][0]["is_subscribed"] is True


class TestSkillsSubscribe:
    """Tests for skills_subscribe method."""

    def test_skills_subscribe_requires_context(self, nx: NexusFS) -> None:
        """Test skills_subscribe requires context."""
        from nexus.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            nx.skills_subscribe("/skill/test/", context=None)

    def test_skills_subscribe_success(self, nx: NexusFS, context: OperationContext) -> None:
        """Test subscribing to a skill."""
        skill_path = "/zone/acme/user:bob/skill/shared/"

        result = nx.skills_subscribe(skill_path, context=context)

        assert result["success"] is True
        assert result["skill_path"] == skill_path
        assert result["already_subscribed"] is False

    def test_skills_subscribe_already_subscribed(
        self, nx: NexusFS, context: OperationContext
    ) -> None:
        """Test subscribing when already subscribed."""
        skill_path = "/skill/test/"

        # Subscribe first
        nx.skills_subscribe(skill_path, context=context)

        # Try again
        result = nx.skills_subscribe(skill_path, context=context)

        assert result["success"] is True
        assert result["already_subscribed"] is True

    def test_skills_subscribe_permission_denied(
        self, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test subscribe fails without read permission."""
        from nexus.core.exceptions import PermissionDeniedError

        mock_rebac.rebac_check.return_value = False

        with pytest.raises(PermissionDeniedError, match="cannot read"):
            nx.skills_subscribe("/skill/private/", context=context)


class TestSkillsUnsubscribe:
    """Tests for skills_unsubscribe method."""

    def test_skills_unsubscribe_requires_context(self, nx: NexusFS) -> None:
        """Test skills_unsubscribe requires context."""
        from nexus.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            nx.skills_unsubscribe("/skill/test/", context=None)

    def test_skills_unsubscribe_success(self, nx: NexusFS, context: OperationContext) -> None:
        """Test unsubscribing from a skill."""
        skill_path = "/skill/test/"

        # Subscribe first
        nx.skills_subscribe(skill_path, context=context)

        result = nx.skills_unsubscribe(skill_path, context=context)

        assert result["success"] is True
        assert result["was_subscribed"] is True

    def test_skills_unsubscribe_not_subscribed(
        self, nx: NexusFS, context: OperationContext
    ) -> None:
        """Test unsubscribing when not subscribed."""
        result = nx.skills_unsubscribe("/skill/not-subscribed/", context=context)

        assert result["success"] is True
        assert result["was_subscribed"] is False


class TestSkillsGetPromptContext:
    """Tests for skills_get_prompt_context method."""

    def test_skills_get_prompt_context_requires_context(self, nx: NexusFS) -> None:
        """Test skills_get_prompt_context requires context."""
        from nexus.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            nx.skills_get_prompt_context(context=None)

    def test_skills_get_prompt_context_empty(self, nx: NexusFS, context: OperationContext) -> None:
        """Test prompt context with no subscriptions."""
        result = nx.skills_get_prompt_context(context=context)

        assert result["count"] == 0
        assert "<available_skills>" in result["xml"]
        assert result["skills"] == []

    def test_skills_get_prompt_context_with_subscriptions(
        self, nx: NexusFS, context: OperationContext
    ) -> None:
        """Test prompt context with subscriptions."""
        # Create skill using NexusFS
        skill_path = "/zone/acme/user:alice/skill/test-skill/"
        nx.write(
            f"{skill_path}SKILL.md",
            b"---\nname: Test Skill\ndescription: A test\nauthor: alice\n---\nContent",
            context=context,
        )

        # Subscribe
        nx.skills_subscribe(skill_path, context=context)

        result = nx.skills_get_prompt_context(context=context)

        assert result["count"] == 1
        assert "Test Skill" in result["xml"]
        assert result["skills"][0]["name"] == "Test Skill"

    def test_skills_get_prompt_context_respects_max_skills(
        self, nx: NexusFS, context: OperationContext
    ) -> None:
        """Test prompt context respects max_skills limit."""
        # Create multiple skills using NexusFS
        for i in range(5):
            skill_path = f"/zone/acme/user:alice/skill/skill-{i}/"
            nx.write(
                f"{skill_path}SKILL.md",
                f"---\nname: Skill {i}\n---\nContent".encode(),
                context=context,
            )
            nx.skills_subscribe(skill_path, context=context)

        result = nx.skills_get_prompt_context(max_skills=2, context=context)

        assert result["count"] <= 2


class TestSkillsLoad:
    """Tests for skills_load method."""

    def test_skills_load_requires_context(self, nx: NexusFS) -> None:
        """Test skills_load requires context."""
        from nexus.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            nx.skills_load("/skill/test/", context=None)

    def test_skills_load_success(self, nx: NexusFS, context: OperationContext) -> None:
        """Test loading a skill."""
        # Create skill using NexusFS
        skill_path = "/zone/acme/user:alice/skill/test-skill/"
        nx.write(
            f"{skill_path}SKILL.md",
            b"---\nname: Test Skill\ndescription: A test\nauthor: alice\nversion: 2.0.0\n---\n# Test Skill\n\nInstructions here.",
            context=context,
        )

        result = nx.skills_load(skill_path, context=context)

        assert result["name"] == "Test Skill"
        assert result["path"] == skill_path
        assert result["owner"] == "alice"
        assert "Instructions" in result["content"]
        assert result["metadata"]["version"] == "2.0.0"

    def test_skills_load_permission_denied(
        self, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test load fails without read permission."""
        from nexus.core.exceptions import PermissionDeniedError

        mock_rebac.rebac_check.return_value = False

        with pytest.raises(PermissionDeniedError, match="cannot read"):
            nx.skills_load("/skill/private/", context=context)

    def test_skills_load_without_frontmatter(self, nx: NexusFS, context: OperationContext) -> None:
        """Test loading a skill without YAML frontmatter."""
        # Create skill without frontmatter using NexusFS
        skill_path = "/zone/acme/user:alice/skill/simple/"
        nx.write(
            f"{skill_path}SKILL.md",
            b"# Simple Skill\n\nJust content.",
            context=context,
        )

        result = nx.skills_load(skill_path, context=context)

        # Name should be derived from heading
        assert result["name"] == "Simple Skill"
        assert "Just content" in result["content"]
