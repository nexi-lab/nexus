"""CRUD tests for SQLAlchemy auth store implementations.

Issue #2436: Verifies all 6 store implementations against in-memory SQLite.
Tests cover: create, read, update, delete, not-found, and edge cases.
"""

import pytest

from nexus.contracts.auth_store_types import (
    APIKeyDTO,
    OAuthAccountDTO,
    OAuthCredentialDTO,
    SystemSettingDTO,
    UserDTO,
    ZoneDTO,
)
from nexus.storage.auth_stores import (
    MetastoreSettingsStore,
    SQLAlchemyAPIKeyStore,
    SQLAlchemyOAuthAccountStore,
    SQLAlchemyOAuthCredentialStore,
    SQLAlchemySystemSettingsStore,
    SQLAlchemyUserStore,
    SQLAlchemyZoneStore,
)


@pytest.fixture()
def session_factory():
    from tests.helpers.in_memory_record_store import InMemoryRecordStore

    store = InMemoryRecordStore()
    yield store.session_factory
    store.close()


# ===========================================================================
# UserStore
# ===========================================================================


class TestUserStore:
    @pytest.fixture()
    def store(self, session_factory):
        return SQLAlchemyUserStore(session_factory)

    def test_create_and_get_by_id(self, store):
        dto = store.create_user(user_id="u1", email="a@b.com", username="alice")
        assert isinstance(dto, UserDTO)
        assert dto.user_id == "u1"
        assert dto.email == "a@b.com"

        fetched = store.get_by_id("u1")
        assert fetched is not None
        assert fetched.user_id == "u1"

    def test_get_by_email(self, store):
        store.create_user(user_id="u1", email="a@b.com")
        assert store.get_by_email("a@b.com") is not None
        assert store.get_by_email("nobody@b.com") is None

    def test_get_by_username(self, store):
        store.create_user(user_id="u1", username="alice")
        assert store.get_by_username("alice") is not None
        assert store.get_by_username("bob") is None

    def test_update_user(self, store):
        store.create_user(user_id="u1", display_name="Old")
        updated = store.update_user("u1", display_name="New")
        assert updated is not None
        assert updated.display_name == "New"

    def test_update_nonexistent_returns_none(self, store):
        assert store.update_user("nope") is None

    def test_check_email_available(self, store):
        assert store.check_email_available("a@b.com") is True
        store.create_user(user_id="u1", email="a@b.com")
        assert store.check_email_available("a@b.com") is False

    def test_check_username_available(self, store):
        assert store.check_username_available("alice") is True
        store.create_user(user_id="u1", username="alice")
        assert store.check_username_available("alice") is False

    def test_get_by_id_not_found(self, store):
        assert store.get_by_id("nonexistent") is None


# ===========================================================================
# APIKeyStore
# ===========================================================================


class TestAPIKeyStore:
    @pytest.fixture()
    def store(self, session_factory):
        return SQLAlchemyAPIKeyStore(session_factory)

    def test_create_and_get_by_hash(self, store):
        dto = store.create_key(key_hash="hash1", user_id="u1", name="test-key")
        assert isinstance(dto, APIKeyDTO)
        assert dto.key_hash == "hash1"
        assert dto.user_id == "u1"
        assert dto.revoked is False

        fetched = store.get_by_hash("hash1")
        assert fetched is not None
        assert fetched.key_hash == "hash1"

    def test_get_by_hash_not_found(self, store):
        assert store.get_by_hash("nonexistent") is None

    def test_revoke_key(self, store):
        dto = store.create_key(key_hash="h1", user_id="u1", name="k")
        result = store.revoke_key(dto.key_id)
        assert result is True
        # Revoked keys should not be returned by get_by_hash
        assert store.get_by_hash("h1") is None

    def test_revoke_key_with_zone_filter(self, store):
        dto = store.create_key(key_hash="h1", user_id="u1", name="k", zone_id="zone-a")
        # Wrong zone should fail
        assert store.revoke_key(dto.key_id, zone_id="zone-b") is False
        # Correct zone should succeed
        assert store.revoke_key(dto.key_id, zone_id="zone-a") is True

    def test_revoke_nonexistent_returns_false(self, store):
        assert store.revoke_key("nonexistent") is False

    def test_update_last_used_no_error(self, store):
        store.create_key(key_hash="h1", user_id="u1", name="k")
        # Should not raise
        store.update_last_used("h1")
        store.update_last_used("nonexistent")  # non-critical, should not raise


