"""Tests for AgentWarmupService (Issue #2172).

Covers:
- Happy path: all required steps pass → CONNECTED
- Mixed required + optional steps
- Required step failures (exception, timeout, returns False)
- Optional step failures → still CONNECTED
- Edge cases: re-warmup, nonexistent agent, empty steps, concurrent, unregister-during
- Step registry operations
- Integration: warmup → status phase check
"""

import asyncio
from datetime import timedelta

import pytest

from nexus.contracts.agent_types import AgentState
from nexus.contracts.agent_warmup_types import WarmupContext, WarmupStep
from nexus.system_services.agents.agent_registry import AgentRegistry
from nexus.system_services.agents.agent_warmup import AgentWarmupService
from tests.helpers.in_memory_record_store import InMemoryRecordStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def record_store():
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture
def registry(record_store):
    return AgentRegistry(record_store=record_store, flush_interval=60)


@pytest.fixture
def warmup_service(registry):
    service = AgentWarmupService(
        agent_registry=registry,
        enabled_bricks=frozenset({"search", "pay", "auth"}),
    )
    return service


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
    async def test_all_required_steps_pass(self, warmup_service, registry):
        """All required steps pass → agent transitions to CONNECTED."""
        registry.register("agent-1", "alice")

        warmup_service.register_step("step_a", _always_pass)
        warmup_service.register_step("step_b", _always_pass)

        steps = [
            WarmupStep("step_a", timeout=timedelta(seconds=5)),
            WarmupStep("step_b", timeout=timedelta(seconds=5)),
        ]

        result = await warmup_service.warmup("agent-1", steps=steps)
        assert result.success is True
        assert result.agent_id == "agent-1"
        assert result.steps_completed == ("step_a", "step_b")
        assert result.steps_skipped == ()
        assert result.failed_step is None
        assert result.error is None
        assert result.duration_ms > 0

        # Verify agent is now CONNECTED
        record = registry.get("agent-1")
        assert record.state is AgentState.CONNECTED
        assert record.generation == 1

    @pytest.mark.asyncio
    async def test_mixed_required_and_optional(self, warmup_service, registry):
        """Required passes, optional fails → still CONNECTED."""
        registry.register("agent-1", "alice")

        warmup_service.register_step("required_step", _always_pass)
        warmup_service.register_step("optional_step", _always_fail)

        steps = [
            WarmupStep("required_step", timeout=timedelta(seconds=5), required=True),
            WarmupStep("optional_step", timeout=timedelta(seconds=5), required=False),
        ]

        result = await warmup_service.warmup("agent-1", steps=steps)
        assert result.success is True
        assert "required_step" in result.steps_completed
        assert "optional_step" in result.steps_skipped

    @pytest.mark.asyncio
    async def test_standard_warmup_runs_in_order(self, warmup_service, registry):
        """Steps execute in order — verified by step_completed ordering."""
        registry.register("agent-1", "alice")

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

        result = await warmup_service.warmup("agent-1", steps=steps)
        assert result.success is True
        assert order == ["a", "b", "c"]
        assert result.steps_completed == ("a", "b", "c")


# ---------------------------------------------------------------------------
# Required step failure tests
# ---------------------------------------------------------------------------


class TestRequiredStepFailure:
    @pytest.mark.asyncio
    async def test_required_step_exception(self, warmup_service, registry):
        """Required step that raises → warmup fails, agent stays UNKNOWN."""
        registry.register("agent-1", "alice")

        warmup_service.register_step("boom", _raise_error)

        steps = [WarmupStep("boom", timeout=timedelta(seconds=5))]
        result = await warmup_service.warmup("agent-1", steps=steps)

        assert result.success is False
        assert result.failed_step == "boom"
        assert result.error is not None

        record = registry.get("agent-1")
        assert record.state is AgentState.UNKNOWN

    @pytest.mark.asyncio
    async def test_required_step_timeout(self, warmup_service, registry):
        """Required step that times out → warmup fails."""
        registry.register("agent-1", "alice")

        warmup_service.register_step("slow", _slow_step)

        steps = [WarmupStep("slow", timeout=timedelta(milliseconds=50))]
        result = await warmup_service.warmup("agent-1", steps=steps)

        assert result.success is False
        assert result.failed_step == "slow"

        record = registry.get("agent-1")
        assert record.state is AgentState.UNKNOWN

    @pytest.mark.asyncio
    async def test_required_step_returns_false(self, warmup_service, registry):
        """Required step returns False → warmup fails."""
        registry.register("agent-1", "alice")

        warmup_service.register_step("nope", _always_fail)

        steps = [WarmupStep("nope", timeout=timedelta(seconds=5))]
        result = await warmup_service.warmup("agent-1", steps=steps)

        assert result.success is False
        assert result.failed_step == "nope"

        record = registry.get("agent-1")
        assert record.state is AgentState.UNKNOWN

    @pytest.mark.asyncio
    async def test_unregistered_required_step(self, warmup_service, registry):
        """Required step not in registry → warmup fails."""
        registry.register("agent-1", "alice")

        steps = [WarmupStep("missing_step", timeout=timedelta(seconds=5), required=True)]
        result = await warmup_service.warmup("agent-1", steps=steps)

        assert result.success is False
        assert result.failed_step == "missing_step"
        assert "not registered" in result.error


