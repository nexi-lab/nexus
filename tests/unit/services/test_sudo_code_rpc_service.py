"""Tests for SudoCodeRPCService — sudowork ↔ nexusd session lifecycle.

Mocks AgentRegistry (single authority over pid identity) and the
AgentRuntimeRegistry slot (kernel-knows trait DI for the in-process
sudo-code crate). Covers the contract sudowork sees: spawn / cancel /
get_session, plus the fail-loudly behaviour when no runtime is
registered for the agent (silent failure would have sudowork waiting
for responses that never come).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.process_types import AgentKind, AgentState
from nexus.services.sudo_code import SudoCodeRPCService

# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


@dataclass
class MockProcessDescriptor:
    pid: str = "pid-1"
    name: str = "scode-standard"
    state: AgentState = AgentState.READY
    owner_id: str = "user1"
    zone_id: str = ROOT_ZONE_ID
    labels: dict[str, str] = field(default_factory=dict)


class MockAgentRegistry:
    """Records spawn / kill / get calls so tests can assert on identity wiring."""

    def __init__(self) -> None:
        self._next_pid = 1
        self._procs: dict[str, MockProcessDescriptor] = {}
        self.spawn_calls: list[dict[str, Any]] = []
        self.kill_calls: list[tuple[str, int]] = []

    def spawn(self, *, name, owner_id, zone_id, kind, labels=None) -> MockProcessDescriptor:
        pid = f"pid-{self._next_pid}"
        self._next_pid += 1
        desc = MockProcessDescriptor(
            pid=pid,
            name=name,
            owner_id=owner_id,
            zone_id=zone_id,
            labels=labels or {},
        )
        self._procs[pid] = desc
        self.spawn_calls.append(
            {
                "name": name,
                "owner_id": owner_id,
                "zone_id": zone_id,
                "kind": kind,
                "labels": labels,
                "pid": pid,
            }
        )
        return desc

    def kill(self, pid: str, exit_code: int = 0) -> MockProcessDescriptor:
        self.kill_calls.append((pid, exit_code))
        return self._procs.pop(pid, MockProcessDescriptor(pid=pid, state=AgentState.TERMINATED))

    def get(self, pid: str) -> MockProcessDescriptor | None:
        return self._procs.get(pid)


class MockRuntime:
    """In-process runtime stub — what the sudo-code crate's trait impl looks like."""

    def __init__(self, *, spawn_raises: Exception | None = None) -> None:
        self.spawn_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self._spawn_raises = spawn_raises

    def spawn(self, *, pid, workspace_path, repos, model) -> None:
        self.spawn_calls.append(
            {
                "pid": pid,
                "workspace_path": workspace_path,
                "repos": list(repos),
                "model": model,
            }
        )
        if self._spawn_raises is not None:
            raise self._spawn_raises

    def cancel(self, *, pid, mode) -> None:
        self.cancel_calls.append({"pid": pid, "mode": mode})


class MockRuntimeRegistry:
    """Kernel-knows slot: name → runtime. ``None`` for unregistered agents."""

    def __init__(self, runtimes: dict[str, MockRuntime] | None = None) -> None:
        self._runtimes = runtimes or {}
        self.get_calls: list[str] = []

    def get(self, agent: str) -> MockRuntime | None:
        self.get_calls.append(agent)
        return self._runtimes.get(agent)


# ---------------------------------------------------------------------------
# start_session
# ---------------------------------------------------------------------------


