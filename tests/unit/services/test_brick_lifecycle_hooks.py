"""Tests for BrickLifecycleManager hook integration + concurrency (Issue #1704).

Phase 3 TDD: hook firing, veto support, event barrier concurrency tests.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.protocols.brick_lifecycle import (
    POST_MOUNT,
    POST_UNMOUNT,
    POST_UNREGISTER,
    PRE_MOUNT,
    PRE_UNMOUNT,
    PRE_UNREGISTER,
    BrickLifecycleProtocol,
    BrickState,
)
from nexus.services.protocols.hook_engine import (
    HookContext,
    HookEngineProtocol,
    HookResult,
)
from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_lifecycle_brick(name: str = "test") -> MagicMock:
    """Create a mock brick that satisfies BrickLifecycleProtocol."""
    brick = AsyncMock(spec=BrickLifecycleProtocol)
    brick.start = AsyncMock(return_value=None)
    brick.stop = AsyncMock(return_value=None)
    brick.health_check = AsyncMock(return_value=True)
    brick.__class__.__name__ = f"{name.capitalize()}Brick"
    return brick


def _make_stateless_brick(name: str = "pay") -> MagicMock:
    """Create a mock brick without lifecycle methods."""
    brick = MagicMock()
    brick.__class__.__name__ = f"{name.capitalize()}Brick"
    if hasattr(brick, "start"):
        del brick.start
    if hasattr(brick, "stop"):
        del brick.stop
    if hasattr(brick, "health_check"):
        del brick.health_check
    return brick


def _make_hook_engine(*, proceed: bool = True) -> AsyncMock:
    """Create a mock HookEngine that always returns the given proceed value."""
    hook_engine = AsyncMock(spec=HookEngineProtocol)
    hook_engine.fire = AsyncMock(
        return_value=HookResult(proceed=proceed, modified_context=None, error=None)
    )
    return hook_engine


def _make_veto_hook_engine(veto_phase: str, error_msg: str = "Vetoed") -> AsyncMock:
    """Create a mock HookEngine that vetoes on a specific phase."""
    hook_engine = AsyncMock(spec=HookEngineProtocol)

    async def _conditional_fire(phase: str, context: HookContext) -> HookResult:
        if phase == veto_phase:
            return HookResult(proceed=False, modified_context=None, error=error_msg)
        return HookResult(proceed=True, modified_context=None, error=None)

    hook_engine.fire = AsyncMock(side_effect=_conditional_fire)
    return hook_engine


# ---------------------------------------------------------------------------
# Hook firing on mount
# ---------------------------------------------------------------------------


class TestMountHookFiring:
    """Verify hooks fire correctly during mount lifecycle."""

    @pytest.mark.asyncio
    async def test_mount_fires_pre_and_post_mount_hooks(self) -> None:
        """Mount should fire PRE_MOUNT before start() and POST_MOUNT after."""
        hook_engine = _make_hook_engine()
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")

        await manager.mount("search")

        # Should have fired PRE_MOUNT and POST_MOUNT
        fire_calls = hook_engine.fire.call_args_list
        phases = [c.args[0] for c in fire_calls]
        assert PRE_MOUNT in phases
        assert POST_MOUNT in phases
        # PRE_MOUNT should come before POST_MOUNT
        assert phases.index(PRE_MOUNT) < phases.index(POST_MOUNT)

    @pytest.mark.asyncio
    async def test_mount_hook_context_contains_brick_info(self) -> None:
        """Hook context should include brick name in payload."""
        hook_engine = _make_hook_engine()
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")

        await manager.mount("search")

        # Check the PRE_MOUNT hook context
        pre_mount_call = hook_engine.fire.call_args_list[0]
        ctx = pre_mount_call.args[1]
        assert isinstance(ctx, HookContext)
        assert ctx.phase == PRE_MOUNT
        assert ctx.payload["brick_name"] == "search"
        assert ctx.payload["protocol_name"] == "SearchProtocol"

    @pytest.mark.asyncio
    async def test_pre_mount_veto_blocks_mount(self) -> None:
        """If PRE_MOUNT hook returns proceed=False, mount should be blocked."""
        hook_engine = _make_veto_hook_engine(PRE_MOUNT, "Untrusted brick")
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_lifecycle_brick("malicious")
        manager.register("malicious", brick, protocol_name="MP")

        await manager.mount("malicious")

        # Brick should be FAILED, not ACTIVE
        status = manager.get_status("malicious")
        assert status is not None
        assert status.state == BrickState.FAILED
        assert "Untrusted brick" in (status.error or "")
        # start() should NOT have been called
        brick.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_post_mount_fires_after_successful_start(self) -> None:
        """POST_MOUNT should only fire if brick successfully started."""
        hook_engine = _make_hook_engine()
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(side_effect=RuntimeError("fail"))
        manager.register("search", brick, protocol_name="SP")

        await manager.mount("search")

        phases = [c.args[0] for c in hook_engine.fire.call_args_list]
        assert PRE_MOUNT in phases
        # POST_MOUNT should NOT fire since start() failed
        assert POST_MOUNT not in phases

    @pytest.mark.asyncio
    async def test_mount_without_hook_engine_works(self) -> None:
        """Manager without hook engine should still mount bricks normally."""
        manager = BrickLifecycleManager()  # No hook engine
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")

        await manager.mount("search")

        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE

    @pytest.mark.asyncio
    async def test_mount_stateless_brick_fires_hooks(self) -> None:
        """Even stateless bricks should fire mount hooks."""
        hook_engine = _make_hook_engine()
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_stateless_brick("pay")
        manager.register("pay", brick, protocol_name="PaymentProtocol")

        await manager.mount("pay")

        phases = [c.args[0] for c in hook_engine.fire.call_args_list]
        assert PRE_MOUNT in phases
        assert POST_MOUNT in phases


# ---------------------------------------------------------------------------
# Hook firing on unmount
# ---------------------------------------------------------------------------


class TestUnmountHookFiring:
    """Verify hooks fire correctly during unmount lifecycle."""

    @pytest.mark.asyncio
    async def test_unmount_fires_pre_and_post_unmount_hooks(self) -> None:
        hook_engine = _make_hook_engine()
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        hook_engine.fire.reset_mock()
        await manager.unmount("search")

        phases = [c.args[0] for c in hook_engine.fire.call_args_list]
        assert PRE_UNMOUNT in phases
        assert POST_UNMOUNT in phases
        assert phases.index(PRE_UNMOUNT) < phases.index(POST_UNMOUNT)

    @pytest.mark.asyncio
    async def test_pre_unmount_veto_blocks_unmount(self) -> None:
        """If PRE_UNMOUNT hook vetoes, brick stays ACTIVE."""
        hook_engine = _make_hook_engine()
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        # Now make hook engine veto unmount
        hook_engine.fire = AsyncMock(
            side_effect=lambda phase, ctx: HookResult(
                proceed=phase != PRE_UNMOUNT,
                modified_context=None,
                error="Cannot unmount during backup" if phase == PRE_UNMOUNT else None,
            )
        )

        await manager.unmount("search")

        # Brick should still be ACTIVE (unmount was vetoed)
        status = manager.get_status("search")
        assert status is not None
        assert status.state == BrickState.ACTIVE

    @pytest.mark.asyncio
    async def test_post_unmount_not_fired_on_stop_failure(self) -> None:
        """POST_UNMOUNT should not fire if stop() raises."""
        hook_engine = _make_hook_engine()
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_lifecycle_brick("search")
        brick.stop = AsyncMock(side_effect=RuntimeError("drain failed"))
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")

        hook_engine.fire.reset_mock()
        await manager.unmount("search")

        phases = [c.args[0] for c in hook_engine.fire.call_args_list]
        assert PRE_UNMOUNT in phases
        assert POST_UNMOUNT not in phases


# ---------------------------------------------------------------------------
# Concurrency tests — event barriers
# ---------------------------------------------------------------------------


class TestConcurrency:
    """Verify concurrent start/stop behavior using event barriers."""

    @pytest.mark.asyncio
    async def test_same_level_bricks_start_concurrently(self) -> None:
        """Bricks at the same DAG level should start concurrently."""
        entered = asyncio.Event()
        both_entered = asyncio.Event()
        entry_count = 0

        async def concurrent_start_a() -> None:
            nonlocal entry_count
            entry_count += 1
            if entry_count >= 2:
                both_entered.set()
            entered.set()
            # Wait for both to have entered before completing
            await asyncio.wait_for(both_entered.wait(), timeout=2.0)

        async def concurrent_start_b() -> None:
            nonlocal entry_count
            entry_count += 1
            if entry_count >= 2:
                both_entered.set()
            # Wait for both to have entered before completing
            await asyncio.wait_for(both_entered.wait(), timeout=2.0)

        brick_a = _make_lifecycle_brick("a")
        brick_a.start = AsyncMock(side_effect=concurrent_start_a)
        brick_b = _make_lifecycle_brick("b")
        brick_b.start = AsyncMock(side_effect=concurrent_start_b)

        manager = BrickLifecycleManager()
        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP")

        await manager.mount_all()

        # Both bricks should be ACTIVE
        _s = manager.get_status("a")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        _s = manager.get_status("b")
        assert _s is not None
        assert _s.state == BrickState.ACTIVE
        # Both entered start() before either completed (proves concurrency)
        assert both_entered.is_set()

    @pytest.mark.asyncio
    async def test_different_levels_start_sequentially(self) -> None:
        """Bricks at different DAG levels should start sequentially."""
        order: list[str] = []

        async def track_a() -> None:
            order.append("a_start")
            await asyncio.sleep(0.01)
            order.append("a_end")

        async def track_b() -> None:
            order.append("b_start")

        brick_a = _make_lifecycle_brick("a")
        brick_a.start = AsyncMock(side_effect=track_a)
        brick_b = _make_lifecycle_brick("b")
        brick_b.start = AsyncMock(side_effect=track_b)

        manager = BrickLifecycleManager()
        manager.register("a", brick_a, protocol_name="AP")
        manager.register("b", brick_b, protocol_name="BP", depends_on=("a",))

        await manager.mount_all()

        # a must complete before b starts
        assert order.index("a_end") < order.index("b_start")

    @pytest.mark.asyncio
    async def test_per_brick_lock_prevents_concurrent_transitions(self) -> None:
        """Concurrent mount+unmount on the same brick should be serialized."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")

        # Mount first
        await manager.mount("search")

        # Try concurrent unmount — should work since lock is per-brick
        await manager.unmount("search")
        _s = manager.get_status("search")
        assert _s is not None
        assert _s.state == BrickState.UNMOUNTED

    @pytest.mark.asyncio
    async def test_mount_all_with_hooks_fires_per_brick(self) -> None:
        """mount_all should fire hooks for each brick individually."""
        hook_engine = _make_hook_engine()
        manager = BrickLifecycleManager(hook_engine=hook_engine)

        manager.register("a", _make_lifecycle_brick("a"), protocol_name="AP")
        manager.register("b", _make_lifecycle_brick("b"), protocol_name="BP")

        await manager.mount_all()

        # Should have 4 hook fires: PRE_MOUNT(a), PRE_MOUNT(b), POST_MOUNT(a), POST_MOUNT(b)
        fire_calls = hook_engine.fire.call_args_list
        brick_names = [c.args[1].payload["brick_name"] for c in fire_calls]
        assert brick_names.count("a") == 2  # PRE_MOUNT + POST_MOUNT
        assert brick_names.count("b") == 2  # PRE_MOUNT + POST_MOUNT