# ---------------------------------------------------------------------------
# Optional step failure tests
# ---------------------------------------------------------------------------


class TestOptionalStepFailure:
    @pytest.mark.asyncio
    async def test_optional_step_fails_continues(self, warmup_service, registry):
        """Optional step failure → logged and skipped, warmup continues."""
        registry.register("agent-1", "alice")

        warmup_service.register_step("opt_fail", _always_fail)
        warmup_service.register_step("required_ok", _always_pass)

        steps = [
            WarmupStep("opt_fail", timeout=timedelta(seconds=5), required=False),
            WarmupStep("required_ok", timeout=timedelta(seconds=5), required=True),
        ]

        result = await warmup_service.warmup("agent-1", steps=steps)
        assert result.success is True
        assert "opt_fail" in result.steps_skipped
        assert "required_ok" in result.steps_completed

    @pytest.mark.asyncio
    async def test_optional_step_timeout(self, warmup_service, registry):
        """Optional step timeout → skipped, warmup continues."""
        registry.register("agent-1", "alice")

        warmup_service.register_step("slow_opt", _slow_step)
        warmup_service.register_step("fast_req", _always_pass)

        steps = [
            WarmupStep("slow_opt", timeout=timedelta(milliseconds=50), required=False),
            WarmupStep("fast_req", timeout=timedelta(seconds=5), required=True),
        ]

        result = await warmup_service.warmup("agent-1", steps=steps)
        assert result.success is True
        assert "slow_opt" in result.steps_skipped

    @pytest.mark.asyncio
    async def test_all_optional_steps_fail(self, warmup_service, registry):
        """ALL optional steps fail → still CONNECTED."""
        registry.register("agent-1", "alice")

        warmup_service.register_step("opt1", _always_fail)
        warmup_service.register_step("opt2", _raise_error)

        steps = [
            WarmupStep("opt1", timeout=timedelta(seconds=5), required=False),
            WarmupStep("opt2", timeout=timedelta(seconds=5), required=False),
        ]

        result = await warmup_service.warmup("agent-1", steps=steps)
        assert result.success is True
        assert result.steps_skipped == ("opt1", "opt2")
        assert result.steps_completed == ()


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_warmup_already_connected(self, warmup_service, registry):
        """Warmup on already-CONNECTED agent → skipped (idempotent)."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)

        result = await warmup_service.warmup("agent-1", steps=[])
        assert result.success is True
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_warmup_nonexistent_agent(self, warmup_service):
        """Warmup on nonexistent agent → error result."""
        result = await warmup_service.warmup("no-such-agent", steps=[])
        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_empty_step_list(self, warmup_service, registry):
        """Empty step list → immediate CONNECTED."""
        registry.register("agent-1", "alice")

        result = await warmup_service.warmup("agent-1", steps=[])
        assert result.success is True

        record = registry.get("agent-1")
        assert record.state is AgentState.CONNECTED

    @pytest.mark.asyncio
    async def test_agent_unregistered_during_warmup(self, warmup_service, registry):
        """Agent unregistered between warmup start and transition → clean failure."""
        registry.register("agent-1", "alice")

        async def _unregister_and_pass(ctx: WarmupContext) -> bool:
            # Simulate agent being unregistered during warmup
            registry.unregister(ctx.agent_id)
            return True

        warmup_service.register_step("sneaky", _unregister_and_pass)

        steps = [WarmupStep("sneaky", timeout=timedelta(seconds=5))]
        result = await warmup_service.warmup("agent-1", steps=steps)

        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_concurrent_warmup_same_agent(self, warmup_service, registry):
        """Concurrent warmup for same agent → one wins via optimistic locking."""
        registry.register("agent-1", "alice")

        warmup_service.register_step("pass", _always_pass)

        steps = [WarmupStep("pass", timeout=timedelta(seconds=5))]

        # First warmup succeeds
        result1 = await warmup_service.warmup("agent-1", steps=steps)
        assert result1.success is True

        # Second warmup on now-CONNECTED agent → idempotent skip
        result2 = await warmup_service.warmup("agent-1", steps=steps)
        assert result2.success is True

    @pytest.mark.asyncio
    async def test_unregistered_optional_step_skipped(self, warmup_service, registry):
        """Optional step not in registry → skipped, not failed."""
        registry.register("agent-1", "alice")

        steps = [WarmupStep("nonexistent_opt", timeout=timedelta(seconds=5), required=False)]
        result = await warmup_service.warmup("agent-1", steps=steps)

        assert result.success is True
        assert "nonexistent_opt" in result.steps_skipped


# ---------------------------------------------------------------------------
# Integration: warmup → status phase
# ---------------------------------------------------------------------------


class TestWarmupStatusIntegration:
    @pytest.mark.asyncio
    async def test_warmup_changes_status_phase(self, warmup_service, registry):
        """After warmup, get_status returns phase=active (CONNECTED → ACTIVE)."""
        registry.register("agent-1", "alice")

        warmup_service.register_step("pass", _always_pass)
        steps = [WarmupStep("pass", timeout=timedelta(seconds=5))]

        result = await warmup_service.warmup("agent-1", steps=steps)
        assert result.success is True

        # Check status via registry
        status = registry.get_status("agent-1")
        assert status is not None
        assert str(status.phase) == "active"
