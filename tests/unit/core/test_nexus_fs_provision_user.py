"""Unit tests for NexusFS.provision_user() (Phase 0.2 — TDD safety net).

Tests cover:
- Input validation (missing user_id, invalid email)
- Happy path with full provisioning
- Partial failure at each step (zone, user, API key, directories, workspace)
- Re-provisioning after soft-delete
- Missing required config (no SessionLocal)
- Zone ID extraction from email
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import make_test_nexus


@pytest.fixture()
def nx_with_db(tmp_path):
    """Create a NexusFS instance with a real SQLite database for provisioning tests."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from nexus.storage.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    nx = make_test_nexus(tmp_path)
    nx.SessionLocal = session_factory

    # Mock entity registry

    mock_registry = MagicMock()
    mock_registry.get_entity.return_value = None

    # Mock API key creator
    mock_key_creator = MagicMock()
    mock_key_creator.create_key.return_value = ("key-123", "nxk-test-api-key")
    # Enlist mock api_key_creator into kernel ServiceRegistry (BrickServices deleted)
    nx.sys_setattr("/__sys__/services/api_key_creator", service=mock_key_creator)

    # Mock ReBAC so we don't need real ReBAC setup
    mock_rebac_manager = MagicMock()

    # Issue #2133: service_wiring.py deleted — explicitly create UserProvisioningService
    # Issue #1801: _system_services deleted — pass mocks directly to service constructor
    from nexus.services.lifecycle.user_provisioning import UserProvisioningService

    nx.sys_setattr(
        "/__sys__/services/user_provisioning",
        service=UserProvisioningService(
            vfs=nx,
            session_factory=session_factory,
            entity_registry=mock_registry,
            api_key_creator=mock_key_creator,
            backend=nx.router.route("/").backend,
            rebac_manager=mock_rebac_manager,
            rmdir_fn=nx.rmdir,
            rebac_create_fn=MagicMock(),
            rebac_delete_fn=MagicMock(),
            register_workspace_fn=MagicMock(),
            register_agent_fn=MagicMock(),
        ),
        allow_overwrite=True,
    )

    return nx


class TestProvisionUserInputValidation:
    """Input validation that should fail fast."""

    @pytest.mark.asyncio
    def test_empty_user_id_raises(self, nx_with_db):
        with pytest.raises(ValueError, match="user_id is required"):
            await nx_with_db.service("user_provisioning").provision_user(
                user_id="", email="test@example.com"
            )

    @pytest.mark.asyncio
    def test_missing_email_raises(self, nx_with_db):
        with pytest.raises(ValueError, match="Valid email required"):
            await nx_with_db.service("user_provisioning").provision_user(user_id="alice", email="")

    @pytest.mark.asyncio
    def test_invalid_email_no_at_sign_raises(self, nx_with_db):
        with pytest.raises(ValueError, match="Valid email required"):
            await nx_with_db.service("user_provisioning").provision_user(
                user_id="alice", email="not-an-email"
            )


class TestProvisionUserZoneIdExtraction:
    """Zone ID extraction from email when not explicitly provided."""

    @pytest.mark.asyncio
    def test_zone_id_extracted_from_email(self, nx_with_db):
        """When zone_id is not provided, extract from email local part."""
        result = await nx_with_db.service("user_provisioning").provision_user(
            user_id="alice",
            email="alice@example.com",
        )
        assert result["zone_id"] == "alice"

    @pytest.mark.asyncio
    def test_explicit_zone_id_takes_precedence(self, nx_with_db):
        result = await nx_with_db.service("user_provisioning").provision_user(
            user_id="alice",
            email="alice@example.com",
            zone_id="custom-zone",
        )
        assert result["zone_id"] == "custom-zone"


class TestProvisionUserHappyPath:
    """Full provisioning should create user, zone, API key, etc."""

    @pytest.mark.asyncio
    def test_returns_expected_keys(self, nx_with_db):
        result = await nx_with_db.service("user_provisioning").provision_user(
            user_id="alice",
            email="alice@example.com",
            zone_id="test-zone",
        )
        assert "user_id" in result
        assert "zone_id" in result
        assert "api_key" in result
        assert "key_id" in result
        assert result["user_id"] == "alice"
        assert result["zone_id"] == "test-zone"

    @pytest.mark.asyncio
    def test_creates_zone_in_database(self, nx_with_db):
        from sqlalchemy import select

        from nexus.storage.models import ZoneModel

        await nx_with_db.service("user_provisioning").provision_user(
            user_id="alice",
            email="alice@example.com",
            zone_id="test-zone",
        )
        session = nx_with_db.SessionLocal()
        try:
            zone = (
                session.execute(select(ZoneModel).filter_by(zone_id="test-zone")).scalars().first()
            )
            assert zone is not None
            assert zone.zone_id == "test-zone"
        finally:
            session.close()

    @pytest.mark.asyncio
    def test_creates_user_in_database(self, nx_with_db):
        from sqlalchemy import select

        from nexus.storage.models import UserModel

        await nx_with_db.service("user_provisioning").provision_user(
            user_id="alice",
            email="alice@example.com",
            zone_id="test-zone",
        )
        session = nx_with_db.SessionLocal()
        try:
            user = session.execute(select(UserModel).filter_by(user_id="alice")).scalars().first()
            assert user is not None
            assert user.email == "alice@example.com"
            assert user.is_active == 1
        finally:
            session.close()

    @pytest.mark.asyncio
    def test_creates_api_key(self, nx_with_db):
        result = await nx_with_db.service("user_provisioning").provision_user(
            user_id="alice",
            email="alice@example.com",
            zone_id="test-zone",
            create_api_key=True,
            create_agents=False,
            import_skills=False,
        )
        assert result["api_key"] is not None
        assert result["key_id"] is not None
        # With agents disabled, only the user's API key is created
        nx_with_db.service("api_key_creator").create_key.assert_called_once()

    @pytest.mark.asyncio
    def test_skip_api_key_creation(self, nx_with_db):
        result = await nx_with_db.service("user_provisioning").provision_user(
            user_id="alice",
            email="alice@example.com",
            zone_id="test-zone",
            create_api_key=False,
            create_agents=False,
            import_skills=False,
        )
        assert result["api_key"] is None
        nx_with_db.service("api_key_creator").create_key.assert_not_called()


