"""Unit tests for SkillService.

Tests the SkillService class directly, without going through the mixin.
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.exceptions import PermissionDeniedError, ValidationError
from nexus.core.permissions import OperationContext
from nexus.services.gateway import NexusFSGateway
from nexus.services.skill_service import SkillService
from nexus.skills.types import PromptContext, SkillContent, SkillInfo
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.storage.sqlalchemy_metadata_store import SQLAlchemyMetadataStore


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
    """Create a NexusFS instance for testing."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        metadata_store=SQLAlchemyMetadataStore(db_path=temp_dir / "metadata.db"),
        record_store=SQLAlchemyRecordStore(db_path=temp_dir / "metadata.db"),
        auto_parse=False,
        enforce_permissions=False,
    )
    nx._rebac_manager = mock_rebac
    yield nx
    nx.close()


@pytest.fixture
def service(nx: NexusFS) -> SkillService:
    """Create a SkillService instance for testing."""
    gateway = NexusFSGateway(nx)
    return SkillService(gateway=gateway)


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


class TestSkillServiceInit:
    """Tests for SkillService initialization."""

    def test_init_with_gateway(self, nx: NexusFS) -> None:
        """Test initialization with gateway."""
        gateway = NexusFSGateway(nx)
        service = SkillService(gateway=gateway)
        assert service._gw is gateway

    def test_get_rebac_from_gateway(self, nx: NexusFS) -> None:
        """Test getting ReBAC from gateway's underlying NexusFS."""
        gateway = NexusFSGateway(nx)
        service = SkillService(gateway=gateway)
        rebac = service._get_rebac()
        assert rebac is nx._rebac_manager


