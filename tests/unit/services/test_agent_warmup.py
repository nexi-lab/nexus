"""Tests for AgentWarmupService (Issue #2172).

Covers:
- Happy path: all required steps pass -> SLEEPING (connected)
- Mixed required + optional steps
- Required step failures (exception, timeout, returns False)
- Optional step failures -> still connected
- Edge cases: re-warmup, nonexistent agent, empty steps, concurrent, unregister-during
- Step registry operations

ProcessTable migration notes:
  - External agents are registered via register_external() -> RUNNING (generation=1)
  - To test warmup (which skips RUNNING), we SIGSTOP -> STOPPED first
  - After warmup, SIGCONT transitions STOPPED -> SLEEPING, bumping generation
  - ProcessTable.get() uses PID, not name
"""

import asyncio
from datetime import timedelta

import pytest

from nexus.contracts.agent_warmup_types import WarmupContext, WarmupStep
from nexus.contracts.process_types import ProcessSignal, ProcessState
from nexus.core.process_table import ProcessTable
from nexus.system_services.agents.agent_warmup import AgentWarmupService
from tests.helpers.dict_metastore import DictMetastore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def process_table():
    return ProcessTable(DictMetastore(), zone_id="test")


@pytest.fixture
def warmup_service(process_table):
    service = AgentWarmupService(
        process_table=process_table,
        enabled_bricks=frozenset({"search", "pay", "auth"}),
    )
    return service


