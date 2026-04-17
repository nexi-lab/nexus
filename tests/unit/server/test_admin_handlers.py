"""Unit tests for admin handler functions in handlers/admin.py.

Covers Issue 10A: Focused unit tests per handler function.
Tests require_admin, require_database_auth, format_api_key_response,
and all 5 handle_admin_* functions.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.rpc.handlers.admin import (
    format_api_key_response,
    handle_admin_create_key,
    handle_admin_get_key,
    handle_admin_list_keys,
    handle_admin_revoke_key,
    handle_admin_update_key,
    require_admin,
    require_database_auth,
)
from nexus.storage.models import Base

# ── Fixtures ──────────────────────────────────────────────


@dataclass
class FakeContext:
    """Minimal context for testing admin checks."""

    is_admin: bool = False
    user: str = "testuser"
    zone_id: str = ROOT_ZONE_ID


@dataclass
class FakeParams:
    """Generic params holder."""

    def __init__(self, **kwargs: Any):
        for k, v in kwargs.items():
            setattr(self, k, v)


@pytest.fixture()
def db_engine(tmp_path):
    """Create fresh SQLite database."""
    db_path = tmp_path / "test_admin.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def session_factory(db_engine):
    """Session factory bound to test database."""
    return sessionmaker(bind=db_engine)


@pytest.fixture()
def auth_provider(session_factory):
    """Mock auth provider with real session_factory."""
    provider = MagicMock()
    provider.session_factory = session_factory
    provider._record_store = SimpleNamespace(session_factory=session_factory)
    return provider


@pytest.fixture()
def admin_context():
    """Admin context for handler calls."""
    return FakeContext(is_admin=True)


@pytest.fixture()
def non_admin_context():
    """Non-admin context for handler calls."""
    return FakeContext(is_admin=False)


# ── require_admin Tests ──────────────────────────────────


class TestRequireAdmin:
    def test_admin_context_passes(self, admin_context):
        """Admin context should not raise."""
        require_admin(admin_context)  # Should not raise

    def test_non_admin_context_raises(self, non_admin_context):
        """Non-admin context should raise NexusPermissionError."""
        from nexus.contracts.exceptions import NexusPermissionError

        with pytest.raises(NexusPermissionError, match="Admin privileges required"):
            require_admin(non_admin_context)

    def test_none_context_raises(self):
        """None context should raise NexusPermissionError."""
        from nexus.contracts.exceptions import NexusPermissionError

        with pytest.raises(NexusPermissionError):
            require_admin(None)


# ── require_database_auth Tests ──────────────────────────


class TestRequireDatabaseAuth:
    def test_valid_auth_provider_passes(self, auth_provider):
        """Auth provider with session_factory should not raise."""
        require_database_auth(auth_provider)  # Should not raise

    def test_none_auth_provider_raises(self):
        """None auth provider should raise ConfigurationError."""
        from nexus.contracts.exceptions import ConfigurationError

        with pytest.raises(ConfigurationError, match="DatabaseAPIKeyAuth"):
            require_database_auth(None)

    def test_auth_provider_without_session_factory_raises(self):
        """Auth provider without session_factory should raise."""
        from nexus.contracts.exceptions import ConfigurationError

        provider = MagicMock(spec=[])  # No attributes
        with pytest.raises(ConfigurationError):
            require_database_auth(provider)


# ── format_api_key_response Tests ──────────────────────────


class TestFormatApiKeyResponse:
    def test_basic_fields(self):
        """Should include core fields without sensitive data."""
        key = MagicMock()
        key.key_id = "kid_1"
        key.user_id = "alice"
        key.subject_type = "user"
        key.subject_id = "alice"
        key.name = "my-key"
        key.zone_id = "zone1"
        key.is_admin = 1
        key.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        key.expires_at = None

        result = format_api_key_response(key)
        assert result["key_id"] == "kid_1"
        assert result["is_admin"] is True
        assert "revoked" not in result
        assert "last_used_at" not in result

    def test_include_sensitive(self):
        """With include_sensitive=True, should include revoked/last_used_at."""
        key = MagicMock()
        key.key_id = "kid_2"
        key.user_id = "bob"
        key.subject_type = "agent"
        key.subject_id = "agent_1"
        key.name = "agent-key"
        key.zone_id = "zone2"
        key.is_admin = 0
        key.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        key.expires_at = datetime(2026, 12, 31, tzinfo=UTC)
        key.revoked = 0
        key.revoked_at = None
        key.last_used_at = datetime(2026, 6, 15, tzinfo=UTC)

        result = format_api_key_response(key, include_sensitive=True)
        assert result["revoked"] is False
        assert result["revoked_at"] is None
        assert "2026-06-15" in result["last_used_at"]


# ── handle_admin_create_key Tests ──────────────────────────


class TestHandleAdminCreateKey:
    def test_creates_key_with_zone(self, auth_provider, admin_context):
        """Should create a key and return key details with raw key."""
        params = FakeParams(
            name="test-key",
            zone_id="zone_alpha",
            user_id="alice",
            is_admin=False,
            expires_days=None,
            subject_type="user",
            subject_id=None,
        )

        result = handle_admin_create_key(auth_provider, params, admin_context)

        assert "key_id" in result
        assert "api_key" in result
        assert result["api_key"].startswith("sk-")
        assert result["zone_id"] == "zone_alpha"

    def test_non_admin_rejected(self, auth_provider, non_admin_context):
        """Non-admin should be rejected."""
        from nexus.contracts.exceptions import NexusPermissionError

        params = FakeParams(
            name="test",
            zone_id="z1",
            user_id="x",
            is_admin=False,
            expires_days=None,
            subject_type="user",
            subject_id=None,
        )

        with pytest.raises(NexusPermissionError):
            handle_admin_create_key(auth_provider, params, non_admin_context)


# ── handle_admin_list_keys Tests ──────────────────────────


class TestHandleAdminListKeys:
    def test_list_keys_with_zone_filter(self, auth_provider, admin_context):
        """Should filter by zone_id."""
        # Create keys in two zones
        params_z1 = FakeParams(
            name="key-z1",
            zone_id="zone1",
            user_id="u1",
            is_admin=False,
            expires_days=None,
            subject_type="user",
            subject_id=None,
        )
        params_z2 = FakeParams(
            name="key-z2",
            zone_id="zone2",
            user_id="u2",
            is_admin=False,
            expires_days=None,
            subject_type="user",
            subject_id=None,
        )
        handle_admin_create_key(auth_provider, params_z1, admin_context)
        handle_admin_create_key(auth_provider, params_z2, admin_context)

        # List only zone1
        list_params = FakeParams(
            user_id=None,
            zone_id="zone1",
            is_admin=None,
            include_revoked=False,
            include_expired=False,
            limit=100,
            offset=0,
        )
        result = handle_admin_list_keys(auth_provider, list_params, admin_context)

        assert result["total"] == 1
        assert result["keys"][0]["zone_id"] == "zone1"


# ── handle_admin_get_key Tests ──────────────────────────


class TestHandleAdminGetKey:
    def test_get_key_with_zone_isolation(self, auth_provider, admin_context):
        """Getting a key with wrong zone should raise NotFound."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        # Create a key in zone1
        create_params = FakeParams(
            name="isolated",
            zone_id="zone1",
            user_id="u1",
            is_admin=False,
            expires_days=None,
            subject_type="user",
            subject_id=None,
        )
        created = handle_admin_create_key(auth_provider, create_params, admin_context)
        key_id = created["key_id"]

        # Get with correct zone — should succeed
        get_params = FakeParams(key_id=key_id, zone_id="zone1")
        result = handle_admin_get_key(auth_provider, get_params, admin_context)
        assert result["key_id"] == key_id

        # Get with wrong zone — should fail
        get_params_wrong = FakeParams(key_id=key_id, zone_id="zone2")
        with pytest.raises(NexusFileNotFoundError):
            handle_admin_get_key(auth_provider, get_params_wrong, admin_context)


