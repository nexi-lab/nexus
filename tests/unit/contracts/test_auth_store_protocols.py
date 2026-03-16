"""Protocol conformance and DTO immutability tests for auth store contracts.

Issue #2436: Validates that:
1. All 6 DTOs are frozen (immutable) with correct defaults
2. All 7 Protocols are runtime_checkable
3. SQLAlchemy store implementations satisfy their Protocols
"""

from datetime import datetime

import pytest

from nexus.contracts.auth_store_protocols import (
    APIKeyStoreProtocol,
    OAuthAccountStoreProtocol,
    OAuthCredentialStoreProtocol,
    SessionFactoryProtocol,
    SystemSettingsStoreProtocol,
    UserStoreProtocol,
    ZoneStoreProtocol,
)
from nexus.contracts.auth_store_types import (
    APIKeyDTO,
    OAuthAccountDTO,
    OAuthCredentialDTO,
    SystemSettingDTO,
    UserDTO,
    ZoneDTO,
)

# ===========================================================================
# DTO immutability tests
# ===========================================================================


class TestUserDTO:
    def test_frozen(self):
        dto = UserDTO(user_id="u1")
        with pytest.raises(AttributeError):
            dto.user_id = "u2"  # type: ignore[misc]

    def test_defaults(self):
        dto = UserDTO(user_id="u1")
        assert dto.email is None
        assert dto.username is None
        assert dto.is_active is True
        assert dto.email_verified is False
        assert dto.is_global_admin is False
        assert dto.password_hash is None
        assert dto.primary_auth_method is None

    def test_all_fields(self):
        now = datetime(2025, 1, 1)
        dto = UserDTO(
            user_id="u1",
            email="a@b.com",
            username="alice",
            display_name="Alice",
            is_active=True,
            email_verified=True,
            zone_id="z1",
            avatar_url="https://pic.jpg",
            user_metadata='{"k":"v"}',
            password_hash="hashed",
            primary_auth_method="password",
            is_global_admin=True,
            api_key="sk-123",
            last_login_at=now,
            created_at=now,
            updated_at=now,
        )
        assert dto.user_id == "u1"
        assert dto.email == "a@b.com"
        assert dto.is_global_admin is True


class TestAPIKeyDTO:
    def test_frozen(self):
        dto = APIKeyDTO(key_id="k1", key_hash="h", user_id="u1", name="test")
        with pytest.raises(AttributeError):
            dto.key_id = "k2"  # type: ignore[misc]

    def test_defaults(self):
        dto = APIKeyDTO(key_id="k1", key_hash="h", user_id="u1", name="test")
        assert dto.subject_type == "user"
        assert dto.is_admin is False
        assert dto.revoked is False
        assert dto.inherit_permissions is False
        assert dto.expires_at is None


class TestOAuthCredentialDTO:
    def test_frozen(self):
        dto = OAuthCredentialDTO(
            credential_id="c1", provider="google", user_email="a@b.com", zone_id="z1"
        )
        with pytest.raises(AttributeError):
            dto.credential_id = "c2"  # type: ignore[misc]

    def test_defaults(self):
        dto = OAuthCredentialDTO(
            credential_id="c1", provider="google", user_email="a@b.com", zone_id="z1"
        )
        assert dto.revoked is False
        assert dto.rotation_counter == 0
        assert dto.token_family_id is None


class TestOAuthAccountDTO:
    def test_frozen(self):
        dto = OAuthAccountDTO(id="oa1", user_id="u1", provider="google", provider_user_id="gid")
        with pytest.raises(AttributeError):
            dto.id = "oa2"  # type: ignore[misc]

    def test_defaults(self):
        dto = OAuthAccountDTO(id="oa1", user_id="u1", provider="google", provider_user_id="gid")
        assert dto.provider_email is None
        assert dto.display_name is None
        assert dto.last_used_at is None


class TestZoneDTO:
    def test_frozen(self):
        dto = ZoneDTO(zone_id="z1", name="Test")
        with pytest.raises(AttributeError):
            dto.zone_id = "z2"  # type: ignore[misc]

    def test_defaults(self):
        dto = ZoneDTO(zone_id="z1", name="Test")
        assert dto.phase == "Active"
        assert dto.domain is None
        assert dto.description is None


class TestSystemSettingDTO:
    def test_frozen(self):
        dto = SystemSettingDTO(key="k", value="v")
        with pytest.raises(AttributeError):
            dto.key = "k2"  # type: ignore[misc]

    def test_defaults(self):
        dto = SystemSettingDTO(key="k", value="v")
        assert dto.description is None


# ===========================================================================
# Protocol runtime_checkable conformance tests
# ===========================================================================


class TestProtocolConformance:
    """Verify SQLAlchemy implementations satisfy their Protocols at runtime."""

    @pytest.fixture()
    def session_factory(self):
        from tests.helpers.in_memory_record_store import InMemoryRecordStore

        store = InMemoryRecordStore()
        yield store.session_factory
        store.close()

    def test_user_store_satisfies_protocol(self, session_factory):
        from nexus.storage.auth_stores import SQLAlchemyUserStore

        store = SQLAlchemyUserStore(session_factory)
        assert isinstance(store, UserStoreProtocol)

    def test_api_key_store_satisfies_protocol(self, session_factory):
        from nexus.storage.auth_stores import SQLAlchemyAPIKeyStore

        store = SQLAlchemyAPIKeyStore(session_factory)
        assert isinstance(store, APIKeyStoreProtocol)

    def test_oauth_credential_store_satisfies_protocol(self, session_factory):
        from nexus.storage.auth_stores import SQLAlchemyOAuthCredentialStore

        store = SQLAlchemyOAuthCredentialStore(session_factory)
        assert isinstance(store, OAuthCredentialStoreProtocol)

    def test_oauth_account_store_satisfies_protocol(self, session_factory):
        from nexus.storage.auth_stores import SQLAlchemyOAuthAccountStore

        store = SQLAlchemyOAuthAccountStore(session_factory)
        assert isinstance(store, OAuthAccountStoreProtocol)

    def test_zone_store_satisfies_protocol(self, session_factory):
        from nexus.storage.auth_stores import SQLAlchemyZoneStore

        store = SQLAlchemyZoneStore(session_factory)
        assert isinstance(store, ZoneStoreProtocol)

    def test_settings_store_satisfies_protocol(self):
        from nexus.storage.auth_stores import MetastoreSettingsStore
        from tests.helpers.dict_metastore import DictMetastore

        store = MetastoreSettingsStore(DictMetastore())
        assert isinstance(store, SystemSettingsStoreProtocol)

    def test_session_factory_satisfies_protocol(self, session_factory):
        assert isinstance(session_factory, SessionFactoryProtocol)