def _register_agent(
    process_table: ProcessTable, name: str = "agent-1", owner: str = "alice"
) -> str:
    """Register an external agent and STOP it so warmup can proceed.

    Returns the PID.  The process starts in RUNNING (from register_external),
    then we SIGSTOP it to STOPPED so the warmup service does not skip it
    (warmup skips agents already in RUNNING).
    """
    desc = process_table.register_external(
        name, owner_id=owner, zone_id="test", connection_id=f"conn-{name}"
    )
    # Move RUNNING -> STOPPED so warmup will not short-circuit
    process_table.signal(desc.pid, ProcessSignal.SIGSTOP)
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
    async def test_all_required_steps_pass(self, warmup_service, process_table):
        """All required steps pass -> agent transitions to SLEEPING (connected)."""
        pid = _register_agent(process_table)

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

        # Verify agent transitioned (SIGCONT: STOPPED -> SLEEPING, generation bumped)
        desc = process_table.get(pid)
        assert desc.state is ProcessState.SLEEPING
        assert desc.generation == 2  # 1 from spawn, +1 from SIGCONT

    @pytest.mark.asyncio
    async def test_mixed_required_and_optional(self, warmup_service, process_table):
        """Required passes, optional fails -> still connected."""
        pid = _register_agent(process_table)

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
    async def test_standard_warmup_runs_in_order(self, warmup_service, process_table):
        """Steps execute in order -- verified by step_completed ordering."""
        pid = _register_agent(process_table)

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
    async def test_required_step_exception(self, warmup_service, process_table):
        """Required step that raises -> warmup fails, agent stays STOPPED."""
        pid = _register_agent(process_table)

        warmup_service.register_step("boom", _raise_error)

        steps = [WarmupStep("boom", timeout=timedelta(seconds=5))]
        result = await warmup_service.warmup(pid, steps=steps)

        assert result.success is False
        assert result.failed_step == "boom"
        assert result.error is not None

        desc = process_table.get(pid)
        assert desc.state is ProcessState.STOPPED

    @pytest.mark.asyncio
    async def test_required_step_timeout(self, warmup_service, process_table):
        """Required step that times out -> warmup fails."""
        pid = _register_agent(process_table)

        warmup_service.register_step("slow", _slow_step)

        steps = [WarmupStep("slow", timeout=timedelta(milliseconds=50))]
        result = await warmup_service.warmup(pid, steps=steps)

        assert result.success is False
        assert result.failed_step == "slow"

        desc = process_table.get(pid)
        assert desc.state is ProcessState.STOPPED

    @pytest.mark.asyncio
    async def test_required_step_returns_false(self, warmup_service, process_table):
        """Required step returns False -> warmup fails."""
        pid = _register_agent(process_table)

        warmup_service.register_step("nope", _always_fail)

        steps = [WarmupStep("nope", timeout=timedelta(seconds=5))]
        result = await warmup_service.warmup(pid, steps=steps)

        assert result.success is False
        assert result.failed_step == "nope"

        desc = process_table.get(pid)
        assert desc.state is ProcessState.STOPPED

    @pytest.mark.asyncio
    async def test_unregistered_required_step(self, warmup_service, process_table):
        """Required step not in registry -> warmup fails."""
        pid = _register_agent(process_table)

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
    async def test_optional_step_fails_continues(self, warmup_service, process_table):
        """Optional step failure -> logged and skipped, warmup continues."""
        pid = _register_agent(process_table)

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
    async def test_optional_step_timeout(self, warmup_service, process_table):
        """Optional step timeout -> skipped, warmup continues."""
        pid = _register_agent(process_table)

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
    async def test_all_optional_steps_fail(self, warmup_service, process_table):
        """ALL optional steps fail -> still connected."""
        pid = _register_agent(process_table)

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
    async def test_warmup_already_running(self, warmup_service, process_table):
        """Warmup on already-RUNNING agent -> skipped (idempotent)."""
        # register_external creates in RUNNING; do NOT stop it
        desc = process_table.register_external(
            "agent-1", owner_id="alice", zone_id="test", connection_id="conn-1"
        )

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
    async def test_empty_step_list(self, warmup_service, process_table):
        """Empty step list -> immediate transition to SLEEPING."""
        pid = _register_agent(process_table)

        result = await warmup_service.warmup(pid, steps=[])
        assert result.success is True

        desc = process_table.get(pid)
        assert desc.state is ProcessState.SLEEPING

    @pytest.mark.asyncio
    async def test_agent_unregistered_during_warmup(self, warmup_service, process_table):
        """Agent unregistered between warmup start and transition -> clean failure."""
        pid = _register_agent(process_table)

        async def _unregister_and_pass(ctx: WarmupContext) -> bool:
            # Simulate agent being unregistered during warmup.
            # unregister_external kills + reaps the process.
            process_table.unregister_external(ctx.agent_id)
            return True

        warmup_service.register_step("sneaky", _unregister_and_pass)

        steps = [WarmupStep("sneaky", timeout=timedelta(seconds=5))]
        result = await warmup_service.warmup(pid, steps=steps)

        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_concurrent_warmup_same_agent(self, warmup_service, process_table):
        """Concurrent warmup for same agent -> second fails (SLEEPING->SLEEPING invalid)."""
        pid = _register_agent(process_table)

        warmup_service.register_step("pass", _always_pass)

        steps = [WarmupStep("pass", timeout=timedelta(seconds=5))]

        # First warmup succeeds: STOPPED -> SLEEPING via SIGCONT
        result1 = await warmup_service.warmup(pid, steps=steps)
        assert result1.success is True

        # Second warmup: agent is now SLEEPING (not RUNNING, so not skipped).
        # Steps pass, but _transition_connected calls signal(SIGCONT) which
        # tries SLEEPING -> SLEEPING -- an invalid transition. Warmup fails
        # cleanly via InvalidTransitionError handling.
        result2 = await warmup_service.warmup(pid, steps=steps)
        assert result2.success is False
        assert result2.error is not None

    @pytest.mark.asyncio
    async def test_unregistered_optional_step_skipped(self, warmup_service, process_table):
        """Optional step not in registry -> skipped, not failed."""
        pid = _register_agent(process_table)

        steps = [WarmupStep("nonexistent_opt", timeout=timedelta(seconds=5), required=False)]
        result = await warmup_service.warmup(pid, steps=steps)

        assert result.success is True
        assert "nonexistent_opt" in result.steps_skipped