# ── handle_admin_revoke_key Tests (9A RPC handler) ──────────


class TestHandleAdminRevokeKey:
    def test_revoke_with_correct_zone(self, auth_provider, admin_context):
        """Revoking with correct zone should succeed."""
        create_params = FakeParams(
            name="revoke-me",
            zone_id="zone1",
            user_id="u1",
            is_admin=False,
            expires_days=None,
            subject_type="user",
            subject_id=None,
        )
        created = handle_admin_create_key(auth_provider, create_params, admin_context)

        revoke_params = FakeParams(key_id=created["key_id"], zone_id="zone1")
        result = handle_admin_revoke_key(auth_provider, revoke_params, admin_context)
        assert result["success"] is True

    def test_revoke_with_wrong_zone_raises(self, auth_provider, admin_context):
        """Revoking with wrong zone should raise NotFound."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        create_params = FakeParams(
            name="zone-isolated",
            zone_id="zone1",
            user_id="u1",
            is_admin=False,
            expires_days=None,
            subject_type="user",
            subject_id=None,
        )
        created = handle_admin_create_key(auth_provider, create_params, admin_context)

        revoke_params = FakeParams(key_id=created["key_id"], zone_id="zone2")
        with pytest.raises(NexusFileNotFoundError):
            handle_admin_revoke_key(auth_provider, revoke_params, admin_context)


# ── handle_admin_update_key Tests ──────────────────────────


class TestHandleAdminUpdateKey:
    def test_self_demotion_prevented(self, auth_provider, admin_context):
        """Cannot remove admin from the last admin key."""
        from nexus.contracts.exceptions import ValidationError

        # Create a single admin key
        create_params = FakeParams(
            name="sole-admin",
            zone_id="zone1",
            user_id="admin_user",
            is_admin=True,
            expires_days=None,
            subject_type="user",
            subject_id=None,
        )
        created = handle_admin_create_key(auth_provider, create_params, admin_context)

        # Try to remove admin privileges — should fail
        update_params = FakeParams(
            key_id=created["key_id"],
            zone_id="zone1",
            is_admin=False,
            name=None,
            expires_days=None,
        )
        with pytest.raises(ValidationError, match="last admin key"):
            handle_admin_update_key(auth_provider, update_params, admin_context)

    def test_demotion_allowed_when_other_admins_exist(self, auth_provider, admin_context):
        """Can remove admin when other admin keys exist in the same zone."""
        # Create two admin keys in zone1
        last_created = None
        for i in range(2):
            create_params = FakeParams(
                name=f"admin-{i}",
                zone_id="zone1",
                user_id=f"admin_{i}",
                is_admin=True,
                expires_days=None,
                subject_type="user",
                subject_id=None,
            )
            last_created = handle_admin_create_key(auth_provider, create_params, admin_context)

        assert last_created is not None
        # Remove admin from the second key — should succeed
        update_params = FakeParams(
            key_id=last_created["key_id"],
            zone_id="zone1",
            is_admin=False,
            name=None,
            expires_days=None,
        )
        result = handle_admin_update_key(auth_provider, update_params, admin_context)
        assert result["success"] is True
        assert result["is_admin"] is False

    def test_update_name(self, auth_provider, admin_context):
        """Updating just the name should work."""
        create_params = FakeParams(
            name="old-name",
            zone_id="zone1",
            user_id="u1",
            is_admin=False,
            expires_days=None,
            subject_type="user",
            subject_id=None,
        )
        created = handle_admin_create_key(auth_provider, create_params, admin_context)

        update_params = FakeParams(
            key_id=created["key_id"],
            zone_id="zone1",
            name="new-name",
            is_admin=None,
            expires_days=None,
        )
        result = handle_admin_update_key(auth_provider, update_params, admin_context)
        assert result["name"] == "new-name"

    def test_update_with_wrong_zone_raises(self, auth_provider, admin_context):
        """Updating a key from the wrong zone should raise NotFound."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        create_params = FakeParams(
            name="zone-locked",
            zone_id="zone1",
            user_id="u1",
            is_admin=False,
            expires_days=None,
            subject_type="user",
            subject_id=None,
        )
        created = handle_admin_create_key(auth_provider, create_params, admin_context)

        update_params = FakeParams(
            key_id=created["key_id"],
            zone_id="zone2",
            name="hacked",
            is_admin=None,
            expires_days=None,
        )
        with pytest.raises(NexusFileNotFoundError):
            handle_admin_update_key(auth_provider, update_params, admin_context)
