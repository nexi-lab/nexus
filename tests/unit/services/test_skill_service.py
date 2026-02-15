"""Unit tests for SkillService.

Tests skill discovery, subscription, loading, sharing, and permission checks.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.core.exceptions import PermissionDeniedError, ValidationError
from nexus.core.permissions import OperationContext
from nexus.services.skill_service import SkillService
from nexus.skills.types import PromptContext, SkillContent, SkillInfo

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_gateway():
    """Create a mock NexusFSGateway with standard skill configuration."""
    gw = MagicMock()

    # Gateway file operations
    gw.read.return_value = b""
    gw.write.return_value = None
    gw.mkdir.return_value = None
    gw.exists.return_value = False
    gw.list.return_value = []

    # Gateway ReBAC operations (Issue #1287: no more _fs reach-through)
    gw.rebac_create.return_value = {"tuple_id": "tuple-123"}
    gw.rebac_list_tuples.return_value = []
    gw.rebac_delete.return_value = True
    gw.rebac_check.return_value = False

    # ReBAC manager via gateway property
    mock_rebac = MagicMock()
    mock_rebac.rebac_check.return_value = False
    mock_rebac.rebac_delete.return_value = None
    gw.rebac_manager = mock_rebac

    # Metadata cache invalidation
    gw.invalidate_metadata_cache.return_value = None

    return gw


@pytest.fixture
def skill_service(mock_gateway):
    """Create a SkillService with a mock gateway."""
    return SkillService(gateway=mock_gateway)


@pytest.fixture
def operation_context():
    """Standard operation context for tests."""
    return OperationContext(
        user="alice",
        groups=["developers"],
        zone_id="acme",
        is_system=False,
        is_admin=False,
    )


@pytest.fixture
def skill_path():
    """Standard skill path for tests."""
    return "/zone/acme/user/alice/skill/code-review/"


# =============================================================================
# SkillService initialization
# =============================================================================


class TestSkillServiceInit:
    """Tests for SkillService construction."""

    def test_init_stores_gateway(self, mock_gateway):
        """SkillService stores the gateway reference."""
        service = SkillService(gateway=mock_gateway)
        assert service._gw is mock_gateway


# =============================================================================
# Context validation
# =============================================================================


class TestContextValidation:
    """Tests for _validate_context."""

    def test_raises_on_none_context(self, skill_service):
        """ValidationError is raised when context is None."""
        with pytest.raises(ValidationError, match="Context with zone_id and user_id required"):
            skill_service._validate_context(None)

    def test_raises_on_missing_zone_id(self, skill_service):
        """ValidationError is raised when zone_id is None."""
        ctx = OperationContext(user="alice", groups=[], zone_id=None)
        with pytest.raises(ValidationError, match="Context with zone_id and user_id required"):
            skill_service._validate_context(ctx)

    def test_raises_on_missing_user_id(self, skill_service):
        """ValidationError is raised when user_id is None."""
        ctx = OperationContext(user="alice", groups=[], zone_id="acme")
        # user_id auto-populates from user, so we must override
        ctx.user_id = None
        with pytest.raises(ValidationError, match="Context with zone_id and user_id required"):
            skill_service._validate_context(ctx)

    def test_valid_context_passes(self, skill_service, operation_context):
        """A valid context does not raise."""
        skill_service._validate_context(operation_context)  # Should not raise


# =============================================================================
# Share / Unshare
# =============================================================================


class TestShare:
    """Tests for share and unshare methods."""

    def test_share_public(self, skill_service, mock_gateway, operation_context, skill_path):
        """Sharing as public creates a direct_viewer tuple with role:public."""
        # Owner check
        mock_gateway.rebac_manager.rebac_check.return_value = True

        result = skill_service.share(skill_path, "public", operation_context)

        assert result == "tuple-123"
        mock_gateway.rebac_create.assert_called_once()
        call_kwargs = mock_gateway.rebac_create.call_args.kwargs
        assert call_kwargs["subject"] == ("role", "public")
        assert call_kwargs["relation"] == "direct_viewer"

    def test_share_with_user(self, skill_service, mock_gateway, operation_context, skill_path):
        """Sharing with a specific user creates the correct tuple."""
        mock_gateway.rebac_manager.rebac_check.return_value = True

        result = skill_service.share(skill_path, "user:bob", operation_context)

        assert result == "tuple-123"
        call_kwargs = mock_gateway.rebac_create.call_args.kwargs
        assert call_kwargs["subject"] == ("user", "bob")

    def test_share_with_zone(self, skill_service, mock_gateway, operation_context, skill_path):
        """Sharing with zone creates a zone member tuple."""
        mock_gateway.rebac_manager.rebac_check.return_value = True

        skill_service.share(skill_path, "zone", operation_context)

        call_kwargs = mock_gateway.rebac_create.call_args.kwargs
        assert call_kwargs["subject"] == ("zone", "acme", "member")

    def test_share_requires_ownership(
        self, skill_service, mock_gateway, operation_context, skill_path
    ):
        """Sharing fails if user does not own the skill."""
        mock_gateway.rebac_manager.rebac_check.return_value = False

        with pytest.raises(PermissionDeniedError, match="does not own skill"):
            skill_service.share(skill_path, "public", operation_context)

    def test_share_validates_context(self, skill_service, skill_path):
        """Sharing requires a valid context."""
        with pytest.raises(ValidationError):
            skill_service.share(skill_path, "public", None)

    def test_share_invalid_target(self, skill_service, mock_gateway, operation_context, skill_path):
        """Invalid share_with format raises ValidationError."""
        mock_gateway.rebac_manager.rebac_check.return_value = True

        with pytest.raises(ValidationError, match="Invalid share_with format"):
            skill_service.share(skill_path, "invalid_target", operation_context)

    def test_unshare_returns_false_when_not_shared(
        self, skill_service, mock_gateway, operation_context, skill_path
    ):
        """Unsharing returns False when no matching tuple exists."""
        mock_gateway.rebac_manager.rebac_check.return_value = True
        mock_gateway.rebac_list_tuples.return_value = []

        result = skill_service.unshare(skill_path, "public", operation_context)
        assert result is False

    def test_unshare_deletes_tuple(
        self, skill_service, mock_gateway, operation_context, skill_path
    ):
        """Unsharing deletes the matching permission tuple."""
        mock_gateway.rebac_manager.rebac_check.return_value = True
        mock_gateway.rebac_list_tuples.return_value = [{"tuple_id": "tuple-to-delete"}]

        result = skill_service.unshare(skill_path, "public", operation_context)

        assert result is True
        mock_gateway.rebac_manager.rebac_delete.assert_called_once_with("tuple-to-delete")


# =============================================================================
# Subscribe / Unsubscribe
# =============================================================================


class TestSubscription:
    """Tests for subscribe and unsubscribe methods."""

    def test_subscribe_adds_skill(self, skill_service, mock_gateway, operation_context, skill_path):
        """Subscribing adds the skill to the user's subscriptions."""
        # Can read the skill
        mock_gateway.rebac_manager.rebac_check.return_value = True
        # No existing subscriptions
        mock_gateway.read.side_effect = FileNotFoundError("not found")

        result = skill_service.subscribe(skill_path, operation_context)

        assert result is True
        mock_gateway.write.assert_called_once()

    def test_subscribe_returns_false_if_already_subscribed(
        self, skill_service, mock_gateway, operation_context, skill_path
    ):
        """Subscribing to an already-subscribed skill returns False."""
        mock_gateway.rebac_manager.rebac_check.return_value = True

        import yaml

        content = yaml.dump({"subscribed_skills": [skill_path]})
        mock_gateway.read.return_value = content.encode("utf-8")

        result = skill_service.subscribe(skill_path, operation_context)
        assert result is False

    def test_subscribe_requires_read_permission(
        self, skill_service, mock_gateway, operation_context, skill_path
    ):
        """Subscribing to a skill the user cannot read raises PermissionDeniedError."""
        mock_gateway.rebac_manager.rebac_check.return_value = False

        with pytest.raises(PermissionDeniedError, match="cannot read skill"):
            skill_service.subscribe(skill_path, operation_context)

    def test_unsubscribe_removes_skill(
        self, skill_service, mock_gateway, operation_context, skill_path
    ):
        """Unsubscribing removes the skill from subscriptions."""
        import yaml

        content = yaml.dump({"subscribed_skills": [skill_path]})
        mock_gateway.read.return_value = content.encode("utf-8")

        result = skill_service.unsubscribe(skill_path, operation_context)

        assert result is True
        mock_gateway.write.assert_called_once()

    def test_unsubscribe_returns_false_if_not_subscribed(
        self, skill_service, mock_gateway, operation_context, skill_path
    ):
        """Unsubscribing from a skill not in the list returns False."""
        mock_gateway.read.side_effect = FileNotFoundError("not found")

        result = skill_service.unsubscribe(skill_path, operation_context)
        assert result is False


