"""Tests for kernel ProcessTable (Issue #1509).

Pure in-memory — no metastore persistence.
Covers: spawn, kill, signal, wait, external processes, close_all, serialization.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from nexus.contracts.process_types import (
    VALID_PROCESS_TRANSITIONS,
    ExternalProcessInfo,
    InvalidTransitionError,
    ProcessDescriptor,
    ProcessError,
    ProcessKind,
    ProcessNotFoundError,
    ProcessSignal,
    ProcessState,
)
from nexus.core.process_table import ProcessTable

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ZONE = "test-zone"
OWNER = "user-1"


def _make_table() -> ProcessTable:
    return ProcessTable()


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
        assert desc.state == ProcessState.RUNNING
        assert desc.kind == ProcessKind.MANAGED
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
        with pytest.raises(ProcessNotFoundError):
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
        pt.spawn("managed-agent", OWNER, ZONE, kind=ProcessKind.MANAGED)
        pt.spawn("unmanaged-agent", OWNER, ZONE, kind=ProcessKind.UNMANAGED)
        assert len(pt.list_processes(kind=ProcessKind.MANAGED)) == 1

    def test_list_filter_state(self) -> None:
        pt = _make_table()
        pt.spawn("a1", OWNER, ZONE)
        desc = pt.spawn("a2", OWNER, ZONE)
        # Transition a2 to SLEEPING
        pt._transition(desc, ProcessState.SLEEPING)
        assert len(pt.list_processes(state=ProcessState.RUNNING)) == 1
        assert len(pt.list_processes(state=ProcessState.SLEEPING)) == 1


# ---------------------------------------------------------------------------
# State Transitions (spawn creates processes in RUNNING state)
# ---------------------------------------------------------------------------


class TestTransitions:
    def test_spawn_starts_running(self) -> None:
        """spawn() creates processes directly in RUNNING state."""
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        assert desc.state == ProcessState.RUNNING

    def test_running_to_sleeping(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        updated = pt._transition(desc, ProcessState.SLEEPING)
        assert updated.state == ProcessState.SLEEPING

    def test_running_to_stopped(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        updated = pt._transition(desc, ProcessState.STOPPED)
        assert updated.state == ProcessState.STOPPED

    def test_stopped_to_sleeping(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        desc = pt._transition(desc, ProcessState.STOPPED)
        updated = pt._transition(desc, ProcessState.SLEEPING)
        assert updated.state == ProcessState.SLEEPING

    def test_invalid_transition_raises(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        # RUNNING→CREATED is invalid
        with pytest.raises(InvalidTransitionError):
            pt._transition(desc, ProcessState.CREATED)

    def test_zombie_is_terminal(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        desc = pt._transition(desc, ProcessState.ZOMBIE)
        with pytest.raises(InvalidTransitionError):
            pt._transition(desc, ProcessState.RUNNING)

    def test_all_valid_transitions(self) -> None:
        """Verify VALID_PROCESS_TRANSITIONS is comprehensive."""
        for state in ProcessState:
            assert state in VALID_PROCESS_TRANSITIONS


# ---------------------------------------------------------------------------
# Signals (spawn creates in RUNNING — no extra transition needed)
# ---------------------------------------------------------------------------


class TestSignals:
    def test_sigstop(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        updated = pt.signal(desc.pid, ProcessSignal.SIGSTOP)
        assert updated.state == ProcessState.STOPPED

    def test_sigcont(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        desc = pt.signal(desc.pid, ProcessSignal.SIGSTOP)
        updated = pt.signal(desc.pid, ProcessSignal.SIGCONT)
        assert updated.state == ProcessState.SLEEPING

    def test_sigterm(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        updated = pt.signal(desc.pid, ProcessSignal.SIGTERM)
        assert updated.state == ProcessState.ZOMBIE

    def test_sigkill(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)
        pt.signal(desc.pid, ProcessSignal.SIGKILL)
        # SIGKILL reaps immediately
        assert pt.get(desc.pid) is None

    def test_sigusr1_merges_payload(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE, labels={"existing": "value"})
        updated = pt.signal(desc.pid, ProcessSignal.SIGUSR1, payload={"steering": "pause"})
        assert updated.labels["existing"] == "value"
        assert updated.labels["steering"] == "pause"

    def test_signal_unknown_pid_raises(self) -> None:
        pt = _make_table()
        with pytest.raises(ProcessNotFoundError):
            pt.signal("nonexistent", ProcessSignal.SIGTERM)


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
        assert zombie.state == ProcessState.ZOMBIE

    def test_kill_nonexistent_raises(self) -> None:
        pt = _make_table()
        with pytest.raises(ProcessNotFoundError):
            pt.kill("nonexistent")

    def test_kill_already_zombie(self) -> None:
        pt = _make_table()
        parent = pt.spawn("parent", OWNER, ZONE)
        child = pt.spawn("child", OWNER, ZONE, parent_pid=parent.pid)
        pt.kill(child.pid)
        # Kill again — should be idempotent
        result = pt.kill(child.pid)
        assert result.state == ProcessState.ZOMBIE

    def test_reap_removes_from_parent_children(self) -> None:
        pt = _make_table()
        parent = pt.spawn("parent", OWNER, ZONE)
        child = pt.spawn("child", OWNER, ZONE, parent_pid=parent.pid)
        # SIGKILL forces immediate reap
        pt.signal(child.pid, ProcessSignal.SIGKILL)
        updated_parent = pt.get(parent.pid)
        assert updated_parent is not None
        assert child.pid not in updated_parent.children


# ---------------------------------------------------------------------------
# Wait (async)
# ---------------------------------------------------------------------------


class TestWait:
    @pytest.mark.asyncio
    async def test_wait_already_zombie(self) -> None:
        pt = _make_table()
        parent = pt.spawn("parent", OWNER, ZONE)
        child = pt.spawn("child", OWNER, ZONE, parent_pid=parent.pid)
        pt.kill(child.pid)  # ZOMBIE (not reaped — has parent)
        result = await pt.wait(child.pid)
        assert result is not None
        assert result.state == ProcessState.ZOMBIE

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
        assert result.state == ProcessState.ZOMBIE
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
        with pytest.raises(ProcessNotFoundError):
            await pt.wait("nonexistent")

    @pytest.mark.asyncio
    async def test_wait_custom_target_states(self) -> None:
        pt = _make_table()
        desc = pt.spawn("a", OWNER, ZONE)

        async def _stopper() -> None:
            await asyncio.sleep(0.05)
            pt.signal(desc.pid, ProcessSignal.SIGSTOP)

        task = asyncio.create_task(_stopper())
        result = await pt.wait(
            desc.pid,
            target_states=frozenset({ProcessState.STOPPED}),
            timeout=2.0,
        )
        assert result is not None
        assert result.state == ProcessState.STOPPED
        await task

    @pytest.mark.asyncio
    async def test_multiple_waiters(self) -> None:
        pt = _make_table()
        parent = pt.spawn("parent", OWNER, ZONE)
        child = pt.spawn("child", OWNER, ZONE, parent_pid=parent.pid)

        results: list[ProcessDescriptor | None] = []

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
        assert desc.kind == ProcessKind.UNMANAGED
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
        with pytest.raises(ProcessError, match="heartbeat only for unmanaged"):
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
        desc = ProcessDescriptor(
            pid="abc123",
            ppid=None,
            name="test",
            owner_id=OWNER,
            zone_id=ZONE,
            kind=ProcessKind.MANAGED,
            state=ProcessState.RUNNING,
            exit_code=None,
            cwd="/workspace",
            children=("child1", "child2"),
            created_at=now,
            updated_at=now,
            labels={"key": "value"},
        )
        json_str = desc.to_json()
        recovered = ProcessDescriptor.from_json(json_str)
        assert recovered.pid == desc.pid
        assert recovered.name == desc.name
        assert recovered.state == desc.state
        assert recovered.children == desc.children
        assert recovered.labels == desc.labels

    def test_roundtrip_with_external_info(self) -> None:
        now = datetime.now(UTC)
        desc = ProcessDescriptor(
            pid="ext123",
            ppid=None,
            name="external",
            owner_id=OWNER,
            zone_id=ZONE,
            kind=ProcessKind.UNMANAGED,
            state=ProcessState.CREATED,
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
        recovered = ProcessDescriptor.from_json(json_str)
        assert recovered.external_info is not None
        assert recovered.external_info.connection_id == "conn-1"
        assert recovered.external_info.host_pid == 9999
