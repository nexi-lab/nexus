"""Targeted tests for SkillService._discover_impl paths.

Covers filter modes, metadata loading, error paths, and edge cases
that will be touched by the Phase 3 pipeline refactor (Issue #1400).
"""

from unittest.mock import MagicMock

import pytest
import yaml

from nexus.bricks.skills.skill_service_adapter import SkillService
from nexus.contracts.types import OperationContext

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_gateway():
    """Create a mock NexusFSGateway with standard skill configuration."""
    gw = MagicMock()
    gw.sys_read.return_value = b""
    gw.sys_write.return_value = None
    gw.sys_mkdir.return_value = None
    gw.sys_access.return_value = False
    gw.sys_readdir.return_value = []
    gw.rebac_create.return_value = {"tuple_id": "tuple-123"}
    gw.rebac_list_tuples.return_value = []
    gw.rebac_delete.return_value = True
    gw.rebac_check.return_value = False
    gw.invalidate_metadata_cache.return_value = None

    mock_rebac = MagicMock()
    mock_rebac.rebac_check.return_value = False
    mock_rebac.rebac_delete.return_value = None
    gw.rebac_manager = mock_rebac

    return gw


@pytest.fixture
def svc(mock_gateway):
    return SkillService(gateway=mock_gateway)


@pytest.fixture
def ctx():
    return OperationContext(
        user_id="alice",
        groups=["developers"],
        zone_id="acme",
        is_system=False,
        is_admin=False,
    )


SKILL_MD = (
    b"---\nname: Test Skill\ndescription: A test\nversion: '1.0'\ntags:\n  - test\n---\n# Test"
)

# =============================================================================
# Filter: "subscribed"
# =============================================================================


class TestDiscoverSubscribed:
    """Tests for discover(filter='subscribed')."""

    def test_returns_subscribed_skills(self, svc, mock_gateway, ctx):
        """Subscribed filter returns only skills in .subscribed.yaml."""
        subs = {"subscribed_skills": ["/zone/acme/user/alice/skill/my-skill/"]}
        mock_gateway.sys_read.side_effect = lambda path, **kw: (
            yaml.dump(subs).encode() if ".subscribed" in path else SKILL_MD
        )
        mock_gateway.rebac_manager.rebac_check.return_value = False

        result = svc.discover(ctx, filter="subscribed")

        assert len(result) == 1
        assert result[0].is_subscribed is True
        assert result[0].name == "Test Skill"

    def test_empty_subscriptions(self, svc, mock_gateway, ctx):
        """Empty subscriptions returns empty list."""
        mock_gateway.sys_read.side_effect = FileNotFoundError("not found")

        result = svc.discover(ctx, filter="subscribed")
        assert result == []

    def test_subscribed_with_public_skill(self, svc, mock_gateway, ctx):
        """Subscribed skill that is also public shows is_public=True."""
        path = "/zone/other/user/bob/skill/shared/"
        subs = {"subscribed_skills": [path]}

        def read_side(p, **kw):
            if ".subscribed" in p:
                return yaml.dump(subs).encode()
            return SKILL_MD

        mock_gateway.sys_read.side_effect = read_side
        # Batch public-set lookup: _find_public_skills uses rebac_list_tuples
        mock_gateway.rebac_list_tuples.return_value = [
            {
                "object_type": "file",
                "object_id": "/zone/other/user/bob/skill/shared",
                "subject_type": "role",
                "subject_id": "public",
                "relation": "direct_viewer",
            }
        ]

        result = svc.discover(ctx, filter="subscribed")

        assert len(result) == 1
        assert result[0].is_public is True


# =============================================================================
# Filter: "owned"
# =============================================================================


class TestDiscoverOwned:
    """Tests for discover(filter='owned')."""

    def test_returns_owned_skills(self, svc, mock_gateway, ctx):
        """Owned filter lists skills in user's skill directory."""
        user_dir = "/zone/acme/user/alice/skill/"
        skill_name = "code-review"
        skill_path = f"{user_dir}{skill_name}/"
        skill_md_path = f"{skill_path}SKILL.md"

        mock_gateway.sys_access.side_effect = lambda p, **kw: p in (user_dir, skill_md_path)
        mock_gateway.sys_readdir.return_value = [f"{user_dir}{skill_name}/SKILL.md"]
        mock_gateway.sys_read.side_effect = lambda p, **kw: b"" if ".subscribed" in p else SKILL_MD
        mock_gateway.rebac_manager.rebac_check.return_value = False

        result = svc.discover(ctx, filter="owned")

        assert len(result) == 1
        assert result[0].owner == "alice"

    def test_empty_skill_directory(self, svc, mock_gateway, ctx):
        """Empty user skill directory returns empty list."""
        mock_gateway.sys_access.return_value = False

        result = svc.discover(ctx, filter="owned")
        assert result == []


