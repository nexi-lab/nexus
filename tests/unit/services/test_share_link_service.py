"""Unit tests for ShareLinkService.

Tests share link CRUD, password protection, expiration, access limits,
permission enforcement, and access logging.

All async service methods are tested via asyncio.run().
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.share_link.share_link_service import ShareLinkService
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    AccessDeniedError,
    ServiceUnavailableError,
    ValidationError,
)
from nexus.contracts.types import OperationContext


def _populate_model_defaults(model: object) -> None:
    """Simulate DB defaults that SQLAlchemy would set on flush/commit."""
    if getattr(model, "created_at", None) is None:
        model.created_at = datetime.now(UTC)
    if getattr(model, "link_id", None) is None:
        import uuid

        model.link_id = str(uuid.uuid4())


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_gateway():
    """Create a mock NexusFSGateway with session factory."""
    gw = MagicMock()
    gw.rebac_check.return_value = True
    gw.access = AsyncMock(return_value=True)
    gw.metadata_get.return_value = MagicMock(is_dir=False)
    # Wire session.add to populate DB-generated defaults
    session = MagicMock()
    session.add.side_effect = _populate_model_defaults
    gw.session_factory.return_value.__enter__ = MagicMock(return_value=session)
    gw.session_factory.return_value.__exit__ = MagicMock(return_value=False)
    return gw


@pytest.fixture
def service(mock_gateway):
    """Create a ShareLinkService with mock gateway."""
    return ShareLinkService(gateway=mock_gateway, enforce_permissions=True)


@pytest.fixture
def service_no_perms(mock_gateway):
    """Create a ShareLinkService with permissions disabled."""
    return ShareLinkService(gateway=mock_gateway, enforce_permissions=False)


@pytest.fixture
def context():
    """Standard operation context."""
    return OperationContext(
        user_id="test_user",
        groups=["test_group"],
        zone_id="test_zone",
        is_system=False,
        is_admin=False,
    )


@pytest.fixture
def admin_context():
    """Admin operation context."""
    return OperationContext(
        user_id="admin_user",
        groups=["admins"],
        zone_id="test_zone",
        is_system=False,
        is_admin=True,
    )


# =============================================================================
# Initialization
# =============================================================================


class TestShareLinkServiceInit:
    """Tests for ShareLinkService construction."""

    def test_init_stores_dependencies(self, mock_gateway):
        """Service stores gateway and enforce flag."""
        svc = ShareLinkService(gateway=mock_gateway, enforce_permissions=True)
        assert svc._gw is mock_gateway
        assert svc._enforce_permissions is True

    def test_init_defaults_to_enforce(self, mock_gateway):
        """Default is to enforce permissions."""
        svc = ShareLinkService(gateway=mock_gateway)
        assert svc._enforce_permissions is True

    def test_init_disable_permissions(self, mock_gateway):
        """Permissions can be disabled."""
        svc = ShareLinkService(gateway=mock_gateway, enforce_permissions=False)
        assert svc._enforce_permissions is False


# =============================================================================
# Password hashing
# =============================================================================


class TestPasswordHashing:
    """Tests for password hash/verify utilities."""

    def test_hash_password_produces_salt_colon_hash(self):
        """Hash format is 'salt:hash'."""
        result = ShareLinkService._hash_password("my_password")
        parts = result.split(":")
        assert len(parts) == 2
        assert len(parts[0]) == 32  # hex salt
        assert len(parts[1]) == 64  # sha256 hex digest

    def test_hash_password_is_non_deterministic(self):
        """Same password produces different hashes (random salt)."""
        h1 = ShareLinkService._hash_password("same_pass")
        h2 = ShareLinkService._hash_password("same_pass")
        assert h1 != h2

    def test_verify_password_correct(self):
        """Correct password verifies."""
        hashed = ShareLinkService._hash_password("secret123")
        assert ShareLinkService._verify_password("secret123", hashed) is True

    def test_verify_password_wrong(self):
        """Wrong password fails verification."""
        hashed = ShareLinkService._hash_password("secret123")
        assert ShareLinkService._verify_password("wrong_pass", hashed) is False

    def test_verify_password_empty(self):
        """Empty password doesn't match non-empty hash."""
        hashed = ShareLinkService._hash_password("secret123")
        assert ShareLinkService._verify_password("", hashed) is False

    def test_verify_password_malformed_hash(self):
        """Malformed hash returns False."""
        assert ShareLinkService._verify_password("password", "no_colon_here") is False
        assert ShareLinkService._verify_password("password", "") is False


# =============================================================================
# Context extraction
# =============================================================================