# =============================================================================
# Skill loading
# =============================================================================


class TestLoad:
    """Tests for the load method."""

    def test_load_returns_skill_content(
        self, skill_service, mock_gateway, operation_context, skill_path
    ):
        """Loading a skill returns SkillContent with parsed metadata."""
        mock_gateway.rebac_manager.rebac_check.return_value = True

        skill_md = b"""---
name: Code Review
description: Automated code review skill
version: "1.0"
---
# Code Review

Review code for best practices.
"""
        mock_gateway.read.return_value = skill_md

        result = skill_service.load(skill_path, operation_context)

        assert isinstance(result, SkillContent)
        assert result.name == "Code Review"
        assert result.description == "Automated code review skill"
        assert "Review code" in result.content

    def test_load_without_frontmatter(
        self, skill_service, mock_gateway, operation_context, skill_path
    ):
        """Loading a skill without YAML frontmatter extracts name from heading."""
        mock_gateway.rebac_manager.rebac_check.return_value = True

        skill_md = b"# My Skill\n\nDo something useful."
        mock_gateway.read.return_value = skill_md

        result = skill_service.load(skill_path, operation_context)

        assert isinstance(result, SkillContent)
        assert result.metadata.get("name") == "My Skill"

    def test_load_requires_read_permission(
        self, skill_service, mock_gateway, operation_context, skill_path
    ):
        """Loading a skill requires read permission."""
        mock_gateway.rebac_manager.rebac_check.return_value = False

        with pytest.raises(PermissionDeniedError, match="cannot read skill"):
            skill_service.load(skill_path, operation_context)

    def test_load_handles_read_error(
        self, skill_service, mock_gateway, operation_context, skill_path
    ):
        """Read errors during load raise ValidationError."""
        mock_gateway.rebac_manager.rebac_check.return_value = True
        mock_gateway.read.side_effect = OSError("Storage unavailable")

        with pytest.raises(ValidationError, match="Failed to read skill content"):
            skill_service.load(skill_path, operation_context)