# =============================================================================
# Filter: "public"
# =============================================================================


class TestDiscoverPublic:
    """Tests for discover(filter='public')."""

    def test_returns_public_skills(self, svc, mock_gateway, ctx):
        """Public filter queries ReBAC for public tuples."""
        mock_gateway.rebac_list_tuples.return_value = [
            {
                "object_type": "file",
                "object_id": "/zone/other/user/bob/skill/shared",
                "subject_type": "role",
                "subject_id": "public",
                "relation": "direct_viewer",
            }
        ]
        mock_gateway.sys_read.side_effect = lambda p, **kw: (
            yaml.dump({"subscribed_skills": []}).encode() if ".subscribed" in p else SKILL_MD
        )

        result = svc.discover(ctx, filter="public")

        assert len(result) == 1
        assert result[0].is_public is True

    def test_no_public_skills(self, svc, mock_gateway, ctx):
        """No public skills returns empty list."""
        mock_gateway.rebac_list_tuples.return_value = []
        mock_gateway.sys_read.side_effect = FileNotFoundError("not found")

        result = svc.discover(ctx, filter="public")
        assert result == []


# =============================================================================
# Filter: "shared"
# =============================================================================


class TestDiscoverShared:
    """Tests for discover(filter='shared')."""

    def test_returns_shared_skills(self, svc, mock_gateway, ctx):
        """Shared filter queries ReBAC for direct_viewer tuples for user."""
        shared_tuple = {
            "object_type": "file",
            "object_id": "/zone/acme/user/bob/skill/testing",
            "subject_type": "user",
            "subject_id": "alice",
            "relation": "direct_viewer",
        }

        def rebac_list_side(*, subject=None, relation=None, object=None):
            # Public query returns empty; shared query returns the tuple
            if subject == ("role", "public"):
                return []
            if subject == ("user", "alice"):
                return [shared_tuple]
            return []

        mock_gateway.rebac_list_tuples.side_effect = rebac_list_side
        mock_gateway.sys_read.side_effect = lambda p, **kw: (
            yaml.dump({"subscribed_skills": []}).encode() if ".subscribed" in p else SKILL_MD
        )

        result = svc.discover(ctx, filter="shared")

        assert len(result) == 1
        assert result[0].owner == "bob"

    def test_shared_skips_public(self, svc, mock_gateway, ctx):
        """Shared filter skips skills that are also public."""
        skill_tuple = {
            "object_type": "file",
            "object_id": "/zone/acme/user/bob/skill/testing",
            "subject_type": "user",
            "subject_id": "alice",
            "relation": "direct_viewer",
        }
        public_tuple = {
            "object_type": "file",
            "object_id": "/zone/acme/user/bob/skill/testing",
            "subject_type": "role",
            "subject_id": "public",
            "relation": "direct_viewer",
        }

        def rebac_list_side(*, subject=None, relation=None, object=None):
            # Both public and shared return the skill
            if subject == ("role", "public"):
                return [public_tuple]
            if subject == ("user", "alice"):
                return [skill_tuple]
            return []

        mock_gateway.rebac_list_tuples.side_effect = rebac_list_side
        mock_gateway.sys_read.side_effect = lambda p, **kw: (
            yaml.dump({"subscribed_skills": []}).encode() if ".subscribed" in p else SKILL_MD
        )

        result = svc.discover(ctx, filter="shared")
        assert result == []


# =============================================================================
# Filter: "all"
# =============================================================================


class TestDiscoverAll:
    """Tests for discover(filter='all')."""

    def test_all_collects_from_multiple_sources(self, svc, mock_gateway, ctx):
        """All filter collects from filesystem, public, and shared sources."""
        # No filesystem skills
        mock_gateway.sys_access.return_value = False
        mock_gateway.sys_readdir.return_value = []
        # No public/shared skills
        mock_gateway.rebac_list_tuples.return_value = []
        mock_gateway.sys_read.side_effect = FileNotFoundError("not found")

        result = svc.discover(ctx, filter="all")
        assert result == []


# =============================================================================
# _load_skill_metadata: system context fallback
# =============================================================================


