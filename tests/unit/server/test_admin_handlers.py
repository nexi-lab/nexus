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
    """Create fresh SQLite database, pre-seeded with zone rows."""
    db_path = tmp_path / "test_admin.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    # Seed zones required by junction-backed tests (#3871).
    with engine.begin() as conn:
        from sqlalchemy import text

        for zid in ("zone1", "zone2"):
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO zones (zone_id, name, phase) VALUES (:z, :z, 'Active')"
                ),
                {"z": zid},
            )
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
        """Should filter by zone_id via junction (#3871)."""
        from nexus.storage.api_key_ops import create_api_key

        # Create keys in two zones using junction-backed create_api_key.
        with auth_provider.session_factory() as s:
            create_api_key(s, user_id="u1", name="key-z1", zones=["zone1"])
            create_api_key(s, user_id="u2", name="key-z2", zones=["zone2"])
            s.commit()

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
        """Getting a key with wrong zone should raise NotFound (junction-based, #3871)."""
        from nexus.contracts.exceptions import NexusFileNotFoundError
        from nexus.storage.api_key_ops import create_api_key

        # Create a key in zone1 only (no zone2 junction row).
        with auth_provider.session_factory() as s:
            key_id, _ = create_api_key(s, user_id="u1", name="isolated", zones=["zone1"])
            s.commit()

        # Get with correct zone — should succeed.
        get_params = FakeParams(key_id=key_id, zone_id="zone1")
        result = handle_admin_get_key(auth_provider, get_params, admin_context)
        assert result["key_id"] == key_id

        # Get with wrong zone — should fail (no zone2 junction row).
        get_params_wrong = FakeParams(key_id=key_id, zone_id="zone2")
        with pytest.raises(NexusFileNotFoundError):
            handle_admin_get_key(auth_provider, get_params_wrong, admin_context)


# ── handle_admin_revoke_key Tests (9A RPC handler) ──────────


class TestHandleAdminRevokeKey:
    def test_revoke_with_correct_zone(self, auth_provider, admin_context):
        """Revoking with correct zone should succeed (junction-based, #3871)."""
        from nexus.storage.api_key_ops import create_api_key

        with auth_provider.session_factory() as s:
            key_id, _ = create_api_key(s, user_id="u1", name="revoke-me", zones=["zone1"])
            s.commit()

        revoke_params = FakeParams(key_id=key_id, zone_id="zone1")
        result = handle_admin_revoke_key(auth_provider, revoke_params, admin_context)
        assert result["success"] is True

    def test_revoke_with_wrong_zone_raises(self, auth_provider, admin_context):
        """Revoking with wrong zone should raise NotFound (junction-based, #3871)."""
        from nexus.contracts.exceptions import NexusFileNotFoundError
        from nexus.storage.api_key_ops import create_api_key

        with auth_provider.session_factory() as s:
            key_id, _ = create_api_key(s, user_id="u1", name="zone-isolated", zones=["zone1"])
            s.commit()

        revoke_params = FakeParams(key_id=key_id, zone_id="zone2")
        with pytest.raises(NexusFileNotFoundError):
            handle_admin_revoke_key(auth_provider, revoke_params, admin_context)


# ── handle_admin_update_key Tests ──────────────────────────


class TestHandleAdminUpdateKey:
    def test_self_demotion_prevented(self, auth_provider, admin_context):
        """Cannot remove admin from the last admin key (junction-based, #3871)."""
        from nexus.contracts.exceptions import ValidationError
        from nexus.storage.api_key_ops import create_api_key

        # Create a single admin key via junction-backed helper.
        with auth_provider.session_factory() as s:
            key_id, _ = create_api_key(
                s, user_id="admin_user", name="sole-admin", zones=["zone1"], is_admin=True
            )
            s.commit()

        # Try to remove admin privileges — should fail (last admin in zone1).
        update_params = FakeParams(
            key_id=key_id,
            zone_id=None,
            is_admin=False,
            name=None,
            expires_days=None,
        )
        with pytest.raises(
            ValidationError, match="zones \\['zone1'\\] would have no remaining admin"
        ):
            handle_admin_update_key(auth_provider, update_params, admin_context)

    def test_partial_overlap_demotion_blocked(self, auth_provider, admin_context):
        """Multi-zone admin demotion blocked when ANY zone would lose its sole admin (#3871).

        Target admin covers zone1+zone2; another admin covers only zone1. Demoting
        the target leaves zone2 with zero admins — must raise.
        """
        from nexus.contracts.exceptions import ValidationError
        from nexus.storage.api_key_ops import create_api_key

        with auth_provider.session_factory() as s:
            target_id, _ = create_api_key(
                s, user_id="admin_a", name="multi", zones=["zone1", "zone2"], is_admin=True
            )
            create_api_key(s, user_id="admin_b", name="zone1-only", zones=["zone1"], is_admin=True)
            s.commit()

        update_params = FakeParams(
            key_id=target_id,
            zone_id=None,
            is_admin=False,
            name=None,
            expires_days=None,
        )
        with pytest.raises(
            ValidationError, match=r"zones \['zone2'\] would have no remaining admin"
        ):
            handle_admin_update_key(auth_provider, update_params, admin_context)

    def test_demotion_allowed_when_other_admins_exist(self, auth_provider, admin_context):
        """Can remove admin when other admin keys exist in the same zone (junction, #3871)."""
        from nexus.storage.api_key_ops import create_api_key

        # Create two admin keys in zone1 via junction-backed helper.
        with auth_provider.session_factory() as s:
            key_id_a, _ = create_api_key(
                s, user_id="admin_0", name="admin-0", zones=["zone1"], is_admin=True
            )
            key_id_b, _ = create_api_key(
                s, user_id="admin_1", name="admin-1", zones=["zone1"], is_admin=True
            )
            s.commit()

        # Remove admin from the second key — should succeed (admin_0 still covers zone1).
        update_params = FakeParams(
            key_id=key_id_b,
            zone_id=None,
            is_admin=False,
            name=None,
            expires_days=None,
        )
        result = handle_admin_update_key(auth_provider, update_params, admin_context)
        assert result["success"] is True
        assert result["is_admin"] is False

    def test_update_name(self, auth_provider, admin_context):
        """Updating just the name should work (junction-based, #3871)."""
        from nexus.storage.api_key_ops import create_api_key

        with auth_provider.session_factory() as s:
            key_id, _ = create_api_key(s, user_id="u1", name="old-name", zones=["zone1"])
            s.commit()

        update_params = FakeParams(
            key_id=key_id,
            zone_id="zone1",
            name="new-name",
            is_admin=None,
            expires_days=None,
        )
        result = handle_admin_update_key(auth_provider, update_params, admin_context)
        assert result["name"] == "new-name"

    def test_update_with_wrong_zone_raises(self, auth_provider, admin_context):
        """Updating a key with wrong zone should raise NotFound (junction-based, #3871)."""
        from nexus.contracts.exceptions import NexusFileNotFoundError
        from nexus.storage.api_key_ops import create_api_key

        with auth_provider.session_factory() as s:
            key_id, _ = create_api_key(s, user_id="u1", name="iso", zones=["zone1"])
            s.commit()

        update_params = FakeParams(
            key_id=key_id,
            zone_id="zone2",  # wrong zone
            name="hacked",
            is_admin=None,
            expires_days=None,
        )
        with pytest.raises(NexusFileNotFoundError):
            handle_admin_update_key(auth_provider, update_params, admin_context)
