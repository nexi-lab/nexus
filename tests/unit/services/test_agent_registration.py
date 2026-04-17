"""Unit tests for AgentRegistrationService (Issue #3130).

Uses mocked dependencies to test the orchestration logic, compensation
on failure, 409 conflict detection, and optional feature passthrough.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.grant_helpers import GrantInput
from nexus.services.agents.agent_registration import (
    AgentAlreadyExistsError,
    AgentRegistrationService,
    RegistrationResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_record_store():
    store = MagicMock()
    mock_session = MagicMock()
    store.session_factory.return_value = mock_session
    return store


@pytest.fixture()
def mock_entity_registry():
    registry = MagicMock()
    # Default: get_entity() returns None (agent doesn't exist yet)
    registry.get_entity.return_value = None
    return registry


@pytest.fixture()
def mock_agent_registry():
    pt = MagicMock()
    return pt


@pytest.fixture()
def mock_rebac_manager():
    manager = MagicMock()
    manager.rebac_write_batch.return_value = 2  # tuples created
    manager.rebac_delete_by_subject.return_value = 2
    return manager


@pytest.fixture()
def mock_agent_registry_with_provisioner(mock_agent_registry):
    """AgentRegistry with a mock provisioner wired in via set_provisioner()."""
    mock_agent_registry.provision = AsyncMock(return_value=True)
    return mock_agent_registry


@pytest.fixture()
def service(
    mock_record_store,
    mock_entity_registry,
    mock_agent_registry_with_provisioner,
    mock_rebac_manager,
):
    return AgentRegistrationService(
        record_store=mock_record_store,
        entity_registry=mock_entity_registry,
        agent_registry=mock_agent_registry_with_provisioner,
        rebac_manager=mock_rebac_manager,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Test the standard registration flow."""

    @pytest.mark.asyncio()
    async def test_register_returns_result(self, service):
        """Registration should return a RegistrationResult with all fields."""
        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.return_value = ("key-123", "sk-raw-key")

            result = await service.register(
                agent_id="test-agent",
                name="Test Agent",
                owner_id="admin-user",
                zone_id=ROOT_ZONE_ID,
            )

        assert isinstance(result, RegistrationResult)
        assert result.agent_id == "test-agent"
        assert result.api_key == "sk-raw-key"
        assert result.key_id == "key-123"
        assert result.owner_id == "admin-user"
        assert result.zone_id == ROOT_ZONE_ID
        assert result.ipc_provisioned is True
        assert result.ipc_inbox is not None

    @pytest.mark.asyncio()
    async def test_register_calls_entity_registry(self, service, mock_entity_registry):
        """Registration must call entity_registry.register_entity()."""
        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.return_value = ("key-1", "sk-key")

            await service.register(
                agent_id="my-agent",
                name="My Agent",
                owner_id="alice",
                zone_id=ROOT_ZONE_ID,
            )

        mock_entity_registry.register_entity.assert_called_once_with(
            entity_type="agent",
            entity_id="my-agent",
            parent_type="user",
            parent_id="alice",
            entity_metadata={"name": "My Agent", "zone_id": "root"},
        )

    @pytest.mark.asyncio()
    async def test_register_calls_agent_registry(self, service, mock_agent_registry):
        """Registration must call agent_registry.register_external()."""
        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.return_value = ("key-1", "sk-key")

            await service.register(
                agent_id="proc-agent",
                name="Process Agent",
                owner_id="alice",
                zone_id=ROOT_ZONE_ID,
            )

        mock_agent_registry.register_external.assert_called_once()
        call_args = mock_agent_registry.register_external.call_args
        # name, owner_id, zone_id are positional; connection_id is keyword
        assert call_args[1].get("connection_id") == "proc-agent"

    @pytest.mark.asyncio()
    async def test_register_with_grants(self, service, mock_rebac_manager):
        """Registration with grants should create ReBAC tuples."""
        grants = [
            GrantInput(path="/workspace/main.py", role="editor"),
            GrantInput(path="/docs/readme.md", role="viewer"),
        ]

        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.return_value = ("key-1", "sk-key")

            result = await service.register(
                agent_id="grant-agent",
                name="Grant Agent",
                owner_id="alice",
                zone_id=ROOT_ZONE_ID,
                grants=grants,
            )

        mock_rebac_manager.rebac_write_batch.assert_called_once()
        assert result.grants_created == 2

    @pytest.mark.asyncio()
    async def test_register_creates_permanent_api_key(self, service):
        """API key must be created with expires_at=None (permanent)."""
        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.return_value = ("key-1", "sk-key")

            await service.register(
                agent_id="perm-agent",
                name="Permanent",
                owner_id="alice",
            )

        mock_key.assert_called_once()
        call_kwargs = mock_key.call_args
        assert call_kwargs[1].get("expires_at") is None

    @pytest.mark.asyncio()
    async def test_register_provisions_ipc(self, service, mock_agent_registry_with_provisioner):
        """IPC provisioning should be called when ipc=True."""
        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.return_value = ("key-1", "sk-key")

            result = await service.register(
                agent_id="ipc-agent",
                name="IPC Agent",
                owner_id="alice",
                ipc=True,
            )

        mock_agent_registry_with_provisioner.provision.assert_called_once_with(
            "ipc-agent", name="IPC Agent"
        )
        assert result.ipc_provisioned is True

    @pytest.mark.asyncio()
    async def test_register_skips_ipc_when_false(
        self, service, mock_agent_registry_with_provisioner
    ):
        """IPC provisioning should be skipped when ipc=False."""
        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.return_value = ("key-1", "sk-key")

            result = await service.register(
                agent_id="no-ipc-agent",
                name="No IPC",
                owner_id="alice",
                ipc=False,
            )

        mock_agent_registry_with_provisioner.provision.assert_not_called()
        assert result.ipc_provisioned is False
        assert result.ipc_inbox is None