# =============================================================================
# _load_subscriptions (YAML parsing)
# =============================================================================


class TestLoadSubscriptions:
    """Tests for _load_subscriptions with various inputs."""

    def test_load_valid_yaml(self, skill_service, mock_gateway, operation_context):
        """Valid YAML with subscribed_skills list is parsed correctly."""
        import yaml

        data = {"subscribed_skills": ["/zone/acme/user/bob/skill/testing/"]}
        mock_gateway.read.return_value = yaml.dump(data).encode("utf-8")

        result = skill_service._load_subscriptions(operation_context)

        assert result == ["/zone/acme/user/bob/skill/testing/"]

    def test_load_missing_file_returns_empty(self, skill_service, mock_gateway, operation_context):
        """Missing subscriptions file returns empty list."""
        mock_gateway.read.side_effect = FileNotFoundError("not found")

        result = skill_service._load_subscriptions(operation_context)
        assert result == []

    def test_load_invalid_yaml_returns_empty(self, skill_service, mock_gateway, operation_context):
        """Invalid YAML content returns empty list."""
        mock_gateway.read.return_value = b"{{invalid yaml: ["

        result = skill_service._load_subscriptions(operation_context)
        assert result == []

    def test_load_empty_file_returns_empty(self, skill_service, mock_gateway, operation_context):
        """Empty file returns empty list."""
        mock_gateway.read.return_value = b""

        result = skill_service._load_subscriptions(operation_context)
        assert result == []

    def test_load_yaml_without_key_returns_empty(
        self, skill_service, mock_gateway, operation_context
    ):
        """YAML without subscribed_skills key returns empty list."""
        import yaml

        data = {"other_key": "value"}
        mock_gateway.read.return_value = yaml.dump(data).encode("utf-8")

        result = skill_service._load_subscriptions(operation_context)
        assert result == []


# =============================================================================
# Discover tests
# =============================================================================


class TestDiscover:
    """Tests for the discover method."""

    def test_discover_subscribed_filter(self, skill_service, mock_gateway, operation_context):
        """Discover with 'subscribed' filter returns subscribed skills."""
        import yaml

        # Set up subscriptions
        data = {"subscribed_skills": ["/zone/acme/user/alice/skill/testing/"]}
        mock_gateway.read.return_value = yaml.dump(data).encode("utf-8")

        # rebac_check returns False for public check
        mock_gateway.rebac_manager.rebac_check.return_value = False

        result = skill_service.discover(operation_context, filter="subscribed")

        assert len(result) == 1
        assert isinstance(result[0], SkillInfo)
        assert result[0].path == "/zone/acme/user/alice/skill/testing/"
        assert result[0].is_subscribed is True

    def test_discover_validates_context(self, skill_service):
        """Discover requires a valid context."""
        with pytest.raises(ValidationError):
            skill_service.discover(None)


# =============================================================================
# _parse_share_target
# =============================================================================