# ===========================================================================
# OAuthCredentialStore
# ===========================================================================


class TestOAuthCredentialStore:
    @pytest.fixture()
    def store(self, session_factory):
        return SQLAlchemyOAuthCredentialStore(session_factory)

    def test_store_and_get_credential(self, store):
        dto = store.store_credential(
            provider="google",
            user_email="a@b.com",
            zone_id="z1",
            encrypted_access_token="enc-at",
            encrypted_refresh_token="enc-rt",
        )
        assert isinstance(dto, OAuthCredentialDTO)
        assert dto.provider == "google"
        assert dto.user_email == "a@b.com"

        fetched = store.get_credential("google", "a@b.com", "z1")
        assert fetched is not None
        assert fetched.credential_id == dto.credential_id

    def test_get_credential_not_found(self, store):
        assert store.get_credential("google", "nobody@b.com", "z1") is None

    def test_store_credential_upsert(self, store):
        dto1 = store.store_credential(
            provider="google",
            user_email="a@b.com",
            zone_id="z1",
            encrypted_access_token="enc-at-1",
        )
        dto2 = store.store_credential(
            provider="google",
            user_email="a@b.com",
            zone_id="z1",
            encrypted_access_token="enc-at-2",
        )
        # Should update existing, same credential_id
        assert dto2.credential_id == dto1.credential_id

    def test_revoke_credential(self, store):
        store.store_credential(
            provider="google",
            user_email="a@b.com",
            zone_id="z1",
            encrypted_access_token="enc-at",
        )
        assert store.revoke_credential("google", "a@b.com", "z1") is True
        # Revoked credential should not be returned
        assert store.get_credential("google", "a@b.com", "z1") is None

    def test_revoke_nonexistent_returns_false(self, store):
        assert store.revoke_credential("google", "nobody@b.com", "z1") is False

    def test_list_credentials(self, store):
        store.store_credential(
            provider="google",
            user_email="a@b.com",
            zone_id="z1",
            encrypted_access_token="enc-at",
        )
        store.store_credential(
            provider="github",
            user_email="a@b.com",
            zone_id="z1",
            encrypted_access_token="enc-at",
        )
        results = store.list_credentials(zone_id="z1")
        assert len(results) == 2

    def test_list_credentials_excludes_revoked(self, store):
        store.store_credential(
            provider="google",
            user_email="a@b.com",
            zone_id="z1",
            encrypted_access_token="enc-at",
        )
        store.revoke_credential("google", "a@b.com", "z1")
        assert len(store.list_credentials(zone_id="z1")) == 0

    def test_update_tokens(self, store):
        dto = store.store_credential(
            provider="google",
            user_email="a@b.com",
            zone_id="z1",
            encrypted_access_token="enc-at-old",
        )
        # Should not raise
        store.update_tokens(
            dto.credential_id,
            encrypted_access_token="enc-at-new",
            encrypted_refresh_token="enc-rt-new",
        )


# ===========================================================================
# OAuthAccountStore
# ===========================================================================


class TestOAuthAccountStore:
    @pytest.fixture()
    def store(self, session_factory):
        return SQLAlchemyOAuthAccountStore(session_factory)

    def test_create_and_get_by_provider(self, store):
        dto = store.create_account(user_id="u1", provider="google", provider_user_id="gid1")
        assert isinstance(dto, OAuthAccountDTO)
        assert dto.user_id == "u1"
        assert dto.provider == "google"

        fetched = store.get_by_provider("google", "gid1")
        assert fetched is not None
        assert fetched.id == dto.id

    def test_get_by_provider_not_found(self, store):
        assert store.get_by_provider("google", "nonexistent") is None

    def test_get_accounts_for_user(self, store):
        store.create_account(user_id="u1", provider="google", provider_user_id="gid1")
        store.create_account(user_id="u1", provider="github", provider_user_id="ghid1")
        accounts = store.get_accounts_for_user("u1")
        assert len(accounts) == 2

    def test_get_accounts_for_user_empty(self, store):
        assert store.get_accounts_for_user("nobody") == []

    def test_delete_account(self, store):
        dto = store.create_account(user_id="u1", provider="google", provider_user_id="gid1")
        assert store.delete_account(dto.id) is True
        assert store.get_by_provider("google", "gid1") is None

    def test_delete_nonexistent_returns_false(self, store):
        assert store.delete_account("nonexistent") is False

    def test_update_last_used(self, store):
        dto = store.create_account(user_id="u1", provider="google", provider_user_id="gid1")
        # Should not raise
        store.update_last_used(dto.id)


