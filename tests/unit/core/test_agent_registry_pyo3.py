"""PyO3-boundary smoke tests for `nexus_runtime.AgentRegistry`.

The kernel-side state-machine semantics (transitions, signals, signal
SIGCONT generation bump, list_by_priority ordering, condvar waits, etc.)
are covered by Rust unit tests in `rust/kernel/src/core/agents/registry.rs`.
These Python tests verify the PyO3 wrapper exposes them correctly:
constructor, descriptor field access, dict round-trips, and the
provisioner late-bind hook.
"""

from __future__ import annotations

import asyncio

import pytest
from nexus_runtime import AgentRegistry

from nexus.contracts.process_types import AgentKind, AgentSignal, AgentState

ZONE = "test-zone"
OWNER = "user-1"


# ---------------------------------------------------------------------------
# Constructor + descriptor surface
# ---------------------------------------------------------------------------


def test_spawn_returns_descriptor_with_attribute_access() -> None:
    registry = AgentRegistry()
    desc = registry.spawn("agent-1", OWNER, ZONE)
    assert desc.pid
    assert desc.name == "agent-1"
    assert desc.owner_id == OWNER
    assert desc.zone_id == ZONE
    assert desc.kind == AgentKind.MANAGED
    assert desc.state == AgentState.REGISTERED
    assert desc.generation == 1
    assert desc.children == []


def test_get_returns_none_for_unknown_pid() -> None:
    registry = AgentRegistry()
    assert registry.get("ghost") is None


def test_descriptor_to_dict_roundtrip() -> None:
    registry = AgentRegistry()
    desc = registry.spawn("agent-1", OWNER, ZONE)
    d = desc.to_dict()
    assert d["pid"] == desc.pid
    assert d["kind"] == AgentKind.MANAGED
    assert d["state"] == AgentState.REGISTERED
    assert d["external_info"] is None


# ---------------------------------------------------------------------------
# Lifecycle: state transitions + signals via PyO3
# ---------------------------------------------------------------------------


def test_update_state_advances_through_lifecycle() -> None:
    registry = AgentRegistry()
    desc = registry.spawn("agent-1", OWNER, ZONE)
    registry.update_state(desc.pid, AgentState.WARMING_UP.value)
    registry.update_state(desc.pid, AgentState.READY.value)
    assert registry.get(desc.pid).state == AgentState.READY


def test_update_state_rejects_invalid_transition() -> None:
    registry = AgentRegistry()
    desc = registry.spawn("agent-1", OWNER, ZONE)
    # REGISTERED -> READY is invalid (must go through WARMING_UP).
    with pytest.raises(ValueError):
        registry.update_state(desc.pid, AgentState.READY.value)


def test_signal_sigterm_terminates_orphan() -> None:
    registry = AgentRegistry()
    desc = registry.spawn("agent-1", OWNER, ZONE)
    registry.signal(desc.pid, AgentSignal.SIGTERM)
    # Orphan auto-reaped.
    assert registry.get(desc.pid) is None


def test_signal_sigcont_bumps_generation() -> None:
    registry = AgentRegistry()
    desc = registry.spawn("agent-1", OWNER, ZONE)
    registry.update_state(desc.pid, AgentState.WARMING_UP.value)
    registry.update_state(desc.pid, AgentState.READY.value)
    registry.signal(desc.pid, AgentSignal.SIGSTOP)
    after = registry.signal(desc.pid, AgentSignal.SIGCONT)
    assert after.state == AgentState.READY
    assert after.generation == desc.generation + 1


# ---------------------------------------------------------------------------
# External agents
# ---------------------------------------------------------------------------


def test_register_external_produces_unmanaged_descriptor() -> None:
    registry = AgentRegistry()
    desc = registry.register_external(
        "ext-1",
        owner_id=OWNER,
        zone_id=ZONE,
        connection_id="conn-1",
        host_pid=4242,
        remote_addr="1.2.3.4:5678",
    )
    assert desc.pid == "conn-1"
    assert desc.kind == AgentKind.UNMANAGED
    assert isinstance(desc.external_info, dict)
    assert desc.external_info["host_pid"] == 4242
    assert desc.external_info["remote_addr"] == "1.2.3.4:5678"


def test_unregister_external_terminates_and_reaps() -> None:
    registry = AgentRegistry()
    registry.register_external("ext-1", owner_id=OWNER, zone_id=ZONE, connection_id="conn-1")
    registry.unregister_external("conn-1")
    assert registry.get("conn-1") is None


def test_heartbeat_rejects_managed_agent() -> None:
    registry = AgentRegistry()
    desc = registry.spawn("agent-1", OWNER, ZONE)
    with pytest.raises(ValueError):
        registry.heartbeat(desc.pid)


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------


def test_list_processes_with_filters() -> None:
    registry = AgentRegistry()
    registry.spawn("a1", OWNER, "zone-a")
    registry.spawn("a2", OWNER, "zone-b")
    registry.spawn("a3", "user-2", "zone-a")
    by_zone = registry.list_processes(zone_id="zone-a")
    assert {p.name for p in by_zone} == {"a1", "a3"}
    by_owner = registry.list_processes(owner_id="user-2")
    assert {p.name for p in by_owner} == {"a3"}