# ---------------------------------------------------------------------------
# Conflict detection (409)
# ---------------------------------------------------------------------------


class TestConflictDetection:
    """Test 409 Conflict when agent_id already exists."""

    @pytest.mark.asyncio()
    async def test_existing_agent_raises_conflict(self, service, mock_entity_registry):
        """If agent_id already exists in entity_registry, raise AgentAlreadyExistsError."""
        existing_entity = MagicMock()
        mock_entity_registry.get_entity.return_value = existing_entity

        with pytest.raises(AgentAlreadyExistsError, match="existing-agent"):
            await service.register(
                agent_id="existing-agent",
                name="Existing",
                owner_id="alice",
            )


# ---------------------------------------------------------------------------
# Compensation on failure
# ---------------------------------------------------------------------------


class TestCompensation:
    """Test rollback behavior on partial failures."""

    @pytest.mark.asyncio()
    async def test_grant_failure_cleans_up_all(
        self, service, mock_entity_registry, mock_agent_registry, mock_rebac_manager
    ):
        """If ReBAC grant creation fails, entity + process + grants must be cleaned up."""
        mock_rebac_manager.rebac_write_batch.side_effect = RuntimeError("ReBAC write failed")

        grants = [GrantInput(path="/workspace/main.py", role="editor")]

        with pytest.raises(RuntimeError, match="ReBAC write failed"):
            await service.register(
                agent_id="fail-agent",
                name="Fail",
                owner_id="alice",
                grants=grants,
            )

        mock_agent_registry.unregister_external.assert_called_once_with("fail-agent")
        mock_entity_registry.delete_entity.assert_called_once_with("agent", "fail-agent")

    @pytest.mark.asyncio()
    async def test_key_creation_failure_cleans_up(
        self, service, mock_entity_registry, mock_agent_registry
    ):
        """If API key creation fails, entity + process are cleaned up."""
        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.side_effect = RuntimeError("Key creation failed")

            with pytest.raises(RuntimeError, match="Key creation failed"):
                await service.register(
                    agent_id="key-fail-agent",
                    name="Key Fail",
                    owner_id="alice",
                )

        mock_agent_registry.unregister_external.assert_called_once_with("key-fail-agent")
        mock_entity_registry.delete_entity.assert_called_once_with("agent", "key-fail-agent")

    @pytest.mark.asyncio()
    async def test_key_creation_failure_cleans_up_grants(
        self, service, mock_entity_registry, mock_rebac_manager
    ):
        """If key creation fails AFTER grants were written, grants are deleted."""
        mock_rebac_manager.rebac_write_batch.return_value = 2

        grants = [GrantInput(path="/workspace/main.py", role="editor")]

        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.side_effect = RuntimeError("Key creation failed")

            with pytest.raises(RuntimeError, match="Key creation failed"):
                await service.register(
                    agent_id="grant-leak-agent",
                    name="Grant Leak",
                    owner_id="alice",
                    grants=grants,
                )

        mock_rebac_manager.rebac_delete_by_subject.assert_called_once_with(
            subject_type="agent",
            subject_id="grant-leak-agent",
            zone_id=ROOT_ZONE_ID,
        )

    @pytest.mark.asyncio()
    async def test_ipc_failure_does_not_rollback(
        self, service, mock_agent_registry_with_provisioner, mock_entity_registry
    ):
        """IPC provisioning failure should NOT roll back the registration."""
        mock_agent_registry_with_provisioner.provision.return_value = False

        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.return_value = ("key-1", "sk-key")

            result = await service.register(
                agent_id="ipc-fail-agent",
                name="IPC Fail",
                owner_id="alice",
                ipc=True,
            )

        # Entity + process NOT cleaned up (IPC failure is non-fatal)
        mock_entity_registry.delete_entity.assert_not_called()
        assert result.ipc_provisioned is False
        assert result.ipc_inbox is None
        assert result.api_key == "sk-key"


