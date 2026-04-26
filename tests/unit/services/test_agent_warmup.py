"""Tests for AgentWarmupService (Issue #2172).

Covers:
- Happy path: all required steps pass -> READY (connected)
- Mixed required + optional steps
- Required step failures (exception, timeout, returns False)
- Optional step failures -> still connected
- Edge cases: re-warmup, nonexistent agent, empty steps, concurrent, unregister-during
- Step registry operations

AgentRegistry migration notes:
  - External agents are registered via register_external() -> REGISTERED (generation=1)
  - To test warmup (which skips BUSY), we advance to READY then SIGSTOP -> SUSPENDED
  - After warmup, SIGCONT transitions SUSPENDED -> READY, bumping generation
  - AgentRegistry.get() uses PID, not name
"""

import asyncio
from datetime import timedelta

import pytest

from nexus.contracts.agent_warmup_types import WarmupContext, WarmupStep
from nexus.contracts.process_types import AgentSignal, AgentState
from nexus.services.agents.agent_registry import AgentRegistry
from nexus.services.agents.agent_warmup import AgentWarmupService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_registry():
    return AgentRegistry()


@pytest.fixture
def warmup_service(agent_registry):
    service = AgentWarmupService(
        agent_registry=agent_registry,
        enabled_bricks=frozenset({"search", "pay", "auth"}),
    )
    return service


def _register_agent(
    agent_registry: AgentRegistry, name: str = "agent-1", owner: str = "alice"
) -> str:
    """Register an external agent and STOP it so warmup can proceed.

    Returns the PID.  The process starts in REGISTERED (from register_external),
    then we advance to READY via WARMING_UP, then SIGSTOP to SUSPENDED so the
    warmup service does not skip it (warmup skips agents already in BUSY).
    """
    desc = agent_registry.register_external(
        name, owner_id=owner, zone_id="test", connection_id=f"conn-{name}"
    )
    # Move REGISTERED -> WARMING_UP -> READY -> SUSPENDED so warmup will not short-circuit
    desc = agent_registry._transition(desc, AgentState.WARMING_UP)
    desc = agent_registry._transition(desc, AgentState.READY)
    agent_registry.signal(desc.pid, AgentSignal.SIGSTOP)
    return desc.pid


# ---------------------------------------------------------------------------
# Helper step functions
# ---------------------------------------------------------------------------


async def _always_pass(_ctx: WarmupContext) -> bool:
    return True


async def _always_fail(_ctx: WarmupContext) -> bool:
    return False


async def _raise_error(_ctx: WarmupContext) -> bool:
    raise RuntimeError("step exploded")


async def _slow_step(_ctx: WarmupContext) -> bool:
    await asyncio.sleep(10)  # Will be cancelled by timeout
    return True


# ---------------------------------------------------------------------------
# Step registry tests
# ---------------------------------------------------------------------------


class TestStepRegistry:
    def test_register_and_get(self, warmup_service):
        warmup_service.register_step("test_step", _always_pass)
        fn = warmup_service.get_step("test_step")
        assert fn is _always_pass

    def test_unregistered_returns_none(self, warmup_service):
        assert warmup_service.get_step("nonexistent") is None

    def test_duplicate_registration_raises(self, warmup_service):
        warmup_service.register_step("dup", _always_pass)
        with pytest.raises(ValueError, match="already registered"):
            warmup_service.register_step("dup", _always_fail)


# ---------------------------------------------------------------------------
# Happy path tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_all_required_steps_pass(self, warmup_service, agent_registry):
        """All required steps pass -> agent transitions to READY (connected)."""
        pid = _register_agent(agent_registry)

        warmup_service.register_step("step_a", _always_pass)
        warmup_service.register_step("step_b", _always_pass)

        steps = [
            WarmupStep("step_a", timeout=timedelta(seconds=5)),
            WarmupStep("step_b", timeout=timedelta(seconds=5)),
        ]

        result = await warmup_service.warmup(pid, steps=steps)
        assert result.success is True
        assert result.agent_id == pid
        assert result.steps_completed == ("step_a", "step_b")
        assert result.steps_skipped == ()
        assert result.failed_step is None
        assert result.error is None
        assert result.duration_ms > 0

        # Verify agent transitioned (SIGCONT: SUSPENDED -> READY, generation bumped)
        desc = agent_registry.get(pid)
        assert desc.state is AgentState.READY
        assert desc.generation == 2  # 1 from spawn, +1 from SIGCONT

    @pytest.mark.asyncio
    async def test_mixed_required_and_optional(self, warmup_service, agent_registry):
        """Required passes, optional fails -> still connected."""
        pid = _register_agent(agent_registry)

        warmup_service.register_step("required_step", _always_pass)
        warmup_service.register_step("optional_step", _always_fail)

        steps = [
            WarmupStep("required_step", timeout=timedelta(seconds=5), required=True),
            WarmupStep("optional_step", timeout=timedelta(seconds=5), required=False),
        ]

        result = await warmup_service.warmup(pid, steps=steps)
        assert result.success is True
        assert "required_step" in result.steps_completed
        assert "optional_step" in result.steps_skipped

    @pytest.mark.asyncio
    async def test_standard_warmup_runs_in_order(self, warmup_service, agent_registry):
        """Steps execute in order -- verified by step_completed ordering."""
        pid = _register_agent(agent_registry)

        order: list[str] = []

        async def _tracking_step(name: str):
            async def _inner(_ctx: WarmupContext) -> bool:
                order.append(name)
                return True

            return _inner

        for name in ["a", "b", "c"]:
            warmup_service.register_step(name, await _tracking_step(name))

        steps = [
            WarmupStep("a", timeout=timedelta(seconds=5)),
            WarmupStep("b", timeout=timedelta(seconds=5)),
            WarmupStep("c", timeout=timedelta(seconds=5)),
        ]

        result = await warmup_service.warmup(pid, steps=steps)
        assert result.success is True
        assert order == ["a", "b", "c"]
        assert result.steps_completed == ("a", "b", "c")