# ===========================================================================
# ZoneStore
# ===========================================================================


class TestZoneStore:
    @pytest.fixture()
    def store(self, session_factory):
        return SQLAlchemyZoneStore(session_factory)

    def test_create_and_get_zone(self, store):
        dto = store.create_zone(zone_id="z1", name="Test Zone")
        assert isinstance(dto, ZoneDTO)
        assert dto.zone_id == "z1"
        assert dto.name == "Test Zone"
        assert dto.phase == "Active"

        fetched = store.get_zone("z1")
        assert fetched is not None
        assert fetched.zone_id == "z1"

    def test_get_zone_not_found(self, store):
        assert store.get_zone("nonexistent") is None

    def test_zone_exists(self, store):
        assert store.zone_exists("z1") is False
        store.create_zone(zone_id="z1", name="Test")
        assert store.zone_exists("z1") is True


# ===========================================================================
# SettingsStore
# ===========================================================================


class TestSettingsStore:
    @pytest.fixture()
    def store(self):
        from tests.helpers.dict_metastore import DictMetastore

        return MetastoreSettingsStore(DictMetastore())

    def test_set_and_get_setting(self, store):
        store.set_setting("key1", "value1", description="desc")
        dto = store.get_setting("key1")
        assert isinstance(dto, SystemSettingDTO)
        assert dto.key == "key1"
        assert dto.value == "value1"
        assert dto.description == "desc"

    def test_get_setting_not_found(self, store):
        assert store.get_setting("nonexistent") is None

    def test_set_setting_upsert(self, store):
        store.set_setting("key1", "v1")
        store.set_setting("key1", "v2")
        dto = store.get_setting("key1")
        assert dto is not None
        assert dto.value == "v2"

    def test_set_setting_update_description(self, store):
        store.set_setting("key1", "v1", description="old")
        store.set_setting("key1", "v1", description="new")
        dto = store.get_setting("key1")
        assert dto is not None
        assert dto.description == "new"


# ===========================================================================
# SQLAlchemySystemSettingsStore — SQL-backed, records-tier implementation
# ===========================================================================


class TestSQLAlchemySystemSettingsStore:
    """Behavioral parity with ``TestSettingsStore`` above, but against the
    record_store SQL backend rather than the tier-violating metastore shim."""

    @pytest.fixture()
    def store(self, session_factory):
        return SQLAlchemySystemSettingsStore(session_factory)

    def test_set_and_get_setting(self, store):
        store.set_setting("key1", "value1", description="desc")
        dto = store.get_setting("key1")
        assert isinstance(dto, SystemSettingDTO)
        assert dto.key == "key1"
        assert dto.value == "value1"
        assert dto.description == "desc"

    def test_get_setting_not_found(self, store):
        assert store.get_setting("nonexistent") is None

    def test_set_setting_upsert(self, store):
        store.set_setting("key1", "v1")
        store.set_setting("key1", "v2")
        dto = store.get_setting("key1")
        assert dto is not None
        assert dto.value == "v2"

    def test_set_setting_update_description(self, store):
        store.set_setting("key1", "v1", description="old")
        store.set_setting("key1", "v1", description="new")
        dto = store.get_setting("key1")
        assert dto is not None
        assert dto.description == "new"

    def test_description_not_overwritten_when_omitted(self, store):
        """Passing description=None on update must not nullify the existing one."""
        store.set_setting("key1", "v1", description="keep me")
        store.set_setting("key1", "v2")  # no description kwarg — None default
        dto = store.get_setting("key1")
        assert dto is not None
        assert dto.value == "v2"
        assert dto.description == "keep me"

    def test_multiple_keys_independent(self, store):
        store.set_setting("a", "1")
        store.set_setting("b", "2")
        a = store.get_setting("a")
        b = store.get_setting("b")
        assert a is not None and a.value == "1"
        assert b is not None and b.value == "2"
