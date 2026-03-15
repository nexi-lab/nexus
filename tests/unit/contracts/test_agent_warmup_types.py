"""Tests for agent warmup types (Issue #2172).

Verifies:
1. WarmupStep frozen dataclass with defaults
2. WarmupResult frozen dataclass with all fields
3. WarmupContext frozen dataclass
4. STANDARD_WARMUP sequence structure
"""

import dataclasses
from datetime import timedelta

import pytest

from nexus.contracts.agent_warmup_types import (
    STANDARD_WARMUP,
    WarmupContext,
    WarmupResult,
    WarmupStep,
)

# ---------------------------------------------------------------------------
# WarmupStep
# ---------------------------------------------------------------------------


class TestWarmupStep:
    def test_defaults(self) -> None:
        step = WarmupStep(name="test_step")
        assert step.name == "test_step"
        assert step.timeout == timedelta(seconds=30)
        assert step.required is True

    def test_custom_values(self) -> None:
        step = WarmupStep(name="slow_step", timeout=timedelta(seconds=60), required=False)
        assert step.name == "slow_step"
        assert step.timeout == timedelta(seconds=60)
        assert step.required is False

    def test_frozen(self) -> None:
        step = WarmupStep(name="immutable")
        with pytest.raises(dataclasses.FrozenInstanceError):
            step.name = "mutated"

    def test_equality(self) -> None:
        a = WarmupStep(name="x", timeout=timedelta(seconds=5))
        b = WarmupStep(name="x", timeout=timedelta(seconds=5))
        assert a == b

    def test_inequality(self) -> None:
        a = WarmupStep(name="x", required=True)
        b = WarmupStep(name="x", required=False)
        assert a != b


# ---------------------------------------------------------------------------
# WarmupResult
# ---------------------------------------------------------------------------


class TestWarmupResult:
    def test_success_result(self) -> None:
        result = WarmupResult(
            success=True,
            agent_id="agent-1",
            steps_completed=("load_credentials", "mount_namespace"),
            duration_ms=42.5,
        )
        assert result.success is True
        assert result.agent_id == "agent-1"
        assert result.steps_completed == ("load_credentials", "mount_namespace")
        assert result.steps_skipped == ()
        assert result.failed_step is None
        assert result.error is None
        assert result.duration_ms == 42.5

    def test_failure_result(self) -> None:
        result = WarmupResult(
            success=False,
            agent_id="agent-2",
            steps_completed=("load_credentials",),
            steps_skipped=("warm_caches",),
            failed_step="mount_namespace",
            error="Required step 'mount_namespace' failed",
            duration_ms=100.0,
        )
        assert result.success is False
        assert result.failed_step == "mount_namespace"
        assert result.error is not None
        assert "mount_namespace" in result.error

    def test_frozen(self) -> None:
        result = WarmupResult(success=True, agent_id="agent-1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.success = False

    def test_defaults(self) -> None:
        result = WarmupResult(success=True, agent_id="agent-1")
        assert result.steps_completed == ()
        assert result.steps_skipped == ()
        assert result.failed_step is None
        assert result.error is None
        assert result.duration_ms == 0.0


# ---------------------------------------------------------------------------
# WarmupContext
# ---------------------------------------------------------------------------


class TestWarmupContext:
    def test_minimal(self) -> None:
        ctx = WarmupContext(
            agent_id="agent-1",
            agent_record={"fake": "record"},
            agent_registry={"fake": "registry"},
        )
        assert ctx.agent_id == "agent-1"
        assert ctx.namespace_manager is None
        assert ctx.enabled_bricks == frozenset()
        assert ctx.cache_store is None
        assert ctx.mcp_config is None

    def test_with_all_fields(self) -> None:
        ctx = WarmupContext(
            agent_id="agent-1",
            agent_record={"fake": "record"},
            agent_registry={"fake": "registry"},
            namespace_manager="ns_mgr",
            enabled_bricks=frozenset({"search", "pay"}),
            cache_store="cache",
            mcp_config={"servers": []},
        )
        assert ctx.namespace_manager == "ns_mgr"
        assert "search" in ctx.enabled_bricks
        assert ctx.cache_store == "cache"
        assert ctx.mcp_config == {"servers": []}

    def test_frozen(self) -> None:
        ctx = WarmupContext(
            agent_id="agent-1",
            agent_record={"fake": "record"},
            agent_registry={"fake": "registry"},
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            ctx.agent_id = "mutated"


# ---------------------------------------------------------------------------
# STANDARD_WARMUP
# ---------------------------------------------------------------------------


class TestStandardWarmup:
    def test_is_tuple(self) -> None:
        assert isinstance(STANDARD_WARMUP, tuple)

    def test_has_five_steps(self) -> None:
        assert len(STANDARD_WARMUP) == 5

    def test_step_names(self) -> None:
        names = [s.name for s in STANDARD_WARMUP]
        assert names == [
            "load_credentials",
            "mount_namespace",
            "verify_bricks",
            "warm_caches",
            "connect_mcp",
        ]

    def test_optional_steps(self) -> None:
        optional = [s.name for s in STANDARD_WARMUP if not s.required]
        assert set(optional) == {"warm_caches", "connect_mcp"}

    def test_required_steps(self) -> None:
        required = [s.name for s in STANDARD_WARMUP if s.required]
        assert set(required) == {
            "load_credentials",
            "mount_namespace",
            "verify_bricks",
        }

    def test_all_steps_are_frozen(self) -> None:
        for step in STANDARD_WARMUP:
            with pytest.raises(dataclasses.FrozenInstanceError):
                step.name = "mutated"