# ---------------------------------------------------------------------------
# Required step failure tests
# ---------------------------------------------------------------------------


class TestRequiredStepFailure:
    @pytest.mark.asyncio
    async def test_required_step_exception(self, warmup_service, agent_registry):
        """Required step that raises -> warmup fails, agent stays SUSPENDED."""
        pid = _register_agent(agent_registry)

        warmup_service.register_step("boom", _raise_error)

        steps = [WarmupStep("boom", timeout=timedelta(seconds=5))]
        result = await warmup_service.warmup(pid, steps=steps)

        assert result.success is False
        assert result.failed_step == "boom"
        assert result.error is not None

        desc = agent_registry.get(pid)
        assert desc.state is AgentState.SUSPENDED

    @pytest.mark.asyncio
    async def test_required_step_timeout(self, warmup_service, agent_registry):
        """Required step that times out -> warmup fails."""
        pid = _register_agent(agent_registry)

        warmup_service.register_step("slow", _slow_step)

        steps = [WarmupStep("slow", timeout=timedelta(milliseconds=50))]
        result = await warmup_service.warmup(pid, steps=steps)

        assert result.success is False
        assert result.failed_step == "slow"

        desc = agent_registry.get(pid)
        assert desc.state is AgentState.SUSPENDED

    @pytest.mark.asyncio
    async def test_required_step_returns_false(self, warmup_service, agent_registry):
        """Required step returns False -> warmup fails."""
        pid = _register_agent(agent_registry)

        warmup_service.register_step("nope", _always_fail)

        steps = [WarmupStep("nope", timeout=timedelta(seconds=5))]
        result = await warmup_service.warmup(pid, steps=steps)

        assert result.success is False
        assert result.failed_step == "nope"

        desc = agent_registry.get(pid)
        assert desc.state is AgentState.SUSPENDED

    @pytest.mark.asyncio
    async def test_unregistered_required_step(self, warmup_service, agent_registry):
        """Required step not in registry -> warmup fails."""
        pid = _register_agent(agent_registry)

        steps = [WarmupStep("missing_step", timeout=timedelta(seconds=5), required=True)]
        result = await warmup_service.warmup(pid, steps=steps)

        assert result.success is False
        assert result.failed_step == "missing_step"
        assert "not registered" in result.error


# ---------------------------------------------------------------------------
# Optional step failure tests
# ---------------------------------------------------------------------------