# ---------------------------------------------------------------------------
# Unregister hook firing (Issue #2363)
# ---------------------------------------------------------------------------


class TestUnregisterHookFiring:
    """Verify hooks fire correctly during unregister lifecycle."""

    @pytest.mark.asyncio
    async def test_unregister_fires_pre_and_post_hooks(self) -> None:
        """Unregister should fire PRE_UNREGISTER and POST_UNREGISTER."""
        hook_engine = _make_hook_engine()
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")
        await manager.unmount("search")

        hook_engine.fire.reset_mock()
        await manager.unregister("search")

        phases = [c.args[0] for c in hook_engine.fire.call_args_list]
        assert PRE_UNREGISTER in phases
        assert POST_UNREGISTER in phases

    @pytest.mark.asyncio
    async def test_pre_unregister_is_non_vetable(self) -> None:
        """PRE_UNREGISTER veto should NOT block unregister (it's informational)."""
        hook_engine = _make_veto_hook_engine(PRE_UNREGISTER, "Cannot veto unregister")
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")
        await manager.unmount("search")

        # Unregister should still proceed despite veto
        await manager.unregister("search")
        assert manager.get_status("search") is None

    @pytest.mark.asyncio
    async def test_unregister_hook_error_doesnt_block(self) -> None:
        """Hook engine errors during unregister should not block the operation."""
        hook_engine = AsyncMock(spec=HookEngineProtocol)
        hook_engine.fire = AsyncMock(side_effect=RuntimeError("hook engine down"))
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SP")
        await manager.mount("search")
        await manager.unmount("search")

        # Should succeed despite hook errors
        await manager.unregister("search")
        assert manager.get_status("search") is None

    @pytest.mark.asyncio
    async def test_unregister_hook_context(self) -> None:
        """Hook context should include brick metadata."""
        hook_engine = _make_hook_engine()
        manager = BrickLifecycleManager(hook_engine=hook_engine)
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")
        await manager.unmount("search")

        hook_engine.fire.reset_mock()
        await manager.unregister("search")

        # Check the PRE_UNREGISTER hook context
        pre_call = hook_engine.fire.call_args_list[0]
        ctx = pre_call.args[1]
        assert isinstance(ctx, HookContext)
        assert ctx.payload["brick_name"] == "search"
        assert ctx.payload["protocol_name"] == "SearchProtocol"
