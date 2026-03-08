"""Integration tests for user secrets lifecycle.

Verifies: set -> encrypt -> retrieve/decrypt -> resolver -> audit log.
"""

import pytest

from nexus.bricks.auth.secrets.crypto import SecretsCrypto
from nexus.bricks.auth.secrets.resolver import SecretResolver
from nexus.bricks.auth.secrets.service import UserSecretsService
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models.auth import UserSecretModel
from nexus.storage.secrets_audit_logger import SecretsAuditLogger


@pytest.fixture()
def record_store():
    from tests.helpers.in_memory_record_store import InMemoryRecordStore

    store = InMemoryRecordStore()
    # Ensure user_secrets table exists
    UserSecretModel.__table__.create(store.engine, checkfirst=True)
    # Ensure secrets_audit_log table exists
    from nexus.storage.models.secrets_audit_log import SecretsAuditLogModel

    SecretsAuditLogModel.__table__.create(store.engine, checkfirst=True)
    yield store
    store.close()


@pytest.fixture()
def crypto():
    return SecretsCrypto(encryption_key=SecretsCrypto.generate_key())


@pytest.fixture()
def audit_logger(record_store):
    return SecretsAuditLogger(record_store=record_store)


@pytest.fixture()
def service(record_store, crypto, audit_logger):
    return UserSecretsService(
        record_store=record_store,
        crypto=crypto,
        audit_logger=audit_logger,
    )


USER_ID = "test-user-1"
ZONE_ID = ROOT_ZONE_ID


class TestSetAndGet:
    def test_set_creates_secret(self, service):
        secret_id = service.set_secret(user_id=USER_ID, name="API_KEY", value="sk-test-123")
        assert secret_id is not None

    def test_get_returns_decrypted_value(self, service):
        service.set_secret(user_id=USER_ID, name="API_KEY", value="sk-test-123")
        value = service.get_secret_value(user_id=USER_ID, name="API_KEY")
        assert value == "sk-test-123"

    def test_get_nonexistent_returns_none(self, service):
        value = service.get_secret_value(user_id=USER_ID, name="MISSING")
        assert value is None

    def test_set_updates_existing(self, service):
        id1 = service.set_secret(user_id=USER_ID, name="KEY", value="v1")
        id2 = service.set_secret(user_id=USER_ID, name="KEY", value="v2")
        # Same record updated
        assert id1 == id2
        assert service.get_secret_value(user_id=USER_ID, name="KEY") == "v2"

    def test_value_is_encrypted_at_rest(self, service, record_store):
        service.set_secret(user_id=USER_ID, name="SECRET", value="plaintext-value")

        from sqlalchemy import select

        with record_store.session_factory() as session:
            row = session.execute(
                select(UserSecretModel).where(UserSecretModel.name == "SECRET")
            ).scalar_one()
            # Encrypted value should NOT equal plaintext
            assert row.encrypted_value != "plaintext-value"
            assert len(row.encrypted_value) > 0


class TestListAndDelete:
    def test_list_returns_metadata_only(self, service):
        service.set_secret(user_id=USER_ID, name="A", value="va")
        service.set_secret(user_id=USER_ID, name="B", value="vb")

        secrets = service.list_secrets(user_id=USER_ID)
        assert len(secrets) == 2
        names = {s["name"] for s in secrets}
        assert names == {"A", "B"}
        # No values in list output
        for s in secrets:
            assert "value" not in s
            assert "encrypted_value" not in s

    def test_list_empty(self, service):
        secrets = service.list_secrets(user_id="nobody")
        assert secrets == []

    def test_delete_existing(self, service):
        service.set_secret(user_id=USER_ID, name="TO_DELETE", value="val")
        assert service.delete_secret(user_id=USER_ID, name="TO_DELETE") is True
        assert service.get_secret_value(user_id=USER_ID, name="TO_DELETE") is None

    def test_delete_nonexistent(self, service):
        assert service.delete_secret(user_id=USER_ID, name="NOPE") is False