class TestStartSession:
    @pytest.mark.asyncio
    async def test_spawns_agent_and_returns_identity_tuple(self):
        ar = MockAgentRegistry()
        runtime = MockRuntime()
        rr = MockRuntimeRegistry({"scode-standard": runtime})
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=rr)

        result = await svc.sudo_code_start_session(
            agent="scode-standard",
            repos=[{"host_path": "/repo/a", "alias": "a"}],
            model="claude-sonnet-4-6",
        )

        assert result["agent_id"] == "pid-1"
        assert result["workspace_path"] == "/proc/pid-1/workspace/"
        assert result["session_id"].startswith("sess-")

        # AgentRegistry got the spawn with kind=MANAGED + agent + service labels.
        assert len(ar.spawn_calls) == 1
        call = ar.spawn_calls[0]
        assert call["name"] == "scode-standard"
        assert call["kind"] is AgentKind.MANAGED
        assert call["labels"] == {
            "service": "sudo_code",
            "agent": "scode-standard",
            "model": "claude-sonnet-4-6",
        }

        # Runtime was driven with the same pid + workspace.
        assert len(runtime.spawn_calls) == 1
        rs = runtime.spawn_calls[0]
        assert rs["pid"] == "pid-1"
        assert rs["workspace_path"] == "/proc/pid-1/workspace/"
        assert rs["repos"] == [{"host_path": "/repo/a", "alias": "a"}]
        assert rs["model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_no_runtime_registry_reaps_pid_and_raises(self):
        """When runtime_registry is None, the service falls back to a
        default-empty registry. start_session must fail loudly: returning
        a session_id with no runtime driving it would be silent failure
        (sudowork would wait for responses that never come)."""
        ar = MockAgentRegistry()
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=None)

        with pytest.raises(RuntimeError, match="no runtime registered"):
            await svc.sudo_code_start_session(agent="scode-standard")

        assert len(ar.spawn_calls) == 1
        assert ar.kill_calls == [("pid-1", -1)]

    @pytest.mark.asyncio
    async def test_runtime_not_registered_for_agent_reaps_pid_and_raises(self):
        """Registry exists but no runtime for this agent — reap and raise."""
        ar = MockAgentRegistry()
        rr = MockRuntimeRegistry()  # empty
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=rr)

        with pytest.raises(RuntimeError, match="no runtime registered"):
            await svc.sudo_code_start_session(agent="scode-fast")

        assert ar.kill_calls == [("pid-1", -1)]

    @pytest.mark.asyncio
    async def test_runtime_spawn_failure_reaps_pid_and_raises(self):
        """On runtime.spawn() failure, AgentRegistry.kill is called so the
        record doesn't drift, and the caller gets a clear RuntimeError."""
        ar = MockAgentRegistry()
        runtime = MockRuntime(spawn_raises=RuntimeError("boom"))
        rr = MockRuntimeRegistry({"scode-standard": runtime})
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=rr)

        with pytest.raises(RuntimeError, match="failed to spawn"):
            await svc.sudo_code_start_session(agent="scode-standard")

        assert ar.kill_calls == [("pid-1", -1)]

    @pytest.mark.asyncio
    async def test_empty_agent_rejected(self):
        ar = MockAgentRegistry()
        svc = SudoCodeRPCService(agent_registry=ar)
        with pytest.raises(ValueError, match="'agent' is required"):
            await svc.sudo_code_start_session(agent="")
        assert ar.spawn_calls == []

    @pytest.mark.asyncio
    async def test_zone_and_owner_pulled_from_context(self):
        ar = MockAgentRegistry()
        rr = MockRuntimeRegistry({"scode-standard": MockRuntime()})
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=rr, zone_id="default")

        await svc.sudo_code_start_session(
            agent="scode-standard",
            context={"zone_id": "tenant-7", "user_id": "ethan"},
        )

        assert ar.spawn_calls[0]["zone_id"] == "tenant-7"
        assert ar.spawn_calls[0]["owner_id"] == "ethan"

    @pytest.mark.asyncio
    async def test_zone_and_owner_default_when_no_context(self):
        ar = MockAgentRegistry()
        rr = MockRuntimeRegistry({"scode-standard": MockRuntime()})
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=rr, zone_id="default")

        await svc.sudo_code_start_session(agent="scode-standard")

        assert ar.spawn_calls[0]["zone_id"] == "default"
        assert ar.spawn_calls[0]["owner_id"] == "system"

    @pytest.mark.asyncio
    async def test_runtime_registry_lookup_failure_reaps_pid_and_raises(self):
        """A buggy registry is a misconfiguration — fail loudly so sudowork
        doesn't think the session is live."""
        ar = MockAgentRegistry()
        rr = MagicMock()
        rr.get.side_effect = RuntimeError("registry exploded")
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=rr)

        with pytest.raises(RuntimeError, match="lookup failed"):
            await svc.sudo_code_start_session(agent="scode-standard")

        assert ar.kill_calls == [("pid-1", -1)]


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_session_kills_pid_and_drops_session(self):
        ar = MockAgentRegistry()
        runtime = MockRuntime()
        rr = MockRuntimeRegistry({"scode-standard": runtime})
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=rr)

        started = await svc.sudo_code_start_session(agent="scode-standard")
        sess_id = started["session_id"]

        result = await svc.sudo_code_cancel(session_id=sess_id, mode="cancel_session")

        assert result["cancelled"] is True
        assert ar.kill_calls == [("pid-1", 0)]
        assert runtime.cancel_calls == [{"pid": "pid-1", "mode": "cancel_session"}]
        # Session map cleared — second cancel raises LookupError.
        with pytest.raises(LookupError):
            await svc.sudo_code_cancel(session_id=sess_id)

    @pytest.mark.asyncio
    async def test_cancel_turn_does_not_kill_pid(self):
        ar = MockAgentRegistry()
        runtime = MockRuntime()
        rr = MockRuntimeRegistry({"scode-standard": runtime})
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=rr)

        started = await svc.sudo_code_start_session(agent="scode-standard")

        result = await svc.sudo_code_cancel(session_id=started["session_id"], mode="cancel_turn")

        assert result["cancelled"] is True
        assert ar.kill_calls == []  # session stays alive
        assert runtime.cancel_calls == [{"pid": "pid-1", "mode": "cancel_turn"}]

    @pytest.mark.asyncio
    async def test_cancel_unknown_session_raises_lookup_error(self):
        ar = MockAgentRegistry()
        svc = SudoCodeRPCService(agent_registry=ar)
        with pytest.raises(LookupError, match="unknown session_id"):
            await svc.sudo_code_cancel(session_id="sess-bogus")

    @pytest.mark.asyncio
    async def test_cancel_invalid_mode_rejected(self):
        ar = MockAgentRegistry()
        rr = MockRuntimeRegistry({"scode-standard": MockRuntime()})
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=rr)
        started = await svc.sudo_code_start_session(agent="scode-standard")
        with pytest.raises(ValueError, match="must be 'cancel_turn' or 'cancel_session'"):
            await svc.sudo_code_cancel(session_id=started["session_id"], mode="explode")