class TestLoadSkillMetadata:
    """Tests for _load_skill_metadata system context fallback."""

    def test_fallback_to_system_context_for_public_skills(self, svc, mock_gateway, ctx):
        """When user read fails but skill is public, fallback to system context."""
        call_count = {"n": 0}

        def read_side(path, **kw):
            call_count["n"] += 1
            context = kw.get("context")
            if context and getattr(context, "is_system", False):
                return SKILL_MD
            raise PermissionError("no access")

        mock_gateway.sys_read.side_effect = read_side

        result = svc._load_skill_metadata(
            "/zone/other/user/bob/skill/public-skill/", ctx, is_public=True
        )

        assert result.get("name") == "Test Skill"
        assert call_count["n"] >= 2  # First attempt + system fallback

    def test_no_fallback_when_not_public(self, svc, mock_gateway, ctx):
        """Non-public skills don't attempt system context fallback."""
        mock_gateway.sys_read.side_effect = PermissionError("no access")

        result = svc._load_skill_metadata(
            "/zone/acme/user/alice/skill/private/", ctx, is_public=False
        )

        assert result == {}


# =============================================================================
# _find_public_skills error path
# =============================================================================


class TestFindPublicSkillsErrors:
    """Tests for _find_public_skills error handling."""

    def test_returns_empty_on_rebac_error(self, svc, mock_gateway):
        """ReBAC errors in _find_public_skills return empty list."""
        mock_gateway.rebac_list_tuples.side_effect = RuntimeError("ReBAC unavailable")

        result = svc._find_public_skills()
        assert result == []

    def test_filters_non_skill_paths(self, svc, mock_gateway):
        """Only paths containing /skill/ are returned."""
        mock_gateway.rebac_list_tuples.return_value = [
            {"object_type": "file", "object_id": "/zone/acme/data/file.txt"},
            {"object_type": "file", "object_id": "/zone/acme/user/bob/skill/test"},
        ]

        result = svc._find_public_skills()
        assert len(result) == 1
        assert "/skill/" in result[0]


# =============================================================================
# _find_direct_viewer_skills error path
# =============================================================================


class TestFindDirectViewerSkillsErrors:
    """Tests for _find_direct_viewer_skills error handling."""

    def test_returns_empty_on_error(self, svc, mock_gateway, ctx):
        """Errors return empty list instead of raising."""
        mock_gateway.rebac_list_tuples.side_effect = RuntimeError("ReBAC unavailable")

        result = svc._find_direct_viewer_skills(ctx)
        assert result == []

    def test_extracts_skill_path_from_skill_md(self, svc, mock_gateway, ctx):
        """SKILL.md paths are converted to skill directory paths."""
        mock_gateway.rebac_list_tuples.return_value = [
            {
                "object_type": "file",
                "object_id": "/zone/acme/user/bob/skill/test/SKILL.md",
                "subject_type": "user",
                "subject_id": "alice",
                "relation": "direct_viewer",
            }
        ]

        result = svc._find_direct_viewer_skills(ctx)
        assert result == ["/zone/acme/user/bob/skill/test/"]


# =============================================================================
# _load_assigned_skills edge cases
# =============================================================================


class TestLoadAssignedSkills:
    """Tests for _load_assigned_skills with various agent_id formats."""

    def test_malformed_agent_id_no_comma(self, svc, mock_gateway, ctx):
        """Agent ID without comma separator returns empty list."""
        ctx.agent_id = "no-comma-here"
        ctx.subject_id = "no-comma-here"

        result = svc._load_assigned_skills(ctx)
        assert result == []

    def test_none_agent_id(self, svc, mock_gateway, ctx):
        """None agent_id returns empty list."""
        ctx.agent_id = None
        ctx.subject_id = None

        result = svc._load_assigned_skills(ctx)
        assert result == []

    def test_valid_agent_id_loads_config(self, svc, mock_gateway, ctx):
        """Valid agent_id format loads config.yaml and returns assigned_skills."""
        ctx.agent_id = "alice,my-agent"
        ctx.subject_id = "alice,my-agent"

        config = {
            "metadata": {
                "assigned_skills": [
                    "/zone/acme/user/alice/skill/code-review/",
                    "/zone/acme/user/alice/skill/testing/",
                ]
            }
        }
        mock_gateway.sys_read.return_value = yaml.dump(config).encode()

        result = svc._load_assigned_skills(ctx)
        assert len(result) == 2

    def test_config_without_metadata(self, svc, mock_gateway, ctx):
        """Config without metadata key returns empty list."""
        ctx.agent_id = "alice,my-agent"
        ctx.subject_id = "alice,my-agent"

        mock_gateway.sys_read.return_value = yaml.dump({"name": "my-agent"}).encode()

        result = svc._load_assigned_skills(ctx)
        assert result == []

    def test_config_read_error(self, svc, mock_gateway, ctx):
        """Error reading config returns empty list."""
        ctx.agent_id = "alice,my-agent"
        ctx.subject_id = "alice,my-agent"

        mock_gateway.sys_read.side_effect = FileNotFoundError("not found")

        result = svc._load_assigned_skills(ctx)
        assert result == []
