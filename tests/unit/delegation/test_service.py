"""Mocked unit tests for DelegationService (Issue #1271).

Tests service logic with mocked dependencies (ReBAC, entity registry,
namespace manager, database). Validates happy paths, error conditions,
and anti-escalation enforcement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from nexus.services.delegation.errors import (
    DelegationChainError,
    DelegationError,
)
from nexus.services.delegation.models import DelegationMode, DelegationResult
from nexus.services.delegation.service import MAX_TTL_SECONDS, DelegationService


@pytest.fixture()
def mock_session_factory():
    """Create a mock session factory that returns a mock session."""
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None
    session.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
    factory = MagicMock(return_value=session)
    return factory


@pytest.fixture()
def mock_rebac_manager():
    """Create a mock ReBAC manager."""
    manager = MagicMock()
    # Default: parent has some grants
    manager.rebac_list_objects.return_value = [
        ("file", "/workspace/a.txt"),
        ("file", "/workspace/b.txt"),
        ("file", "/workspace/c.txt"),
    ]
    manager.rebac_write_batch.return_value = 3
    return manager


@pytest.fixture()
def mock_namespace_manager():
    """Create a mock namespace manager."""
    manager = MagicMock()
    entry = MagicMock()
    entry.virtual_path = "/workspace"
    manager.get_mount_table.return_value = [entry]
    return manager


@pytest.fixture()
def mock_entity_registry():
    """Create a mock entity registry."""
    registry = MagicMock()
    entity = MagicMock()
    entity.parent_type = "user"
    entity.parent_id = "alice"
    registry.get_entity.return_value = entity
    return registry


@pytest.fixture()
def mock_agent_registry():
    """Create a mock agent registry."""
    registry = MagicMock()
    record = MagicMock()
    record.agent_id = "worker_1"
    record.owner_id = "alice"
    registry.register.return_value = record
    registry.unregister.return_value = True
    return registry


@pytest.fixture()
def service(
    mock_session_factory,
    mock_rebac_manager,
    mock_namespace_manager,
    mock_entity_registry,
    mock_agent_registry,
):
    """Create a DelegationService with all mocked dependencies."""
    return DelegationService(
        session_factory=mock_session_factory,
        rebac_manager=mock_rebac_manager,
        namespace_manager=mock_namespace_manager,
        entity_registry=mock_entity_registry,
        agent_registry=mock_agent_registry,
    )


class TestDelegateCopyMode:
    @patch("nexus.server.auth.database_key.DatabaseAPIKeyAuth.create_key")
    def test_happy_path(
        self,
        mock_create_key,
        service,
        mock_agent_registry,
        mock_rebac_manager,
    ):
        """Copy mode delegation creates worker with parent's grants."""
        mock_create_key.return_value = ("key_id_1", "sk-test-key")

        result = service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_1",
            worker_name="Worker Agent",
            delegation_mode=DelegationMode.COPY,
            zone_id="default",
            ttl_seconds=3600,
        )

        assert isinstance(result, DelegationResult)
        assert result.worker_agent_id == "worker_1"
        assert result.api_key == "sk-test-key"
        assert result.delegation_mode == DelegationMode.COPY
        assert result.expires_at is not None
        assert result.mount_table == ["/workspace"]

        # Verify agent_registry.register was called
        mock_agent_registry.register.assert_called_once()

        # Verify ReBAC tuples were created
        mock_rebac_manager.rebac_write_batch.assert_called_once()


class TestDelegateCleanMode:
    @patch("nexus.server.auth.database_key.DatabaseAPIKeyAuth.create_key")
    def test_happy_path(
        self,
        mock_create_key,
        service,
    ):
        """Clean mode with valid add_grants creates worker."""
        mock_create_key.return_value = ("key_id_1", "sk-test-key")

        result = service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_1",
            worker_name="Worker Agent",
            delegation_mode=DelegationMode.CLEAN,
            zone_id="default",
            add_grants=["/workspace/a.txt"],
        )

        assert isinstance(result, DelegationResult)
        assert result.delegation_mode == DelegationMode.CLEAN


class TestDelegateSharedMode:
    @patch("nexus.server.auth.database_key.DatabaseAPIKeyAuth.create_key")
    def test_happy_path(
        self,
        mock_create_key,
        service,
    ):
        """Shared mode creates worker with all parent grants."""
        mock_create_key.return_value = ("key_id_1", "sk-test-key")

        result = service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_1",
            worker_name="Worker Agent",
            delegation_mode=DelegationMode.SHARED,
        )

        assert isinstance(result, DelegationResult)
        assert result.delegation_mode == DelegationMode.SHARED