class TestZoneIsolation:
    def test_secrets_isolated_by_zone(self, service):
        service.set_secret(user_id=USER_ID, name="KEY", value="zone1-val", zone_id="zone-1")
        service.set_secret(user_id=USER_ID, name="KEY", value="zone2-val", zone_id="zone-2")

        assert (
            service.get_secret_value(user_id=USER_ID, name="KEY", zone_id="zone-1") == "zone1-val"
        )
        assert (
            service.get_secret_value(user_id=USER_ID, name="KEY", zone_id="zone-2") == "zone2-val"
        )

    def test_list_scoped_to_zone(self, service):
        service.set_secret(user_id=USER_ID, name="A", value="v", zone_id="z1")
        service.set_secret(user_id=USER_ID, name="B", value="v", zone_id="z2")

        z1_secrets = service.list_secrets(user_id=USER_ID, zone_id="z1")
        assert len(z1_secrets) == 1
        assert z1_secrets[0]["name"] == "A"


class TestAuditLogging:
    def test_get_secret_emits_audit_event(self, service, audit_logger):
        service.set_secret(user_id=USER_ID, name="AUDITED", value="secret")
        service.get_secret_value(user_id=USER_ID, name="AUDITED")

        events = audit_logger.iter_events(
            filters={"actor_id": USER_ID, "event_type": "key_accessed"},
        )
        assert len(events) >= 1
        event = events[0]
        assert event.actor_id == USER_ID
        assert event.event_type == "key_accessed"

    def test_audit_event_contains_secret_name(self, service, audit_logger):
        service.set_secret(user_id=USER_ID, name="MY_KEY", value="val")
        service.get_secret_value(user_id=USER_ID, name="MY_KEY")

        events = audit_logger.iter_events(filters={"actor_id": USER_ID})
        assert len(events) >= 1
        import json

        details = json.loads(events[0].details)
        assert details["secret_name"] == "MY_KEY"


class TestSecretResolver:
    def test_resolve_simple_string(self, service):
        service.set_secret(user_id=USER_ID, name="TOKEN", value="abc123")

        resolver = SecretResolver(secrets_service=service, user_id=USER_ID)
        result = resolver.resolve_string("Bearer nexus-secret:TOKEN")
        assert result == "Bearer abc123"

    def test_resolve_config_dict(self, service):
        service.set_secret(user_id=USER_ID, name="DB_PASS", value="s3cret")

        resolver = SecretResolver(secrets_service=service, user_id=USER_ID)
        config = {
            "database": {
                "host": "localhost",
                "password": "nexus-secret:DB_PASS",
            },
            "api_key": "nexus-secret:DB_PASS",
        }
        resolved = resolver.resolve_config(config)
        assert resolved["database"]["password"] == "s3cret"
        assert resolved["api_key"] == "s3cret"
        assert resolved["database"]["host"] == "localhost"  # unchanged

    def test_resolve_missing_secret_leaves_pattern(self, service):
        resolver = SecretResolver(secrets_service=service, user_id=USER_ID)
        result = resolver.resolve_string("nexus-secret:MISSING_KEY")
        assert result == "nexus-secret:MISSING_KEY"

    def test_resolve_list_values(self, service):
        service.set_secret(user_id=USER_ID, name="KEY1", value="val1")

        resolver = SecretResolver(secrets_service=service, user_id=USER_ID)
        config = ["nexus-secret:KEY1", "static"]
        resolved = resolver.resolve_config(config)
        assert resolved == ["val1", "static"]

    def test_has_secrets(self, service):
        resolver = SecretResolver(secrets_service=service, user_id=USER_ID)
        assert resolver.has_secrets({"key": "nexus-secret:FOO"}) is True
        assert resolver.has_secrets({"key": "plain-value"}) is False
        assert resolver.has_secrets("nexus-secret:BAR") is True
        assert resolver.has_secrets(42) is False

    def test_resolve_with_zone(self, service):
        service.set_secret(user_id=USER_ID, name="ZONED", value="zone-val", zone_id="z1")

        resolver = SecretResolver(secrets_service=service, user_id=USER_ID, zone_id="z1")
        result = resolver.resolve_string("nexus-secret:ZONED")
        assert result == "zone-val"