def test_count_by_state_scopes_to_zone() -> None:
    registry = AgentRegistry()
    desc = registry.spawn("agent-1", OWNER, ZONE)
    registry.update_state(desc.pid, AgentState.WARMING_UP.value)
    registry.update_state(desc.pid, AgentState.READY.value)
    registry.update_state(desc.pid, AgentState.BUSY.value)
    assert registry.count_by_state(AgentState.BUSY.value, zone_id=ZONE) == 1
    assert registry.count_by_state(AgentState.BUSY.value, zone_id="other") == 0


# ---------------------------------------------------------------------------
# wait_for_state — sync condvar-backed
# ---------------------------------------------------------------------------


def test_wait_for_state_returns_immediately_when_already_terminal() -> None:
    registry = AgentRegistry()
    desc = registry.spawn("agent-1", OWNER, ZONE)
    registry.update_state(desc.pid, AgentState.TERMINATED.value)
    state = registry.wait_for_state(desc.pid, AgentState.TERMINATED.value, 100)
    assert state == "TERMINATED"


@pytest.mark.asyncio
async def test_wait_for_state_via_to_thread_unblocks_on_transition() -> None:
    registry = AgentRegistry()
    desc = registry.spawn("agent-1", OWNER, ZONE)
    registry.update_state(desc.pid, AgentState.WARMING_UP.value)

    async def trip() -> None:
        await asyncio.sleep(0.02)
        registry.update_state(desc.pid, AgentState.READY.value)

    waiter = asyncio.create_task(
        asyncio.to_thread(registry.wait_for_state, desc.pid, AgentState.READY.value, 1000)
    )
    await trip()
    state = await waiter
    assert state == "READY"


# ---------------------------------------------------------------------------
# Provisioner late-bind hook
# ---------------------------------------------------------------------------


def test_set_and_get_provisioner_round_trip() -> None:
    registry = AgentRegistry()

    class _FakeProvisioner:
        async def provision(self, _agent_id: str, **_: object) -> None:
            return None

    p = _FakeProvisioner()
    registry.set_provisioner(p)
    fetched = registry.get_provisioner()
    assert fetched is p
    dropped = registry.take_provisioner()
    assert dropped is p
    assert registry.get_provisioner() is None


# ---------------------------------------------------------------------------
# VALID_AGENT_TRANSITIONS drift guard
# ---------------------------------------------------------------------------
#
# `contracts/process_types.py:VALID_AGENT_TRANSITIONS` and the Rust
# `AgentState::can_transition_to` table in
# `rust/kernel/src/core/agents/registry.rs` encode the same FSM and have
# to stay byte-identical.  This test exhaustively probes both directions
# through the PyO3 surface so a silent edit on either side surfaces here
# rather than as a runtime InvalidTransition deep in production.


def test_valid_transitions_match_rust_table() -> None:
    from nexus.contracts.process_types import VALID_AGENT_TRANSITIONS

    states = list(AgentState)
    for src in states:
        allowed = VALID_AGENT_TRANSITIONS[src]
        for dst in states:
            registry = AgentRegistry()
            desc = registry.spawn("a", OWNER, ZONE)

            # Drive the agent into `src` through legal hops so we can
            # then probe `src -> dst`.  REGISTERED is the spawn default;
            # all others require a chain of update_state calls.
            chain: dict[AgentState, list[AgentState]] = {
                AgentState.REGISTERED: [],
                AgentState.WARMING_UP: [AgentState.WARMING_UP],
                AgentState.READY: [AgentState.WARMING_UP, AgentState.READY],
                AgentState.BUSY: [
                    AgentState.WARMING_UP,
                    AgentState.READY,
                    AgentState.BUSY,
                ],
                AgentState.SUSPENDED: [
                    AgentState.WARMING_UP,
                    AgentState.READY,
                    AgentState.SUSPENDED,
                ],
                AgentState.TERMINATED: [AgentState.TERMINATED],
            }
            for hop in chain[src]:
                registry.update_state(desc.pid, hop.value)
            assert registry.get(desc.pid).state == src

            # Self-transition is always tolerated by the Rust table —
            # update_state returns Ok(true) without firing observers —
            # so `src == dst` is excluded from the drift comparison.
            if src == dst:
                continue

            should_succeed = dst in allowed
            if should_succeed:
                # No exception, post-state == dst.
                registry.update_state(desc.pid, dst.value)
                assert registry.get(desc.pid).state == dst
            else:
                with pytest.raises(ValueError):
                    registry.update_state(desc.pid, dst.value)


def test_terminated_is_terminal_in_both_tables() -> None:
    from nexus.contracts.process_types import VALID_AGENT_TRANSITIONS

    # Python side: TERMINATED has empty out-edges.
    assert VALID_AGENT_TRANSITIONS[AgentState.TERMINATED] == frozenset()

    # Rust side: every non-self transition out of TERMINATED rejects.
    for dst in AgentState:
        if dst == AgentState.TERMINATED:
            continue
        registry = AgentRegistry()
        desc = registry.spawn("a", OWNER, ZONE)
        registry.update_state(desc.pid, AgentState.TERMINATED.value)
        with pytest.raises(ValueError):
            registry.update_state(desc.pid, dst.value)
