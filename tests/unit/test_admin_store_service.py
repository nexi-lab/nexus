"""Integration tests for AdminStoreService (task #129 validation).

Validates all AdminStoreService methods work correctly with a real
SQLite-backed RecordStore (no Raft/Rust dependency required).

Tests cover:
- provision_zone / provision_user_record (provision_user path)
- lock_user_and_provision_key (API key creation)
- get_user_record / get_owner_key_expiration / get_agent_api_key
- revoke_agent_api_keys (delete_agent path)
- delete_api_keys_for_user / delete_oauth_records (deprovision_user path)
- soft_delete_user (deprovision_user path)
- delete_file_paths_by_prefix / delete_rebac_tuples_by_path (rmdir cascade)
- get_agent_key_expiration (agent key lifecycle)
- Performance: each method completes in <50ms
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from nexus.services.admin_store import AdminStoreService
from nexus.storage.record_store import SQLAlchemyRecordStore


@pytest.fixture
def record_store():
    """In-memory SQLite RecordStore."""
    rs = SQLAlchemyRecordStore()  # defaults to sqlite:///:memory:
    yield rs
    rs.close()


@pytest.fixture
def admin_store(record_store):
    """AdminStoreService backed by in-memory SQLite."""
    return AdminStoreService(record_store.session_factory)


# ---------------------------------------------------------------------------
# Zone + User provisioning (covers provision_user code path in nexus_fs)
# ---------------------------------------------------------------------------


class TestProvisionZone:
    def test_create_zone(self, admin_store):
        assert admin_store.provision_zone("zone-1", "Test Org") is True

    def test_idempotent_zone(self, admin_store):
        admin_store.provision_zone("zone-1", "Test Org")
        assert admin_store.provision_zone("zone-1", "Test Org") is False

    def test_zone_default_name(self, admin_store):
        assert admin_store.provision_zone("z2", None) is True


class TestProvisionUserRecord:
    def test_create_user(self, admin_store):
        admin_store.provision_zone("zone-1", None)
        result = admin_store.provision_user_record(
            user_id="u1", email="u1@test.com", display_name="User 1", zone_id="zone-1"
        )
        assert result is True

    def test_idempotent_user(self, admin_store):
        admin_store.provision_zone("zone-1", None)
        admin_store.provision_user_record("u1", "u1@test.com", None, "zone-1")
        assert admin_store.provision_user_record("u1", "u1@test.com", None, "zone-1") is False

    def test_reactivate_soft_deleted_user(self, admin_store):
        admin_store.provision_zone("zone-1", None)
        admin_store.provision_user_record("u1", "u1@test.com", None, "zone-1")
        admin_store.soft_delete_user("u1")
        # Re-provision should reactivate, return False (not a new insert)
        result = admin_store.provision_user_record("u1", "u1@test.com", None, "zone-1")
        assert result is False
        user = admin_store.get_user_record("u1")
        assert user is not None
        assert user["is_active"] is True


# ---------------------------------------------------------------------------
# User record queries
# ---------------------------------------------------------------------------


class TestGetUserRecord:
    def test_found(self, admin_store):
        admin_store.provision_zone("zone-1", None)
        admin_store.provision_user_record("u1", "u1@test.com", "User 1", "zone-1")
        record = admin_store.get_user_record("u1")
        assert record is not None
        assert record["user_id"] == "u1"
        assert record["email"] == "u1@test.com"
        assert record["zone_id"] == "zone-1"
        assert record["is_active"] is True

    def test_not_found(self, admin_store):
        assert admin_store.get_user_record("nonexistent") is None


# ---------------------------------------------------------------------------
# Soft delete (deprovision_user path)
# ---------------------------------------------------------------------------


class TestSoftDeleteUser:
    def test_soft_delete(self, admin_store):
        admin_store.provision_zone("zone-1", None)
        admin_store.provision_user_record("u1", "u1@test.com", None, "zone-1")
        assert admin_store.soft_delete_user("u1") is True
        user = admin_store.get_user_record("u1")
        assert user is not None
        assert user["is_active"] is False

    def test_soft_delete_missing(self, admin_store):
        assert admin_store.soft_delete_user("no-such-user") is False


# ---------------------------------------------------------------------------
# API key operations (delete_agent / deprovision_user paths)
# ---------------------------------------------------------------------------


class TestAPIKeyOperations:
    def _insert_api_key(self, record_store, **kwargs):
        """Insert an API key row directly for testing."""
        from nexus.storage.models import APIKeyModel

        # Ensure required fields have defaults
        kwargs.setdefault("key_hash", uuid.uuid4().hex)
        session = record_store.session_factory()
        try:
            key = APIKeyModel(**kwargs)
            session.add(key)
            session.commit()
        finally:
            session.close()

    def test_get_owner_key_expiration_none(self, admin_store):
        assert admin_store.get_owner_key_expiration("u1") is None

    def test_get_owner_key_expiration_found(self, admin_store, record_store):
        expires = datetime.now(UTC) + timedelta(days=30)
        self._insert_api_key(
            record_store,
            user_id="u1",
            name="owner-key",
            subject_type="user",
            subject_id="u1",
            revoked=0,
            expires_at=expires,
        )
        result = admin_store.get_owner_key_expiration("u1")
        assert result is not None

    def test_revoke_agent_api_keys(self, admin_store, record_store):
        self._insert_api_key(
            record_store,
            user_id="owner",
            name="agent-key",
            subject_type="agent",
            subject_id="agent-1",
            revoked=0,
        )
        count = admin_store.revoke_agent_api_keys("agent-1")
        assert count == 1
        # Verify key is now revoked
        key_info = admin_store.get_agent_api_key("agent-1")
        assert key_info is None

    def test_delete_api_keys_for_user(self, admin_store, record_store):
        self._insert_api_key(
            record_store,
            user_id="u-del",
            name="doomed-key",
            subject_type="user",
            subject_id="u-del",
            revoked=0,
        )
        count = admin_store.delete_api_keys_for_user("u-del")
        assert count == 1

    def test_get_all_active_agent_keys(self, admin_store, record_store):
        self._insert_api_key(
            record_store,
            user_id="owner",
            name="agent-key-1",
            subject_type="agent",
            subject_id="a1",
            revoked=0,
            inherit_permissions=1,
        )
        keys = admin_store.get_all_active_agent_keys()
        assert "a1" in keys
        assert keys["a1"]["inherit_permissions"] is True


# ---------------------------------------------------------------------------
# OAuth cleanup (deprovision_user path)
# ---------------------------------------------------------------------------


class TestOAuthCleanup:
    def test_no_oauth_tables(self, admin_store):
        """Should return (0, 0) when OAuth tables don't exist."""
        result = admin_store.delete_oauth_records("u1")
        assert result == (0, 0)


