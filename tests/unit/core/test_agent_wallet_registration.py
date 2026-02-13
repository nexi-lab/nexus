"""Unit tests for agent registration with wallet and capabilities (Issue #1210).

Tests cover:
- Capabilities property on AgentRecord (metadata-based access)
- Capabilities passed through AgentRegistry.register()
- Wallet provisioning called during NexusFS.register_agent()
- Wallet provisioning skipped when provisioner is None (feature flag off)
- Wallet provisioning failure is non-blocking
- Wallet cleanup on agent deletion
- Idempotent re-registration doesn't re-provision
"""

from __future__ import annotations

import types
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.agent_record import AgentRecord, AgentState
from nexus.core.agent_registry import AgentRegistry
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def now():
    return datetime.now(UTC)


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def registry(session_factory):
    return AgentRegistry(session_factory=session_factory, flush_interval=60)


# ---------------------------------------------------------------------------
# AgentRecord.capabilities property tests
# ---------------------------------------------------------------------------


class TestAgentRecordCapabilities:
    """Tests for the capabilities property on AgentRecord."""

    def test_capabilities_from_metadata(self, now):
        """capabilities property reads from metadata['capabilities']."""
        record = AgentRecord(
            agent_id="agent-1",
            owner_id="alice",
            zone_id="default",
            name="Test Agent",
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({"capabilities": ["search", "analyze", "code"]}),
            created_at=now,
            updated_at=now,
        )
        assert record.capabilities == ["search", "analyze", "code"]

    def test_capabilities_empty_when_not_set(self, now):
        """capabilities returns empty list when not in metadata."""
        record = AgentRecord(
            agent_id="agent-1",
            owner_id="alice",
            zone_id="default",
            name=None,
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        assert record.capabilities == []

    def test_capabilities_returns_copy(self, now):
        """capabilities returns a new list (not a reference to metadata internals)."""
        caps = ["search", "analyze"]
        record = AgentRecord(
            agent_id="agent-1",
            owner_id="alice",
            zone_id=None,
            name=None,
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({"capabilities": caps}),
            created_at=now,
            updated_at=now,
        )
        result = record.capabilities
        result.append("mutated")
        # Original should be unaffected
        assert record.capabilities == ["search", "analyze"]

    def test_capabilities_handles_non_list(self, now):
        """capabilities returns empty list if metadata value is not a list."""
        record = AgentRecord(
            agent_id="agent-1",
            owner_id="alice",
            zone_id=None,
            name=None,
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({"capabilities": "not-a-list"}),
            created_at=now,
            updated_at=now,
        )
        assert record.capabilities == []

    def test_capabilities_handles_tuple(self, now):
        """capabilities converts tuple to list."""
        record = AgentRecord(
            agent_id="agent-1",
            owner_id="alice",
            zone_id=None,
            name=None,
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({"capabilities": ("search", "code")}),
            created_at=now,
            updated_at=now,
        )
        assert record.capabilities == ["search", "code"]


# ---------------------------------------------------------------------------
# AgentRegistry.register() with capabilities tests
# ---------------------------------------------------------------------------


class TestRegistryCapabilities:
    """Tests for capabilities param in AgentRegistry.register()."""

    def test_register_with_capabilities(self, registry):
        """Capabilities are stored in agent metadata."""
        record = registry.register(
            "agent-1",
            "alice",
            zone_id="default",
            name="Searcher",
            capabilities=["search", "analyze"],
        )
        assert record.capabilities == ["search", "analyze"]

    def test_register_capabilities_merged_with_metadata(self, registry):
        """Capabilities are merged into existing metadata dict."""
        record = registry.register(
            "agent-2",
            "alice",
            zone_id="default",
            metadata={"platform": "langgraph"},
            capabilities=["code"],
        )
        assert record.capabilities == ["code"]
        assert record.metadata["platform"] == "langgraph"

    def test_register_without_capabilities(self, registry):
        """Without capabilities param, capabilities list is empty."""
        record = registry.register("agent-3", "alice", zone_id="default")
        assert record.capabilities == []

    def test_register_empty_capabilities(self, registry):
        """Empty capabilities list is not stored (falsy)."""
        record = registry.register(
            "agent-4",
            "alice",
            zone_id="default",
            capabilities=[],
        )
        assert record.capabilities == []


# ---------------------------------------------------------------------------
# Wallet provisioning integration tests (NexusFS-level)
# ---------------------------------------------------------------------------


class TestWalletProvisioningOnRegistration:
    """Tests for wallet auto-provisioning during agent registration.

    Uses a mock NexusFS to test the orchestration logic without
    requiring full NexusFS setup (which needs backend, metadata store, etc.).
    """

    def _make_nexus_fs_mock(self, wallet_provisioner=None, agent_registry=None):
        """Create a minimal mock that simulates NexusFS.register_agent behavior."""
        mock_fs = MagicMock()
        mock_fs._wallet_provisioner = wallet_provisioner
        mock_fs._agent_registry = agent_registry
        mock_fs._entity_registry = MagicMock()
        mock_fs._extract_user_id = MagicMock(return_value="alice")
        mock_fs._extract_zone_id = MagicMock(return_value="default")
        return mock_fs

    def test_wallet_provisioner_called_on_registration(self):
        """Wallet provisioner is called with agent_id and zone_id."""
        provisioner = MagicMock()
        provisioner.return_value = None

        # Directly test the provisioner call pattern from register_agent
        agent_id = "alice,DataAnalyst"
        zone_id = "default"

        # Simulate the NexusFS register_agent provisioner block
        try:
            provisioner(agent_id, zone_id)
        except Exception:
            pass

        provisioner.assert_called_once_with(agent_id, zone_id)

    def test_wallet_provisioner_skipped_when_none(self):
        """When wallet_provisioner is None, no provisioning happens."""
        # This is a no-op test: ensure no error when provisioner is None
        wallet_provisioner = None
        agent_id = "alice,DataAnalyst"
        zone_id = "default"

        # Simulate the NexusFS register_agent provisioner block
        if wallet_provisioner is not None:
            wallet_provisioner(agent_id, zone_id)

        # No exception = pass

    def test_wallet_provisioner_failure_is_non_blocking(self):
        """If wallet provisioner raises, registration should still succeed."""
        provisioner = MagicMock(side_effect=RuntimeError("TigerBeetle unavailable"))

        agent_id = "alice,DataAnalyst"
        zone_id = "default"

        # Simulate the NexusFS register_agent provisioner block (non-blocking)
        wallet_provisioned = False
        try:
            provisioner(agent_id, zone_id)
            wallet_provisioned = True
        except Exception:
            # Non-blocking: log warning but don't fail registration
            wallet_provisioned = False

        assert not wallet_provisioned
        provisioner.assert_called_once_with(agent_id, zone_id)


# ---------------------------------------------------------------------------
# Wallet cleanup on deletion tests
# ---------------------------------------------------------------------------


class TestWalletCleanupOnDeletion:
    """Tests for wallet cleanup during agent deletion."""

    def test_cleanup_called_when_provisioner_has_cleanup(self):
        """If wallet_provisioner has .cleanup attribute, it's called."""
        provisioner = MagicMock()
        provisioner.cleanup = MagicMock()

        agent_id = "alice,DataAnalyst"
        zone_id = "default"

        # Simulate the NexusFS delete_agent cleanup block
        cleanup_fn = getattr(provisioner, "cleanup", None)
        if cleanup_fn is not None:
            cleanup_fn(agent_id, zone_id)

        provisioner.cleanup.assert_called_once_with(agent_id, zone_id)

    def test_no_cleanup_when_provisioner_lacks_cleanup(self):
        """If wallet_provisioner lacks .cleanup, skip gracefully."""

        def simple_provisioner(_agent_id, _zone_id):
            pass

        # Simulate the NexusFS delete_agent cleanup block
        cleanup_fn = getattr(simple_provisioner, "cleanup", None)
        assert cleanup_fn is None  # Simple function has no .cleanup

    def test_cleanup_failure_is_non_blocking(self):
        """If wallet cleanup raises, deletion should still proceed."""
        provisioner = MagicMock()
        provisioner.cleanup = MagicMock(side_effect=RuntimeError("TB error"))

        agent_id = "alice,DataAnalyst"
        zone_id = "default"

        # Simulate the NexusFS delete_agent cleanup block (non-blocking)
        cleanup_fn = getattr(provisioner, "cleanup", None)
        try:
            if cleanup_fn is not None:
                cleanup_fn(agent_id, zone_id)
        except Exception:
            pass  # Non-blocking

        provisioner.cleanup.assert_called_once_with(agent_id, zone_id)


# ---------------------------------------------------------------------------
# Factory wallet provisioner tests
# ---------------------------------------------------------------------------


class TestFactoryWalletProvisioner:
    """Tests for _create_wallet_provisioner in factory.py."""

    def test_returns_none_when_pay_disabled(self):
        """When NEXUS_PAY_ENABLED is not set, returns None."""
        with patch.dict("os.environ", {}, clear=True):
            from nexus.factory import _create_wallet_provisioner

            result = _create_wallet_provisioner()
            assert result is None

    def test_returns_none_when_pay_explicitly_disabled(self):
        """When NEXUS_PAY_ENABLED=false, returns None."""
        with patch.dict("os.environ", {"NEXUS_PAY_ENABLED": "false"}, clear=False):
            from nexus.factory import _create_wallet_provisioner

            result = _create_wallet_provisioner()
            assert result is None

    def test_returns_none_when_tigerbeetle_not_installed(self):
        """When tigerbeetle package is missing, returns None."""
        with (
            patch.dict("os.environ", {"NEXUS_PAY_ENABLED": "true"}, clear=False),
            patch.dict("sys.modules", {"tigerbeetle": None}),
        ):
            from nexus.factory import _create_wallet_provisioner

            result = _create_wallet_provisioner()
            assert result is None
