"""Unit tests for create_agent_api_key() helper (Issue #3130)."""

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from tests.helpers.in_memory_record_store import InMemoryRecordStore


@pytest.fixture()
def record_store():
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture()
def session(record_store):
    """Session pre-seeded with ROOT_ZONE_ID so create_api_key (#3871 round 5
    zone-existence validation) can mint keys against it."""
    from nexus.storage.models import ZoneModel

    with record_store.session_factory() as seed:
        seed.add(ZoneModel(zone_id=ROOT_ZONE_ID, name="root", phase="Active"))
        seed.commit()
    s = record_store.session_factory()
    yield s
    s.close()


class TestCreateAgentApiKey:
    """Tests for the create_agent_api_key() helper."""

    def test_creates_key_with_agent_subject_type(self, session):
        """Key must have subject_type='agent'."""
        from nexus.storage.api_key_ops import create_agent_api_key
        from nexus.storage.models import APIKeyModel

        key_id, raw_key = create_agent_api_key(
            session,
            agent_id="test-agent-01",
            agent_name="Test Agent",
            owner_id="alice",
            zone_id=ROOT_ZONE_ID,
        )
        session.commit()

        # Verify the key was stored with correct subject_type
        from sqlalchemy import select

        model = session.execute(
            select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        ).scalar_one()

        assert model.subject_type == "agent"
        assert model.subject_id == "test-agent-01"
        assert model.user_id == "alice"
        assert model.zone_id is None  # column no longer written (#3871)
        assert raw_key.startswith("sk-")

    def test_agent_id_passed_as_subject_id(self, session):
        """agent_id must become subject_id on the stored key."""
        from nexus.storage.api_key_ops import create_agent_api_key
        from nexus.storage.models import APIKeyModel

        key_id, _ = create_agent_api_key(
            session,
            agent_id="my-unique-agent",
            agent_name="Agent X",
            owner_id="bob",
            zone_id=ROOT_ZONE_ID,
        )
        session.commit()

        from sqlalchemy import select

        model = session.execute(
            select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        ).scalar_one()

        assert model.subject_id == "my-unique-agent"

    def test_permanent_key_no_expiry(self, session):
        """Top-level agent keys should have no expiry when expires_at=None."""
        from nexus.storage.api_key_ops import create_agent_api_key
        from nexus.storage.models import APIKeyModel

        key_id, _ = create_agent_api_key(
            session,
            agent_id="permanent-agent",
            agent_name="Permanent",
            owner_id="carol",
            zone_id=ROOT_ZONE_ID,
            expires_at=None,
        )
        session.commit()

        from sqlalchemy import select

        model = session.execute(
            select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        ).scalar_one()

        assert model.expires_at is None

    def test_ttl_key_with_expiry(self, session):
        """Delegation keys should respect the provided expires_at."""
        from datetime import UTC, datetime, timedelta

        from nexus.storage.api_key_ops import create_agent_api_key
        from nexus.storage.models import APIKeyModel

        expiry = datetime.now(UTC) + timedelta(hours=24)
        key_id, _ = create_agent_api_key(
            session,
            agent_id="ttl-agent",
            agent_name="TTL Agent",
            owner_id="dave",
            zone_id=ROOT_ZONE_ID,
            expires_at=expiry,
        )
        session.commit()

        from sqlalchemy import select

        model = session.execute(
            select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        ).scalar_one()

        assert model.expires_at is not None

    def test_key_name_includes_agent_prefix(self, session):
        """Key name should be prefixed with 'agent:' for identification."""
        from nexus.storage.api_key_ops import create_agent_api_key
        from nexus.storage.models import APIKeyModel

        key_id, _ = create_agent_api_key(
            session,
            agent_id="named-agent",
            agent_name="My Agent",
            owner_id="eve",
            zone_id=ROOT_ZONE_ID,
        )
        session.commit()

        from sqlalchemy import select

        model = session.execute(
            select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        ).scalar_one()

        assert model.name == "agent:My Agent"