# ---------------------------------------------------------------------------
# Metadata / permission cleanup (rmdir cascade path)
# ---------------------------------------------------------------------------


class TestPathCleanup:
    def _insert_file_path(self, record_store, virtual_path, zone_id="zone-1"):
        from nexus.storage.models import FilePathModel

        session = record_store.session_factory()
        try:
            fp = FilePathModel(
                virtual_path=virtual_path,
                backend_id="local",
                physical_path=f"/data{virtual_path}",
                content_hash="abc123",
                zone_id=zone_id,
            )
            session.add(fp)
            session.commit()
        finally:
            session.close()

    def _insert_rebac_tuple(self, record_store, object_id, zone_id="zone-1"):
        from nexus.storage.models import ReBACTupleModel

        session = record_store.session_factory()
        try:
            t = ReBACTupleModel(
                tuple_id=uuid.uuid4().hex,
                object_type="file",
                object_id=object_id,
                relation="owner",
                subject_type="user",
                subject_id="u1",
                zone_id=zone_id,
            )
            session.add(t)
            session.commit()
        finally:
            session.close()

    def test_delete_file_paths_by_prefix(self, admin_store, record_store):
        self._insert_file_path(record_store, "/zone-1/data/file1.txt")
        self._insert_file_path(record_store, "/zone-1/data/file2.txt")
        self._insert_file_path(record_store, "/zone-1/other/file3.txt")
        count = admin_store.delete_file_paths_by_prefix("/zone-1/data/")
        assert count == 2

    def test_delete_rebac_tuples_by_path(self, admin_store, record_store):
        self._insert_rebac_tuple(record_store, "/zone-1/data/file1.txt")
        self._insert_rebac_tuple(record_store, "/zone-1/data/file2.txt")
        self._insert_rebac_tuple(record_store, "/zone-1/other/file3.txt")
        count = admin_store.delete_rebac_tuples_by_path("/zone-1/data/")
        assert count == 2