class TestContextExtraction:
    """Tests for _extract_context_info helper."""

    def test_extracts_from_context(self, context):
        """Extracts zone_id, user, is_admin from context."""
        zone_id, user_id, is_admin = ShareLinkService._extract_context_info(context)
        assert zone_id == "test_zone"
        assert user_id == "test_user"
        assert is_admin is False

    def test_extracts_admin_flag(self, admin_context):
        """Admin flag extracted correctly."""
        _, _, is_admin = ShareLinkService._extract_context_info(admin_context)
        assert is_admin is True

    def test_defaults_for_none_context(self):
        """None context returns defaults."""
        zone_id, user_id, is_admin = ShareLinkService._extract_context_info(None)
        assert zone_id == ROOT_ZONE_ID
        assert user_id == "anonymous"
        assert is_admin is False


# =============================================================================
# create_share_link
# =============================================================================


class TestCreateShareLink:
    """Tests for the create_share_link method."""

    def test_invalid_permission_level(self, service, context):
        """Invalid permission_level raises ValidationError."""
        with pytest.raises(ValidationError):
            asyncio.run(
                service.create_share_link(
                    path="/test/file.txt",
                    permission_level="invalid",
                    context=context,
                )
            )

    def test_invalid_path(self, service, context):
        """Invalid path raises ValidationError."""
        with pytest.raises(ValidationError):
            asyncio.run(
                service.create_share_link(
                    path="",
                    permission_level="viewer",
                    context=context,
                )
            )

    def test_permission_denied(self, service, mock_gateway, context):
        """Denied rebac_check raises AccessDeniedError."""
        mock_gateway.rebac_check.return_value = False
        with pytest.raises(AccessDeniedError):
            asyncio.run(
                service.create_share_link(
                    path="/test/file.txt",
                    permission_level="viewer",
                    context=context,
                )
            )

    def test_no_session_factory_returns_500(self, mock_gateway, context):
        """Missing session_factory raises ServiceUnavailableError."""
        mock_gateway.session_factory = None
        svc = ShareLinkService(gateway=mock_gateway)
        with pytest.raises(ServiceUnavailableError):
            asyncio.run(
                svc.create_share_link(
                    path="/test/file.txt",
                    permission_level="viewer",
                    context=context,
                )
            )

    def test_skips_permission_check_when_disabled(self, service_no_perms, mock_gateway, context):
        """With enforce_permissions=False, no rebac_check is called."""
        mock_gateway.rebac_check.return_value = False  # Would fail if checked
        # Should still succeed because enforce_permissions=False
        # (will fail at DB level since mock session is used)
        asyncio.run(
            service_no_perms.create_share_link(
                path="/test/file.txt",
                permission_level="viewer",
                context=context,
            )
        )
        # rebac_check should not be called
        mock_gateway.rebac_check.assert_not_called()


# =============================================================================
# get_share_link
# =============================================================================


class TestGetShareLink:
    """Tests for the get_share_link method."""

    def test_no_session_factory(self, mock_gateway):
        """Missing session_factory raises ServiceUnavailableError."""
        mock_gateway.session_factory = None
        svc = ShareLinkService(gateway=mock_gateway)
        with pytest.raises(ServiceUnavailableError):
            asyncio.run(svc.get_share_link(link_id="abc123"))


# =============================================================================
# revoke_share_link
# =============================================================================


class TestRevokeShareLink:
    """Tests for the revoke_share_link method."""

    def test_no_session_factory(self, mock_gateway):
        """Missing session_factory raises ServiceUnavailableError."""
        mock_gateway.session_factory = None
        svc = ShareLinkService(gateway=mock_gateway)
        with pytest.raises(ServiceUnavailableError):
            asyncio.run(svc.revoke_share_link(link_id="abc123"))


# =============================================================================
# list_share_links
# =============================================================================


class TestListShareLinks:
    """Tests for the list_share_links method."""

    def test_no_session_factory(self, mock_gateway):
        """Missing session_factory raises ServiceUnavailableError."""
        mock_gateway.session_factory = None
        svc = ShareLinkService(gateway=mock_gateway)
        with pytest.raises(ServiceUnavailableError):
            asyncio.run(svc.list_share_links())


# =============================================================================
# access_share_link
# =============================================================================


class TestAccessShareLink:
    """Tests for the access_share_link method."""

    def test_no_session_factory(self, mock_gateway):
        """Missing session_factory raises ServiceUnavailableError."""
        mock_gateway.session_factory = None
        svc = ShareLinkService(gateway=mock_gateway)
        with pytest.raises(ServiceUnavailableError):
            asyncio.run(svc.access_share_link(link_id="abc123"))


# =============================================================================
# get_share_link_access_logs
# =============================================================================


class TestGetShareLinkAccessLogs:
    """Tests for the get_share_link_access_logs method."""

    def test_no_session_factory(self, mock_gateway):
        """Missing session_factory raises ServiceUnavailableError."""
        mock_gateway.session_factory = None
        svc = ShareLinkService(gateway=mock_gateway)
        with pytest.raises(ServiceUnavailableError):
            asyncio.run(svc.get_share_link_access_logs(link_id="abc123"))
