"""Tests for kernel AgentRegistry (Issue #1509).

Pure in-memory — no metastore persistence.
Covers: spawn, kill, signal, wait, external processes, close_all, serialization.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from nexus.contracts.process_types import (
    VALID_AGENT_TRANSITIONS,
    AgentDescriptor,
    AgentError,
    AgentKind,
    AgentNotFoundError,
    AgentSignal,
    AgentState,
    ExternalProcessInfo,
    InvalidTransitionError,
)
from nexus.services.agents.agent_registry import AgentRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ZONE = "test-zone"
OWNER = "user-1"


def _make_table() -> AgentRegistry:
    return AgentRegistry()


# ---------------------------------------------------------------------------
# Spawn / Get / List
# ---------------------------------------------------------------------------


class TestSpawn:
    def test_spawn_creates_process(self) -> None:
        pt = _make_table()
        desc = pt.spawn("agent-1", OWNER, ZONE)
        assert desc.name == "agent-1"
        assert desc.owner_id == OWNER
        assert desc.zone_id == ZONE
        assert desc.state == AgentState.REGISTERED
        assert desc.kind == AgentKind.MANAGED
        assert len(desc.pid) == 12

    def test_spawn_unique_pids(self) -> None:
        pt = _make_table()
        pids = {pt.spawn(f"a{i}", OWNER, ZONE).pid for i in range(10)}
        assert len(pids) == 10

    def test_spawn_with_parent(self) -> None:
        pt = _make_table()
        parent = pt.spawn("parent", OWNER, ZONE)
        child = pt.spawn("child", OWNER, ZONE, parent_pid=parent.pid)
        assert child.ppid == parent.pid
        # Parent's children updated
        updated_parent = pt.get(parent.pid)
        assert updated_parent is not None
        assert child.pid in updated_parent.children

    def test_spawn_invalid_parent_raises(self) -> None:
        pt = _make_table()
        with pytest.raises(AgentNotFoundError):
            pt.spawn("child", OWNER, ZONE, parent_pid="nonexistent")

    def test_spawn_with_labels(self) -> None:
        pt = _make_table()
        desc = pt.spawn("agent", OWNER, ZONE, labels={"model": "claude"})
        assert desc.labels == {"model": "claude"}

    def test_spawn_with_cwd(self) -> None:
        pt = _make_table()
        desc = pt.spawn("agent", OWNER, ZONE, cwd="/workspace")
        assert desc.cwd == "/workspace"

    def test_get_existing(self) -> None:
        pt = _make_table()
        desc = pt.spawn("agent", OWNER, ZONE)
        assert pt.get(desc.pid) == desc

    def test_get_nonexistent(self) -> None:
        pt = _make_table()
        assert pt.get("nonexistent") is None

    def test_list_all(self) -> None:
        pt = _make_table()
        pt.spawn("a1", OWNER, ZONE)
        pt.spawn("a2", OWNER, ZONE)
        assert len(pt.list_processes()) == 2

    def test_list_filter_zone(self) -> None:
        pt = _make_table()
        pt.spawn("a1", OWNER, ZONE)
        pt.spawn("a2", OWNER, "other-zone")
        assert len(pt.list_processes(zone_id=ZONE)) == 1

    def test_list_filter_owner(self) -> None:
        pt = _make_table()
        pt.spawn("a1", OWNER, ZONE)
        pt.spawn("a2", "user-2", ZONE)
        assert len(pt.list_processes(owner_id=OWNER)) == 1

    def test_list_filter_kind(self) -> None:
        pt = _make_table()
        pt.spawn("managed-agent", OWNER, ZONE, kind=AgentKind.MANAGED)
        pt.spawn("unmanaged-agent", OWNER, ZONE, kind=AgentKind.UNMANAGED)
        assert len(pt.list_processes(kind=AgentKind.MANAGED)) == 1

    def test_list_filter_state(self) -> None:
        pt = _make_table()
        pt.spawn("a1", OWNER, ZONE)
        desc = pt.spawn("a2", OWNER, ZONE)
        # Transition a2 to WARMING_UP (valid from REGISTERED)
        pt._transition(desc, AgentState.WARMING_UP)
        assert len(pt.list_processes(state=AgentState.REGISTERED)) == 1
        assert len(pt.list_processes(state=AgentState.WARMING_UP)) == 1


# ---------------------------------------------------------------------------
# State Transitions (spawn creates processes in REGISTERED state)
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_spawn_starts_registered(self) -> None:
        """spawn() creates processes directly in REGISTERED state."""
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        assert desc.state == AgentState.REGISTERED

    def test_registered_to_warming_up(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        updated = pt._transition(desc, AgentState.WARMING_UP)
        assert updated.state == AgentState.WARMING_UP

    def test_warming_up_to_ready(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        desc = pt._transition(desc, AgentState.WARMING_UP)
        updated = pt._transition(desc, AgentState.READY)
        assert updated.state == AgentState.READY

    def test_ready_to_busy(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        desc = pt._transition(desc, AgentState.WARMING_UP)
        desc = pt._transition(desc, AgentState.READY)
        updated = pt._transition(desc, AgentState.BUSY)
        assert updated.state == AgentState.BUSY

    def test_busy_to_ready(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        desc = pt._transition(desc, AgentState.WARMING_UP)
        desc = pt._transition(desc, AgentState.READY)
        desc = pt._transition(desc, AgentState.BUSY)
        updated = pt._transition(desc, AgentState.READY)
        assert updated.state == AgentState.READY

    def test_ready_to_suspended(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        desc = pt._transition(desc, AgentState.WARMING_UP)
        desc = pt._transition(desc, AgentState.READY)
        updated = pt._transition(desc, AgentState.SUSPENDED)
        assert updated.state == AgentState.SUSPENDED

    def test_suspended_to_ready(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        desc = pt._transition(desc, AgentState.WARMING_UP)
        desc = pt._transition(desc, AgentState.READY)
        desc = pt._transition(desc, AgentState.SUSPENDED)
        updated = pt._transition(desc, AgentState.READY)
        assert updated.state == AgentState.READY

    def test_invalid_transition_raises(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        # REGISTERED→BUSY is invalid (must go through WARMING_UP→READY first)
        with pytest.raises(InvalidTransitionError):
            pt._transition(desc, AgentState.BUSY)

    def test_terminated_is_terminal(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        desc = pt._transition(desc, AgentState.TERMINATED)
        with pytest.raises(InvalidTransitionError):
            pt._transition(desc, AgentState.REGISTERED)

    def test_all_valid_transitions(self) -> None:
        """Verify VALID_AGENT_TRANSITIONS is comprehensive."""
        for state in AgentState:
            assert state in VALID_AGENT_TRANSITIONS


# ---------------------------------------------------------------------------
# Signals (spawn creates in REGISTERED — transition to READY before signal tests)
# ---------------------------------------------------------------------------


def _spawn_ready(pt: AgentRegistry, name: str = "a") -> "AgentDescriptor":
    """Spawn a process and advance it to READY state for signal tests."""
    desc = pt.spawn(name, OWNER, ZONE)
    desc = pt._transition(desc, AgentState.WARMING_UP)
    desc = pt._transition(desc, AgentState.READY)
    return desc


class TestSignals:
    def test_sigcont_from_registered_is_invalid(self) -> None:
        pt = _make_table()
        desc = pt.register_external("a", OWNER, ZONE, connection_id="conn-a")
        with pytest.raises(InvalidTransitionError, match="from registered to ready"):
            pt.signal(desc.pid, AgentSignal.SIGCONT)

    def test_sigstop(self) -> None:
        pt = _make_table()
        desc = _spawn_ready(pt)
        updated = pt.signal(desc.pid, AgentSignal.SIGSTOP)
        assert updated.state == AgentState.SUSPENDED

    def test_sigcont(self) -> None:
        pt = _make_table()
        desc = _spawn_ready(pt)
        desc = pt.signal(desc.pid, AgentSignal.SIGSTOP)
        updated = pt.signal(desc.pid, AgentSignal.SIGCONT)
        assert updated.state == AgentState.READY

    def test_sigterm(self) -> None:
        pt = _make_table()
        desc = _spawn_ready(pt)
        updated = pt.signal(desc.pid, AgentSignal.SIGTERM)
        assert updated.state == AgentState.TERMINATED

    def test_sigkill(self) -> None:
        pt = _make_table()
        desc = _spawn_ready(pt)
        pt.signal(desc.pid, AgentSignal.SIGKILL)
        # SIGKILL reaps immediately
        assert pt.get(desc.pid) is None

    def test_sigusr1_merges_payload(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE, labels={"existing": "value"})
        updated = pt.signal(desc.pid, AgentSignal.SIGUSR1, payload={"steering": "pause"})
        assert updated.labels["existing"] == "value"
        assert updated.labels["steering"] == "pause"

    def test_signal_unknown_pid_raises(self) -> None:
        pt = _make_table()
        with pytest.raises(AgentNotFoundError):
            pt.signal("nonexistent", AgentSignal.SIGTERM)


# ---------------------------------------------------------------------------
# Kill / Reap
# ---------------------------------------------------------------------------


class TestKillReap:
    def test_kill_orphan_auto_reaps(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        pt.kill(desc.pid)
        assert pt.get(desc.pid) is None  # reaped

    def test_kill_with_parent_keeps_zombie(self) -> None:
        pt = _make_table()
        parent = pt.spawn("parent", OWNER, ZONE)
        child = pt.spawn("child", OWNER, ZONE, parent_pid=parent.pid)
        pt.kill(child.pid)
        # Not reaped — parent can wait()
        zombie = pt.get(child.pid)
        assert zombie is not None
        assert zombie.state == AgentState.TERMINATED

    def test_kill_nonexistent_raises(self) -> None:
        pt = _make_table()
        with pytest.raises(AgentNotFoundError):
            pt.kill("nonexistent")

    def test_kill_already_zombie(self) -> None:
        pt = _make_table()
        parent = pt.spawn("parent", OWNER, ZONE)
        child = pt.spawn("child", OWNER, ZONE, parent_pid=parent.pid)
        pt.kill(child.pid)
        # Kill again — should be idempotent
        result = pt.kill(child.pid)
        assert result.state == AgentState.TERMINATED

    def test_reap_removes_from_parent_children(self) -> None:
        pt = _make_table()
        parent = pt.spawn("parent", OWNER, ZONE)
        child = pt.spawn("child", OWNER, ZONE, parent_pid=parent.pid)
        # SIGKILL forces immediate reap
        pt.signal(child.pid, AgentSignal.SIGKILL)
        updated_parent = pt.get(parent.pid)
        assert updated_parent is not None
        assert child.pid not in updated_parent.children


# ---------------------------------------------------------------------------
# Wait (async)
# ---------------------------------------------------------------------------


class TestWait:
    @pytest.mark.asyncio
    async def test_wait_already_terminated(self) -> None:
        pt = _make_table()
        parent = pt.spawn("parent", OWNER, ZONE)
        child = pt.spawn("child", OWNER, ZONE, parent_pid=parent.pid)
        pt.kill(child.pid)  # TERMINATED (not reaped — has parent)
        result = await pt.wait(child.pid)
        assert result is not None
        assert result.state == AgentState.TERMINATED

    @pytest.mark.asyncio
    async def test_wait_blocks_until_state_change(self) -> None:
        pt = _make_table()
        parent = pt.spawn("parent", OWNER, ZONE)
        child = pt.spawn("child", OWNER, ZONE, parent_pid=parent.pid)

        async def _killer() -> None:
            await asyncio.sleep(0.05)
            pt.kill(child.pid)

        task = asyncio.create_task(_killer())
        result = await pt.wait(child.pid, timeout=2.0)
        assert result is not None
        assert result.state == AgentState.TERMINATED
        await task

    @pytest.mark.asyncio
    async def test_wait_timeout(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        result = await pt.wait(desc.pid, timeout=0.05)
        assert result is None

    @pytest.mark.asyncio
    async def test_wait_nonexistent_raises(self) -> None:
        pt = _make_table()
        with pytest.raises(AgentNotFoundError):
            await pt.wait("nonexistent")

    @pytest.mark.asyncio
    async def test_wait_custom_target_states(self) -> None:
        pt = _make_table()
        desc = _spawn_ready(pt)

        async def _stopper() -> None:
            await asyncio.sleep(0.05)
            pt.signal(desc.pid, AgentSignal.SIGSTOP)

        task = asyncio.create_task(_stopper())
        result = await pt.wait(
            desc.pid,
            target_states=frozenset({AgentState.SUSPENDED}),
            timeout=2.0,
        )
        assert result is not None
        assert result.state == AgentState.SUSPENDED
        await task

    @pytest.mark.asyncio
    async def test_multiple_waiters(self) -> None:
        pt = _make_table()
        parent = pt.spawn("parent", OWNER, ZONE)
        child = pt.spawn("child", OWNER, ZONE, parent_pid=parent.pid)

        results: list[AgentDescriptor | None] = []

        async def _waiter() -> None:
            r = await pt.wait(child.pid, timeout=2.0)
            results.append(r)

        t1 = asyncio.create_task(_waiter())
        t2 = asyncio.create_task(_waiter())
        await asyncio.sleep(0.05)
        pt.kill(child.pid)
        await asyncio.gather(t1, t2)
        # At least one waiter gets the result (the other may get None
        # if the process was already reaped)
        assert any(r is not None for r in results)


# ---------------------------------------------------------------------------
# External Processes
# ---------------------------------------------------------------------------


class TestExternalProcesses:
    def test_register_external(self) -> None:
        pt = _make_table()
        desc = pt.register_external(
            "claude-code",
            OWNER,
            ZONE,
            connection_id="conn-1",
            host_pid=12345,
            remote_addr="127.0.0.1:50051",
        )
        assert desc.kind == AgentKind.UNMANAGED
        assert desc.external_info is not None
        assert desc.external_info.connection_id == "conn-1"
        assert desc.external_info.host_pid == 12345

    def test_heartbeat(self) -> None:
        pt = _make_table()
        desc = pt.register_external("agent", OWNER, ZONE, connection_id="c1")
        before = desc.external_info.last_heartbeat
        updated = pt.heartbeat(desc.pid)
        assert updated.external_info.last_heartbeat >= before

    def test_heartbeat_on_managed_raises(self) -> None:
        pt = _make_table()
        desc = pt.spawn("managed", OWNER, ZONE)
        with pytest.raises(AgentError, match="heartbeat only for unmanaged"):
            pt.heartbeat(desc.pid)

    def test_unregister_external(self) -> None:
        pt = _make_table()
        desc = pt.register_external("agent", OWNER, ZONE, connection_id="c1")
        pt.unregister_external(desc.pid)
        assert pt.get(desc.pid) is None  # reaped


# ---------------------------------------------------------------------------
# Close All
# ---------------------------------------------------------------------------


class TestCloseAll:
    def test_close_all(self) -> None:
        pt = _make_table()
        pt.spawn("a", OWNER, ZONE)
        pt.spawn("b", OWNER, ZONE)
        pt.close_all()
        assert len(pt.list_processes()) == 0


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_roundtrip(self) -> None:
        now = datetime.now(UTC)
        desc = AgentDescriptor(
            pid="abc123",
            ppid=None,
            name="test",
            owner_id=OWNER,
            zone_id=ZONE,
            kind=AgentKind.MANAGED,
            state=AgentState.BUSY,
            exit_code=None,
            cwd="/workspace",
            children=("child1", "child2"),
            created_at=now,
            updated_at=now,
            labels={"key": "value"},
        )
        json_str = desc.to_json()
        recovered = AgentDescriptor.from_json(json_str)
        assert recovered.pid == desc.pid
        assert recovered.name == desc.name
        assert recovered.state == desc.state
        assert recovered.children == desc.children
        assert recovered.labels == desc.labels

    def test_roundtrip_with_external_info(self) -> None:
        now = datetime.now(UTC)
        desc = AgentDescriptor(
            pid="ext123",
            ppid=None,
            name="external",
            owner_id=OWNER,
            zone_id=ZONE,
            kind=AgentKind.UNMANAGED,
            state=AgentState.REGISTERED,
            created_at=now,
            updated_at=now,
            external_info=ExternalProcessInfo(
                connection_id="conn-1",
                host_pid=9999,
                remote_addr="10.0.0.1:50051",
                protocol="grpc",
                last_heartbeat=now,
            ),
        )
        json_str = desc.to_json()
        recovered = AgentDescriptor.from_json(json_str)
        assert recovered.external_info is not None
        assert recovered.external_info.connection_id == "conn-1"
        assert recovered.external_info.host_pid == 9999