class TestAntiEscalation:
    def test_clean_mode_escalation_rejected(
        self,
        service,
    ):
        """Clean mode rejects grants not held by parent."""
        from nexus.services.delegation.errors import EscalationError

        with pytest.raises(EscalationError):
            service.delegate(
                coordinator_agent_id="coordinator_1",
                coordinator_owner_id="alice",
                worker_id="worker_1",
                worker_name="Worker Agent",
                delegation_mode=DelegationMode.CLEAN,
                add_grants=["/not/in/parent.txt"],
            )


class TestTTLEnforcement:
    def test_ttl_too_large(self, service):
        """TTL exceeding MAX_TTL_SECONDS is rejected."""
        with pytest.raises(DelegationError, match="exceeds maximum"):
            service.delegate(
                coordinator_agent_id="coordinator_1",
                coordinator_owner_id="alice",
                worker_id="worker_1",
                worker_name="Worker Agent",
                delegation_mode=DelegationMode.COPY,
                ttl_seconds=MAX_TTL_SECONDS + 1,
            )

    def test_ttl_zero(self, service):
        """TTL of zero is rejected."""
        with pytest.raises(DelegationError, match="positive"):
            service.delegate(
                coordinator_agent_id="coordinator_1",
                coordinator_owner_id="alice",
                worker_id="worker_1",
                worker_name="Worker Agent",
                delegation_mode=DelegationMode.COPY,
                ttl_seconds=0,
            )

    def test_ttl_negative(self, service):
        """Negative TTL is rejected."""
        with pytest.raises(DelegationError, match="positive"):
            service.delegate(
                coordinator_agent_id="coordinator_1",
                coordinator_owner_id="alice",
                worker_id="worker_1",
                worker_name="Worker Agent",
                delegation_mode=DelegationMode.COPY,
                ttl_seconds=-1,
            )


class TestDelegationChain:
    def test_delegated_agent_cannot_delegate(
        self,
        mock_session_factory,
        mock_rebac_manager,
        mock_entity_registry,
    ):
        """A delegated agent cannot create further delegations."""
        # Set up: get_delegation returns a record for the coordinator
        session = mock_session_factory()
        existing_delegation = MagicMock()
        existing_delegation.delegation_id = "existing_1"
        existing_delegation.agent_id = "coordinator_1"
        existing_delegation.parent_agent_id = "grandparent_1"
        existing_delegation.delegation_mode = "copy"
        existing_delegation.scope_prefix = None
        existing_delegation.lease_expires_at = None
        existing_delegation.removed_grants = "[]"
        existing_delegation.added_grants = "[]"
        existing_delegation.readonly_paths = "[]"
        existing_delegation.zone_id = "default"
        existing_delegation.created_at = datetime.now(UTC)

        session.query.return_value.filter.return_value.first.return_value = existing_delegation

        mock_agent_registry = MagicMock()

        service = DelegationService(
            session_factory=mock_session_factory,
            rebac_manager=mock_rebac_manager,
            entity_registry=mock_entity_registry,
            agent_registry=mock_agent_registry,
        )

        with pytest.raises(DelegationChainError, match="cannot delegate"):
            service.delegate(
                coordinator_agent_id="coordinator_1",
                coordinator_owner_id="alice",
                worker_id="worker_1",
                worker_name="Worker Agent",
                delegation_mode=DelegationMode.COPY,
            )


class TestRevokeDelegation:
    def test_revoke_existing(
        self,
        service,
        mock_agent_registry,
        mock_session_factory,
    ):
        """Revoking an existing delegation succeeds."""
        # Set up: load_delegation_record returns a record
        session = mock_session_factory()
        record = MagicMock()
        record.delegation_id = "del_1"
        record.agent_id = "worker_1"
        record.parent_agent_id = "coordinator_1"
        record.delegation_mode = "copy"
        record.scope_prefix = None
        record.lease_expires_at = None
        record.removed_grants = "[]"
        record.added_grants = "[]"
        record.readonly_paths = "[]"
        record.zone_id = "default"
        record.created_at = datetime.now(UTC)

        session.query.return_value.filter.return_value.first.return_value = record

        # Mock the tuple deletion to avoid direct DB access
        service._delete_worker_tuples = MagicMock()
        service._revoke_worker_api_key = MagicMock()

        result = service.revoke_delegation("del_1")
        assert result is True

        # Verify agent_registry.unregister was called
        mock_agent_registry.unregister.assert_called_once_with(record.agent_id)

    def test_revoke_nonexistent(self, service, mock_session_factory):
        """Revoking a non-existent delegation raises error."""
        from nexus.services.delegation.errors import DelegationNotFoundError

        session = mock_session_factory()
        session.query.return_value.filter.return_value.first.return_value = None

        with pytest.raises(DelegationNotFoundError):
            service.revoke_delegation("nonexistent_id")


class TestListDelegations:
    def test_list_empty(self, service):
        """Listing delegations for agent with none returns empty."""
        result = service.list_delegations("coordinator_1")
        assert result == []
