"""Unit tests for AgentNamespaceForkService (Issue #1273).

Tests fork creation, read fall-through, isolation, discard, cleanup, and listing.
"""

import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

pytest.importorskip("pyroaring")


from nexus.bricks.rebac.namespace_manager import MountEntry
from nexus.contracts.exceptions import NamespaceForkNotFoundError
from nexus.contracts.namespace_fork_types import ForkMode
from nexus.system_services.namespace.namespace_fork_service import (
    AgentNamespaceForkService,
)


@pytest.fixture
def mock_namespace_manager() -> MagicMock:
    """Mock NamespaceManager with 3 mount entries."""
    mgr = MagicMock()
    mgr.get_mount_table.return_value = [
        MountEntry(virtual_path="/workspace/alpha"),
        MountEntry(virtual_path="/workspace/beta"),
        MountEntry(virtual_path="/workspace/gamma"),
    ]
    return mgr


@pytest.fixture
def fork_service(mock_namespace_manager: MagicMock) -> AgentNamespaceForkService:
    return AgentNamespaceForkService(
        namespace_manager=mock_namespace_manager,
        ttl_seconds=1800,
    )


# ── TestForkCreation ──────────────────────────────────────────────────


class TestForkCreation:
    def test_fork_returns_valid_uuid(self, fork_service: AgentNamespaceForkService) -> None:
        info = fork_service.fork("agent-1")
        assert len(info.fork_id) == 32  # uuid4 hex
        assert info.agent_id == "agent-1"

    def test_copy_mode_inherits_parent_mounts(
        self, fork_service: AgentNamespaceForkService
    ) -> None:
        info = fork_service.fork("agent-1", mode=ForkMode.COPY)
        assert info.mount_count == 3
        assert info.mode == ForkMode.COPY

    def test_clean_mode_empty(self, fork_service: AgentNamespaceForkService) -> None:
        info = fork_service.fork("agent-1", mode=ForkMode.CLEAN)
        assert info.mount_count == 0
        assert info.mode == ForkMode.CLEAN

    def test_info_fields(self, fork_service: AgentNamespaceForkService) -> None:
        info = fork_service.fork("agent-1", zone_id="zone-x")
        assert info.agent_id == "agent-1"
        assert info.zone_id == "zone-x"
        assert info.parent_fork_id is None
        assert isinstance(info.created_at, datetime)

    def test_fork_of_fork(self, fork_service: AgentNamespaceForkService) -> None:
        parent_info = fork_service.fork("agent-1")
        # Add an entry to parent fork
        parent_ns = fork_service.get_fork(parent_info.fork_id)
        parent_ns.put("/workspace/delta", MountEntry(virtual_path="/workspace/delta"))

        child_info = fork_service.fork("agent-1", parent_fork_id=parent_info.fork_id)
        assert child_info.parent_fork_id == parent_info.fork_id
        assert child_info.mount_count == 4  # 3 inherited + 1 added by parent

    def test_nonexistent_parent_raises(self, fork_service: AgentNamespaceForkService) -> None:
        with pytest.raises(NamespaceForkNotFoundError):
            fork_service.fork("agent-1", parent_fork_id="nonexistent")


# ── TestForkReadFallthrough ───────────────────────────────────────────


class TestForkReadFallthrough:
    def test_overlay_value_returned(self, fork_service: AgentNamespaceForkService) -> None:
        info = fork_service.fork("agent-1")
        ns = fork_service.get_fork(info.fork_id)
        new_entry = MountEntry(virtual_path="/workspace/new")
        ns.put("/workspace/new", new_entry)
        assert ns.get("/workspace/new") == new_entry

    def test_fallthrough_to_parent(self, fork_service: AgentNamespaceForkService) -> None:
        info = fork_service.fork("agent-1", mode=ForkMode.COPY)
        ns = fork_service.get_fork(info.fork_id)
        result = ns.get("/workspace/alpha")
        assert result is not None
        assert result.virtual_path == "/workspace/alpha"

    def test_deleted_key_returns_none(self, fork_service: AgentNamespaceForkService) -> None:
        info = fork_service.fork("agent-1", mode=ForkMode.COPY)
        ns = fork_service.get_fork(info.fork_id)
        ns.delete("/workspace/alpha")
        assert ns.get("/workspace/alpha") is None

    def test_write_does_not_mutate_parent_snapshot(
        self, fork_service: AgentNamespaceForkService
    ) -> None:
        info = fork_service.fork("agent-1", mode=ForkMode.COPY)
        ns = fork_service.get_fork(info.fork_id)
        original_snapshot = ns.get_parent_snapshot()
        ns.put("/workspace/new", MountEntry(virtual_path="/workspace/new"))
        assert ns.get_parent_snapshot() == original_snapshot