class TestProvisionUserIdempotency:
    """Provisioning the same user twice should be idempotent."""

    @pytest.mark.asyncio
    def test_second_provision_does_not_duplicate_zone(self, nx_with_db):
        from sqlalchemy import select

        from nexus.storage.models import ZoneModel

        await nx_with_db.service("user_provisioning").provision_user(
            user_id="alice", email="alice@example.com", zone_id="z1"
        )
        await nx_with_db.service("user_provisioning").provision_user(
            user_id="bob", email="bob@example.com", zone_id="z1"
        )

        session = nx_with_db.SessionLocal()
        try:
            zones = session.execute(select(ZoneModel).filter_by(zone_id="z1")).scalars().all()
            assert len(zones) == 1
        finally:
            session.close()


class TestProvisionUserReactivation:
    """Re-provisioning a soft-deleted user should reactivate them."""

    @pytest.mark.asyncio
    def test_reactivate_soft_deleted_user(self, nx_with_db):
        from sqlalchemy import select

        from nexus.storage.models import UserModel

        # First provision
        await nx_with_db.service("user_provisioning").provision_user(
            user_id="alice", email="alice@example.com", zone_id="z1"
        )

        # Soft-delete
        session = nx_with_db.SessionLocal()
        user = session.execute(select(UserModel).filter_by(user_id="alice")).scalars().first()
        user.is_active = 0
        user.deleted_at = datetime.now(UTC)
        session.commit()
        session.close()

        # Re-provision should reactivate
        await nx_with_db.service("user_provisioning").provision_user(
            user_id="alice", email="alice@example.com", zone_id="z1"
        )

        session = nx_with_db.SessionLocal()
        try:
            user = session.execute(select(UserModel).filter_by(user_id="alice")).scalars().first()
            assert user.is_active == 1
            assert user.deleted_at is None
        finally:
            session.close()


class TestProvisionUserPartialFailure:
    """Partial failures at various steps."""

    @pytest.mark.asyncio
    def test_api_key_creator_not_injected(self, nx_with_db):
        # api_key_creator not enlisted → service() returns None
        # Also update the service (Issue #2033: provision_user delegated to service)
        ups = nx_with_db.service("user_provisioning")
        if ups is not None:
            ups._api_key_creator = None
        with pytest.raises(RuntimeError, match="API key creator not injected"):
            await nx_with_db.service("user_provisioning").provision_user(
                user_id="alice",
                email="alice@example.com",
                zone_id="z1",
                create_api_key=True,
            )

    @pytest.mark.asyncio
    def test_no_session_local_raises(self, tmp_path):
        """Missing SessionLocal should raise TypeError (None is not callable)."""
        from nexus.services.lifecycle.user_provisioning import UserProvisioningService

        nx = make_test_nexus(tmp_path)
        mock_registry = MagicMock()
        mock_registry.get_entity.return_value = None
        # Issue #1801: _system_services deleted — pass mocks directly to service constructor
        # Issue #2133: explicitly create service with session_factory=None
        nx.sys_setattr(
            "/__sys__/services/user_provisioning",
            service=UserProvisioningService(
                vfs=nx,
                session_factory=None,
                entity_registry=mock_registry,
                api_key_creator=None,
                backend=nx.router.route("/").backend,
                rebac_manager=MagicMock(),
                rmdir_fn=nx.rmdir,
                rebac_create_fn=MagicMock(),
                rebac_delete_fn=MagicMock(),
                register_workspace_fn=MagicMock(),
                register_agent_fn=MagicMock(),
            ),
            allow_overwrite=True,
        )
        # Don't set SessionLocal — it defaults to None
        with pytest.raises(TypeError):
            await nx.service("user_provisioning").provision_user(
                user_id="alice", email="alice@example.com"
            )

    @pytest.mark.asyncio
    def test_directory_creation_failure_continues(self, nx_with_db):
        """If directory creation fails, provisioning should continue."""
        # Issue #2033: _create_user_directories is now on UserProvisioningService
        ref = nx_with_db.service("user_provisioning")
        target = ref if ref is not None else nx_with_db
        with patch.object(target, "_create_user_directories", side_effect=OSError("disk full")):
            result = await nx_with_db.service("user_provisioning").provision_user(
                user_id="alice",
                email="alice@example.com",
                zone_id="z1",
                create_agents=False,
                import_skills=False,
            )
            # Should still return a result (provisioning continues past directory failure)
            assert result["user_id"] == "alice"

    @pytest.mark.asyncio
    def test_workspace_creation_failure_continues(self, nx_with_db):
        """If workspace creation fails, provisioning should still return a result.

        Note: workspace_path is assigned before the exists() check, so even if
        the workspace creation fails, the path is still in the result dict.
        The key assertion is that provisioning doesn't abort.
        """
        with patch.object(nx_with_db, "mkdir", side_effect=Exception("workspace error")):
            result = await nx_with_db.service("user_provisioning").provision_user(
                user_id="alice",
                email="alice@example.com",
                zone_id="z1",
                create_agents=False,
                import_skills=False,
            )
            # Provisioning continues past workspace failure
            assert result["user_id"] == "alice"
            assert result["zone_id"] == "z1"