class TestParseShareTarget:
    """Tests for _parse_share_target helper."""

    def test_parse_public(self, skill_service, operation_context):
        """'public' target parses to role:public tuple."""
        result = skill_service._parse_share_target("public", operation_context)
        assert result == ("role", "public")

    def test_parse_zone(self, skill_service, operation_context):
        """'zone' target parses to zone member tuple."""
        result = skill_service._parse_share_target("zone", operation_context)
        assert result == ("zone", "acme", "member")

    def test_parse_group(self, skill_service, operation_context):
        """'group:<name>' target parses to group member tuple."""
        result = skill_service._parse_share_target("group:admins", operation_context)
        assert result == ("group", "admins", "member")

    def test_parse_user(self, skill_service, operation_context):
        """'user:<id>' target parses to user tuple."""
        result = skill_service._parse_share_target("user:bob", operation_context)
        assert result == ("user", "bob")

    def test_parse_agent(self, skill_service, operation_context):
        """'agent:<id>' target parses to agent tuple."""
        result = skill_service._parse_share_target("agent:bot1", operation_context)
        assert result == ("agent", "bot1")

    def test_parse_empty_group_raises(self, skill_service, operation_context):
        """Empty group name raises ValidationError."""
        with pytest.raises(ValidationError, match="Group name cannot be empty"):
            skill_service._parse_share_target("group:", operation_context)

    def test_parse_empty_user_raises(self, skill_service, operation_context):
        """Empty user ID raises ValidationError."""
        with pytest.raises(ValidationError, match="User ID cannot be empty"):
            skill_service._parse_share_target("user:", operation_context)

    def test_parse_invalid_format_raises(self, skill_service, operation_context):
        """Invalid format raises ValidationError."""
        with pytest.raises(ValidationError, match="Invalid share_with format"):
            skill_service._parse_share_target("bad_format", operation_context)


# =============================================================================
# _extract_owner_from_path
# =============================================================================


class TestExtractOwnerFromPath:
    """Tests for _extract_owner_from_path helper."""

    def test_extracts_user_from_standard_path(self, skill_service):
        """Owner is extracted from /zone/{zone}/user/{user}/skill/{name}/ paths."""
        result = skill_service._extract_owner_from_path("/zone/acme/user/alice/skill/code-review/")
        assert result == "alice"

    def test_returns_unknown_for_non_standard_path(self, skill_service):
        """Non-standard paths return 'unknown'."""
        result = skill_service._extract_owner_from_path("/skill/system-skill/")
        assert result == "unknown"


# =============================================================================
# _parse_skill_content
# =============================================================================


class TestParseSkillContent:
    """Tests for SKILL.md content parsing."""

    def test_parse_with_frontmatter(self, skill_service):
        """Content with YAML frontmatter is parsed into metadata and body."""
        content = """---
name: Test Skill
description: A test skill
version: "2.0"
tags:
  - testing
  - automation
---
# Test Skill

Instructions go here.
"""
        metadata, body = skill_service._parse_skill_content(content)

        assert metadata["name"] == "Test Skill"
        assert metadata["description"] == "A test skill"
        assert metadata["version"] == "2.0"
        assert "testing" in metadata["tags"]
        assert "Instructions go here." in body

    def test_parse_without_frontmatter(self, skill_service):
        """Content without frontmatter extracts name from heading."""
        content = "# My Great Skill\n\nSome instructions."
        metadata, body = skill_service._parse_skill_content(content)

        assert metadata.get("name") == "My Great Skill"

    def test_parse_empty_content(self, skill_service):
        """Empty content returns empty metadata."""
        metadata, body = skill_service._parse_skill_content("")
        assert metadata == {}


# =============================================================================
# get_prompt_context
# =============================================================================


class TestGetPromptContext:
    """Tests for get_prompt_context method."""

    def test_returns_prompt_context(self, skill_service, mock_gateway, operation_context):
        """get_prompt_context returns a PromptContext instance."""
        # No subscriptions
        mock_gateway.read.side_effect = FileNotFoundError("not found")

        result = skill_service.get_prompt_context(operation_context)

        assert isinstance(result, PromptContext)
        assert result.count == 0
        assert "<available_skills>" in result.xml

    def test_includes_subscribed_skills(self, skill_service, mock_gateway, operation_context):
        """get_prompt_context includes metadata from subscribed skills."""
        import yaml

        skill_path = "/zone/acme/user/alice/skill/testing/"
        subs = {"subscribed_skills": [skill_path]}
        subs_yaml = yaml.dump(subs).encode("utf-8")

        # First read: subscriptions file, second read: SKILL.md
        skill_md = b"---\nname: Testing\ndescription: Test runner\n---\nRun tests."

        def read_side_effect(path, **kwargs):
            if ".subscribed.yaml" in path:
                return subs_yaml
            return skill_md

        mock_gateway.read.side_effect = read_side_effect

        # User can read the skill
        mock_gateway.rebac_manager.rebac_check.return_value = True

        result = skill_service.get_prompt_context(operation_context)

        assert result.count == 1
        assert result.skills[0].name == "Testing"

    def test_validates_context(self, skill_service):
        """get_prompt_context requires a valid context."""
        with pytest.raises(ValidationError):
            skill_service.get_prompt_context(None)