# ── TestForkIsolation ─────────────────────────────────────────────────


class TestForkIsolation:
    def test_two_forks_independent(self, fork_service: AgentNamespaceForkService) -> None:
        info1 = fork_service.fork("agent-1")
        info2 = fork_service.fork("agent-2")
        ns1 = fork_service.get_fork(info1.fork_id)
        ns2 = fork_service.get_fork(info2.fork_id)
        ns1.put("/workspace/only-1", MountEntry(virtual_path="/workspace/only-1"))
        assert ns2.get("/workspace/only-1") is None

    def test_write_invisible_to_other(self, fork_service: AgentNamespaceForkService) -> None:
        info1 = fork_service.fork("agent-1")
        info2 = fork_service.fork("agent-1")  # same agent, two forks
        ns1 = fork_service.get_fork(info1.fork_id)
        ns2 = fork_service.get_fork(info2.fork_id)
        ns1.put("/workspace/x", MountEntry(virtual_path="/workspace/x"))
        assert ns2.get("/workspace/x") is None

    def test_concurrent_forks_no_interference(
        self, fork_service: AgentNamespaceForkService
    ) -> None:
        forks = [fork_service.fork(f"agent-{i}") for i in range(10)]
        assert len({f.fork_id for f in forks}) == 10  # all unique


# ── TestForkDiscard ───────────────────────────────────────────────────


class TestForkDiscard:
    def test_discard_removes(self, fork_service: AgentNamespaceForkService) -> None:
        info = fork_service.fork("agent-1")
        fork_service.discard(info.fork_id)
        with pytest.raises(NamespaceForkNotFoundError):
            fork_service.get_fork(info.fork_id)

    def test_discard_nonexistent_raises(self, fork_service: AgentNamespaceForkService) -> None:
        with pytest.raises(NamespaceForkNotFoundError):
            fork_service.discard("no-such-fork")


# ── TestForkCleanup ───────────────────────────────────────────────────


class TestForkCleanup:
    def test_expired_cleaned(self, mock_namespace_manager: MagicMock) -> None:
        svc = AgentNamespaceForkService(
            namespace_manager=mock_namespace_manager,
            ttl_seconds=0,  # everything expires immediately
        )
        svc.fork("agent-1")
        time.sleep(0.01)  # tiny delay to ensure expiry
        count = svc.cleanup_expired()
        assert count == 1
        assert svc.list_forks() == []

    def test_active_not_cleaned(self, fork_service: AgentNamespaceForkService) -> None:
        fork_service.fork("agent-1")
        count = fork_service.cleanup_expired()
        assert count == 0

    def test_cleanup_returns_count(self, mock_namespace_manager: MagicMock) -> None:
        svc = AgentNamespaceForkService(
            namespace_manager=mock_namespace_manager,
            ttl_seconds=0,
        )
        for i in range(5):
            svc.fork(f"agent-{i}")
        time.sleep(0.01)
        assert svc.cleanup_expired() == 5


# ── TestListForks ─────────────────────────────────────────────────────


class TestListForks:
    def test_list_all(self, fork_service: AgentNamespaceForkService) -> None:
        fork_service.fork("agent-1")
        fork_service.fork("agent-2")
        fork_service.fork("agent-3")
        assert len(fork_service.list_forks()) == 3

    def test_list_filtered_by_agent(self, fork_service: AgentNamespaceForkService) -> None:
        fork_service.fork("agent-1")
        fork_service.fork("agent-1")
        fork_service.fork("agent-2")
        assert len(fork_service.list_forks(agent_id="agent-1")) == 2
        assert len(fork_service.list_forks(agent_id="agent-2")) == 1
        assert len(fork_service.list_forks(agent_id="agent-3")) == 0