class TestSkillServiceShare:
    """Tests for SkillService.share()."""

    def test_share_public(
        self, service: SkillService, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test sharing skill publicly."""
        result = service.share("/zone/acme/user:alice/skill/test/", "public", context)

        assert result == "tuple-123"
        # NexusFS.rebac_create internally calls _rebac_manager.rebac_write
        mock_rebac.rebac_write.assert_called_once()
        call_kwargs = mock_rebac.rebac_write.call_args[1]
        assert call_kwargs["subject"] == ("role", "public")
        assert call_kwargs["relation"] == "direct_viewer"

    def test_share_group(
        self, service: SkillService, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test sharing with a group."""
        service.share("/zone/acme/user:alice/skill/test/", "group:eng", context)

        call_kwargs = mock_rebac.rebac_write.call_args[1]
        assert call_kwargs["subject"] == ("group", "eng", "member")

    def test_share_user(
        self, service: SkillService, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test sharing with a user."""
        service.share("/zone/acme/user:alice/skill/test/", "user:bob", context)

        call_kwargs = mock_rebac.rebac_write.call_args[1]
        assert call_kwargs["subject"] == ("user", "bob")

    def test_share_invalid_format(self, service: SkillService, context: OperationContext) -> None:
        """Test sharing with invalid format raises error."""
        with pytest.raises(ValidationError, match="Invalid share_with format"):
            service.share("/skill/test/", "invalid", context)

    def test_share_without_ownership(
        self, service: SkillService, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test sharing fails without ownership."""
        mock_rebac.rebac_check.return_value = False

        with pytest.raises(PermissionDeniedError, match="does not own"):
            service.share("/skill/test/", "public", context)


class TestSkillServiceUnshare:
    """Tests for SkillService.unshare()."""

    def test_unshare_success(
        self, service: SkillService, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test successful unsharing."""
        # NexusFS.rebac_list_tuples does direct SQL, so we patch the method on the instance
        with patch.object(nx, "rebac_list_tuples", return_value=[{"tuple_id": "tuple-456"}]):
            result = service.unshare("/skill/test/", "public", context)

        assert result is True
        mock_rebac.rebac_delete.assert_called_once_with("tuple-456")

    def test_unshare_not_found(
        self, service: SkillService, nx: NexusFS, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test unsharing when no share exists."""
        # NexusFS.rebac_list_tuples does direct SQL, so we patch the method on the instance
        with patch.object(nx, "rebac_list_tuples", return_value=[]):
            result = service.unshare("/skill/test/", "public", context)

        assert result is False
        mock_rebac.rebac_delete.assert_not_called()


class TestSkillServiceSubscribe:
    """Tests for SkillService.subscribe() and unsubscribe()."""

    def test_subscribe_new(self, service: SkillService, context: OperationContext) -> None:
        """Test subscribing to a new skill."""
        result = service.subscribe("/skill/test/", context)

        assert result is True

        # Verify saved
        subscriptions = service._load_subscriptions(context)
        assert "/skill/test/" in subscriptions

    def test_subscribe_already_subscribed(
        self, service: SkillService, context: OperationContext
    ) -> None:
        """Test subscribing when already subscribed."""
        service.subscribe("/skill/test/", context)
        result = service.subscribe("/skill/test/", context)

        assert result is False

    def test_subscribe_permission_denied(
        self, service: SkillService, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test subscribe fails without read permission."""
        mock_rebac.rebac_check.return_value = False

        with pytest.raises(PermissionDeniedError, match="cannot read"):
            service.subscribe("/skill/private/", context)

    def test_unsubscribe_success(self, service: SkillService, context: OperationContext) -> None:
        """Test unsubscribing."""
        service.subscribe("/skill/test/", context)
        result = service.unsubscribe("/skill/test/", context)

        assert result is True

        subscriptions = service._load_subscriptions(context)
        assert "/skill/test/" not in subscriptions

    def test_unsubscribe_not_subscribed(
        self, service: SkillService, context: OperationContext
    ) -> None:
        """Test unsubscribing when not subscribed."""
        result = service.unsubscribe("/skill/not-subscribed/", context)

        assert result is False


class TestSkillServiceDiscover:
    """Tests for SkillService.discover()."""

    def test_discover_empty(self, service: SkillService, context: OperationContext) -> None:
        """Test discover returns empty when no skills."""
        result = service.discover(context)

        assert result == []

    def test_discover_returns_skill_info(
        self, service: SkillService, nx: NexusFS, context: OperationContext
    ) -> None:
        """Test discover returns SkillInfo objects."""
        # Create a skill
        nx.write(
            "/zone/acme/user:alice/skill/test/SKILL.md",
            b"---\nname: Test\ndescription: A test\nauthor: alice\n---\nContent",
            context=context,
        )

        result = service.discover(context)

        assert len(result) == 1
        assert isinstance(result[0], SkillInfo)
        assert result[0].name == "Test"
        assert result[0].description == "A test"

    def test_discover_filter_subscribed(
        self, service: SkillService, nx: NexusFS, context: OperationContext
    ) -> None:
        """Test discover with subscribed filter."""
        # Create two skills
        nx.write(
            "/zone/acme/user:alice/skill/sub/SKILL.md",
            b"---\nname: Subscribed\n---\n",
            context=context,
        )
        nx.write(
            "/zone/acme/user:alice/skill/unsub/SKILL.md",
            b"---\nname: NotSubscribed\n---\n",
            context=context,
        )

        # Subscribe to one
        service.subscribe("/zone/acme/user:alice/skill/sub/", context)

        result = service.discover(context, filter="subscribed")

        assert len(result) == 1
        assert result[0].name == "Subscribed"


class TestSkillServiceLoad:
    """Tests for SkillService.load()."""

    def test_load_success(
        self, service: SkillService, nx: NexusFS, context: OperationContext
    ) -> None:
        """Test loading a skill."""
        skill_path = "/zone/acme/user:alice/skill/test/"
        nx.write(
            f"{skill_path}SKILL.md",
            b"---\nname: Test\ndescription: A test\nauthor: alice\nversion: '1.0'\n---\n# Content\nHere",
            context=context,
        )

        result = service.load(skill_path, context)

        assert isinstance(result, SkillContent)
        assert result.name == "Test"
        assert result.description == "A test"
        assert result.owner == "alice"
        assert "# Content" in result.content
        assert result.metadata["version"] == "1.0"

    def test_load_permission_denied(
        self, service: SkillService, context: OperationContext, mock_rebac: MagicMock
    ) -> None:
        """Test load fails without permission."""
        mock_rebac.rebac_check.return_value = False

        with pytest.raises(PermissionDeniedError, match="cannot read"):
            service.load("/skill/private/", context)


class TestSkillServicePromptContext:
    """Tests for SkillService.get_prompt_context()."""

    def test_prompt_context_empty(self, service: SkillService, context: OperationContext) -> None:
        """Test prompt context with no subscriptions."""
        result = service.get_prompt_context(context)

        assert isinstance(result, PromptContext)
        assert result.count == 0
        assert result.skills == []
        assert "<available_skills>" in result.xml

    def test_prompt_context_with_skills(
        self, service: SkillService, nx: NexusFS, context: OperationContext
    ) -> None:
        """Test prompt context with subscribed skills."""
        skill_path = "/zone/acme/user:alice/skill/test/"
        nx.write(
            f"{skill_path}SKILL.md",
            b"---\nname: PromptSkill\ndescription: For prompts\n---\nContent",
            context=context,
        )

        service.subscribe(skill_path, context)

        result = service.get_prompt_context(context)

        assert result.count == 1
        assert "PromptSkill" in result.xml
        assert result.token_estimate > 0


class TestSkillServiceValidation:
    """Tests for SkillService validation."""

    def test_validate_context_none(self, service: SkillService) -> None:
        """Test validation fails with None context."""
        with pytest.raises(ValidationError, match="Context"):
            service._validate_context(None)

    def test_validate_context_missing_zone(self, service: SkillService) -> None:
        """Test validation fails without zone_id."""
        ctx = OperationContext(
            user="alice",
            groups=[],
            zone_id=None,
            user_id="alice",
        )
        with pytest.raises(ValidationError, match="zone_id"):
            service._validate_context(ctx)

    def test_validate_context_empty_user_id(self, service: SkillService) -> None:
        """Test validation fails with empty user_id."""
        ctx = OperationContext(
            user="alice",
            groups=[],
            zone_id="acme",
            user_id="",  # Empty string
        )
        with pytest.raises(ValidationError, match="user_id"):
            service._validate_context(ctx)