# ---------------------------------------------------------------------------
# get_session
# ---------------------------------------------------------------------------


class TestGetSession:
    @pytest.mark.asyncio
    async def test_returns_state_from_registry(self):
        ar = MockAgentRegistry()
        rr = MockRuntimeRegistry({"scode-standard": MockRuntime()})
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=rr)
        started = await svc.sudo_code_start_session(agent="scode-standard")

        snap = await svc.sudo_code_get_session(session_id=started["session_id"])

        assert snap["session_id"] == started["session_id"]
        assert snap["agent_id"] == "pid-1"
        assert snap["agent"] == "scode-standard"
        assert snap["workspace_path"] == "/proc/pid-1/workspace/"
        assert snap["state"] == "ready"

    @pytest.mark.asyncio
    async def test_terminated_pid_surfaces_as_terminated_state(self):
        """When AgentRegistry.get returns None (pid reaped), state is 'terminated'."""
        ar = MockAgentRegistry()
        rr = MockRuntimeRegistry({"scode-standard": MockRuntime()})
        svc = SudoCodeRPCService(agent_registry=ar, runtime_registry=rr)
        started = await svc.sudo_code_start_session(agent="scode-standard")
        # Simulate pid reaped out-of-band.
        ar._procs.clear()

        snap = await svc.sudo_code_get_session(session_id=started["session_id"])
        assert snap["state"] == "terminated"

    @pytest.mark.asyncio
    async def test_unknown_session_raises_lookup_error(self):
        ar = MockAgentRegistry()
        svc = SudoCodeRPCService(agent_registry=ar)
        with pytest.raises(LookupError, match="unknown session_id"):
            await svc.sudo_code_get_session(session_id="sess-bogus")