# ---------------------------------------------------------------------------
# Optional public key
# ---------------------------------------------------------------------------


class TestPublicKey:
    """Test optional Ed25519 public key registration."""

    @pytest.mark.asyncio()
    async def test_no_public_key_skips_registration(self, service):
        """When no public_key_hex is provided, key registration is skipped."""
        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.return_value = ("key-1", "sk-key")

            result = await service.register(
                agent_id="no-key-agent",
                name="No Key",
                owner_id="alice",
                public_key_hex=None,
            )

        assert result.public_key_registered is False

    @pytest.mark.asyncio()
    async def test_public_key_failure_is_non_fatal(self, service):
        """Public key registration failure should not fail the registration."""
        service._key_service = MagicMock()

        with (
            patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key,
            patch.object(
                service, "_register_public_key", side_effect=RuntimeError("Key store error")
            ),
        ):
            mock_key.return_value = ("key-1", "sk-key")

            result = await service.register(
                agent_id="key-err-agent",
                name="Key Error",
                owner_id="alice",
                public_key_hex="a" * 64,  # 32 bytes in hex
            )

        assert result.public_key_registered is False
        assert result.api_key == "sk-key"


# ---------------------------------------------------------------------------
# Missing dependencies
# ---------------------------------------------------------------------------


class TestMissingDependencies:
    """Test behavior when optional dependencies are None."""

    @pytest.mark.asyncio()
    async def test_no_rebac_manager_skips_grants(
        self, mock_record_store, mock_entity_registry, mock_agent_registry_with_provisioner
    ):
        """If rebac_manager is None, grants are silently skipped."""
        svc = AgentRegistrationService(
            record_store=mock_record_store,
            entity_registry=mock_entity_registry,
            agent_registry=mock_agent_registry_with_provisioner,
            rebac_manager=None,
        )

        grants = [GrantInput(path="/workspace/main.py", role="editor")]

        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.return_value = ("key-1", "sk-key")

            result = await svc.register(
                agent_id="no-rebac-agent",
                name="No ReBAC",
                owner_id="alice",
                grants=grants,
            )

        assert result.grants_created == 0

    @pytest.mark.asyncio()
    async def test_no_ipc_provisioner_skips_ipc(
        self, mock_record_store, mock_entity_registry, mock_rebac_manager
    ):
        """If agent_registry.provision returns False, IPC is marked not provisioned."""
        agent_reg = MagicMock()
        agent_reg.provision = AsyncMock(return_value=False)
        svc = AgentRegistrationService(
            record_store=mock_record_store,
            entity_registry=mock_entity_registry,
            agent_registry=agent_reg,
            rebac_manager=mock_rebac_manager,
        )

        with patch("nexus.storage.api_key_ops.create_agent_api_key") as mock_key:
            mock_key.return_value = ("key-1", "sk-key")

            result = await svc.register(
                agent_id="no-ipc-prov-agent",
                name="No IPC Prov",
                owner_id="alice",
                ipc=True,
            )

        assert result.ipc_provisioned is False