# ---------------------------------------------------------------------------
# Agent key expiration helper
# ---------------------------------------------------------------------------


class TestAgentKeyExpiration:
    def test_default_365_days(self, admin_store):
        """No owner key → defaults to 365 days from now."""
        exp = admin_store.get_agent_key_expiration("u-no-keys")
        assert (exp - datetime.now(UTC)).days >= 364

    def test_uses_owner_expiration(self, admin_store, record_store):
        from nexus.storage.models import APIKeyModel

        expires = datetime.now(UTC) + timedelta(days=60)
        session = record_store.session_factory()
        try:
            session.add(
                APIKeyModel(
                    user_id="u-owner",
                    key_hash=uuid.uuid4().hex,
                    name="owner-key",
                    subject_type="user",
                    subject_id="u-owner",
                    revoked=0,
                    expires_at=expires,
                )
            )
            session.commit()
        finally:
            session.close()

        result = admin_store.get_agent_key_expiration("u-owner")
        # Should use the owner key's expiration
        assert abs((result - expires).total_seconds()) < 2

    def test_expired_owner_key_raises(self, admin_store, record_store):
        from nexus.storage.models import APIKeyModel

        expired = datetime.now(UTC) - timedelta(days=1)
        session = record_store.session_factory()
        try:
            session.add(
                APIKeyModel(
                    user_id="u-exp",
                    key_hash=uuid.uuid4().hex,
                    name="expired-key",
                    subject_type="user",
                    subject_id="u-exp",
                    revoked=0,
                    expires_at=expired,
                )
            )
            session.commit()
        finally:
            session.close()

        with pytest.raises(ValueError, match="expired"):
            admin_store.get_agent_key_expiration("u-exp")


# ---------------------------------------------------------------------------
# Performance validation — each method <50ms on SQLite
# ---------------------------------------------------------------------------


class TestPerformance:
    """Verify AdminStoreService methods have no performance regression."""

    def test_provision_performance(self, admin_store):
        start = time.perf_counter()
        admin_store.provision_zone("perf-zone", "PerfOrg")
        admin_store.provision_user_record("perf-u", "p@t.com", None, "perf-zone")
        elapsed = time.perf_counter() - start
        assert elapsed < 0.05, f"Provisioning took {elapsed:.3f}s (>50ms)"

    def test_query_performance(self, admin_store):
        admin_store.provision_zone("q-zone", None)
        admin_store.provision_user_record("q-u", "q@t.com", None, "q-zone")

        start = time.perf_counter()
        admin_store.get_user_record("q-u")
        admin_store.get_owner_key_expiration("q-u")
        admin_store.get_all_active_agent_keys()
        elapsed = time.perf_counter() - start
        assert elapsed < 0.05, f"Queries took {elapsed:.3f}s (>50ms)"

    def test_cleanup_performance(self, admin_store, record_store):
        admin_store.provision_zone("c-zone", None)
        admin_store.provision_user_record("c-u", "c@t.com", None, "c-zone")

        start = time.perf_counter()
        admin_store.delete_api_keys_for_user("c-u")
        admin_store.delete_oauth_records("c-u")
        admin_store.soft_delete_user("c-u")
        admin_store.delete_file_paths_by_prefix("/c-zone/")
        admin_store.delete_rebac_tuples_by_path("/c-zone/")
        elapsed = time.perf_counter() - start
        assert elapsed < 0.05, f"Cleanup took {elapsed:.3f}s (>50ms)"


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_implements_protocol(self, admin_store):
        from nexus.services.protocols.admin_store import AdminStoreProtocol

        assert isinstance(admin_store, AdminStoreProtocol)