class TestOptionalStepFailure:
    @pytest.mark.asyncio
    async def test_optional_step_fails_continues(self, warmup_service, agent_registry):
        """Optional step failure -> logged and skipped, warmup continues."""
        pid = _register_agent(agent_registry)

        warmup_service.register_step("opt_fail", _always_fail)
        warmup_service.register_step("required_ok", _always_pass)

        steps = [
            WarmupStep("opt_fail", timeout=timedelta(seconds=5), required=False),
            WarmupStep("required_ok", timeout=timedelta(seconds=5), required=True),
        ]

        result = await warmup_service.warmup(pid, steps=steps)
        assert result.success is True
        assert "opt_fail" in result.steps_skipped
        assert "required_ok" in result.steps_completed

    @pytest.mark.asyncio
    async def test_optional_step_timeout(self, warmup_service, agent_registry):
        """Optional step timeout -> skipped, warmup continues."""
        pid = _register_agent(agent_registry)

        warmup_service.register_step("slow_opt", _slow_step)
        warmup_service.register_step("fast_req", _always_pass)

        steps = [
            WarmupStep("slow_opt", timeout=timedelta(milliseconds=50), required=False),
            WarmupStep("fast_req", timeout=timedelta(seconds=5), required=True),
        ]

        result = await warmup_service.warmup(pid, steps=steps)
        assert result.success is True
        assert "slow_opt" in result.steps_skipped

    @pytest.mark.asyncio
    async def test_all_optional_steps_fail(self, warmup_service, agent_registry):
        """ALL optional steps fail -> still connected."""
        pid = _register_agent(agent_registry)

        warmup_service.register_step("opt1", _always_fail)
        warmup_service.register_step("opt2", _raise_error)

        steps = [
            WarmupStep("opt1", timeout=timedelta(seconds=5), required=False),
            WarmupStep("opt2", timeout=timedelta(seconds=5), required=False),
        ]

        result = await warmup_service.warmup(pid, steps=steps)
        assert result.success is True
        assert result.steps_skipped == ("opt1", "opt2")
        assert result.steps_completed == ()


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_warmup_already_busy(self, warmup_service, agent_registry):
        """Warmup on already-BUSY agent -> skipped (idempotent)."""
        # register_external creates in REGISTERED; advance to BUSY
        desc = agent_registry.register_external(
            "agent-1", owner_id="alice", zone_id="test", connection_id="conn-1"
        )
        desc = agent_registry._transition(desc, AgentState.WARMING_UP)
        desc = agent_registry._transition(desc, AgentState.READY)
        desc = agent_registry._transition(desc, AgentState.BUSY)

        result = await warmup_service.warmup(desc.pid, steps=[])
        assert result.success is True
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_warmup_nonexistent_agent(self, warmup_service):
        """Warmup on nonexistent agent -> error result."""
        result = await warmup_service.warmup("no-such-pid", steps=[])
        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_empty_step_list(self, warmup_service, agent_registry):
        """Empty step list -> immediate transition to READY."""
        pid = _register_agent(agent_registry)

        result = await warmup_service.warmup(pid, steps=[])
        assert result.success is True

        desc = agent_registry.get(pid)
        assert desc.state is AgentState.READY

    @pytest.mark.asyncio
    async def test_empty_step_list_from_registered_agent(self, warmup_service, agent_registry):
        """Freshly registered external agents can warm up directly to READY."""
        desc = agent_registry.register_external(
            "fresh-agent",
            owner_id="alice",
            zone_id="test",
            connection_id="conn-fresh-agent",
        )

        result = await warmup_service.warmup(desc.pid, steps=[])
        assert result.success is True

        updated = agent_registry.get(desc.pid)
        assert updated is not None
        assert updated.state is AgentState.READY
        assert updated.generation == desc.generation + 1

    @pytest.mark.asyncio
    async def test_agent_unregistered_during_warmup(self, warmup_service, agent_registry):
        """Agent unregistered between warmup start and transition -> clean failure."""
        pid = _register_agent(agent_registry)

        async def _unregister_and_pass(ctx: WarmupContext) -> bool:
            # Simulate agent being unregistered during warmup.
            # unregister_external kills + reaps the process.
            agent_registry.unregister_external(ctx.agent_id)
            return True

        warmup_service.register_step("sneaky", _unregister_and_pass)

        steps = [WarmupStep("sneaky", timeout=timedelta(seconds=5))]
        result = await warmup_service.warmup(pid, steps=steps)

        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_concurrent_warmup_same_agent(self, warmup_service, agent_registry):
        """Concurrent warmup for same agent -> second fails (SLEEPING->SLEEPING invalid)."""
        pid = _register_agent(agent_registry)

        warmup_service.register_step("pass", _always_pass)

        steps = [WarmupStep("pass", timeout=timedelta(seconds=5))]

        # First warmup succeeds: SUSPENDED -> READY via SIGCONT
        result1 = await warmup_service.warmup(pid, steps=steps)
        assert result1.success is True

        # Second warmup: agent is now READY (not BUSY, so not skipped).
        # Steps pass, but _transition_connected calls signal(SIGCONT) which
        # tries READY -> READY -- an invalid transition. Warmup fails
        # cleanly via InvalidTransitionError handling.
        result2 = await warmup_service.warmup(pid, steps=steps)
        assert result2.success is False
        assert result2.error is not None

    @pytest.mark.asyncio
    async def test_unregistered_optional_step_skipped(self, warmup_service, agent_registry):
        """Optional step not in registry -> skipped, not failed."""
        pid = _register_agent(agent_registry)

        steps = [WarmupStep("nonexistent_opt", timeout=timedelta(seconds=5), required=False)]
        result = await warmup_service.warmup(pid, steps=steps)

        assert result.success is True
        assert "nonexistent_opt" in result.steps_skipped
