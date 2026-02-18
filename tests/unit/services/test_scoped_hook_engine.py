"""Tests for ScopedHookEngine — per-agent scoping + verified execution (Issue #1257).

TDD: RED tests written first, then implementation to make them GREEN.
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.plugins.async_hooks import AsyncHookEngine
from nexus.plugins.hooks import PluginHooks
from nexus.services.hook_engine import ScopedHookEngine
from nexus.services.protocols.hook_engine import (
    POST_WRITE,
    PRE_WRITE,
    HookCapabilities,
    HookContext,
    HookId,
    HookResult,
    HookSpec,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _noop_handler(ctx: HookContext) -> HookResult:
    """No-op hook handler that always proceeds."""
    return HookResult(proceed=True, modified_context=None, error=None)


async def _veto_handler(ctx: HookContext) -> HookResult:
    """Hook handler that vetoes the operation."""
    return HookResult(proceed=False, modified_context=None, error="vetoed")


async def _modify_handler(ctx: HookContext) -> HookResult:
    """Hook handler that modifies context."""
    return HookResult(
        proceed=True,
        modified_context={"path": ctx.path, "modified": True},
        error=None,
    )


async def _slow_handler(ctx: HookContext) -> HookResult:
    """Hook handler that takes forever."""
    await asyncio.sleep(100)
    return HookResult(proceed=True, modified_context=None, error=None)


def _make_engine(*, default_timeout_ms: int = 5000) -> ScopedHookEngine:
    """Create a ScopedHookEngine wrapping fresh PluginHooks."""
    inner = AsyncHookEngine(inner=PluginHooks())
    return ScopedHookEngine(inner=inner, default_timeout_ms=default_timeout_ms)


def _make_context(
    phase: str = PRE_WRITE,
    agent_id: str | None = None,
    path: str = "/test.txt",
) -> HookContext:
    return HookContext(
        phase=phase,
        path=path,
        zone_id="zone1",
        agent_id=agent_id,
        payload={},
    )


# ---------------------------------------------------------------------------
# Agent scope filtering
# ---------------------------------------------------------------------------


class TestAgentScopeFiltering:
    """Verify hooks fire only for their scoped agent."""

    @pytest.mark.asyncio
    async def test_global_hook_fires_for_all_agents(self) -> None:
        """Hook with agent_scope=None fires for any agent_id in context."""
        engine = _make_engine()
        spec = HookSpec(phase=PRE_WRITE, handler_name="global")
        await engine.register_hook(spec, _noop_handler)

        # Fire for agent A
        result_a = await engine.fire(PRE_WRITE, _make_context(agent_id="agent_a"))
        assert result_a.proceed is True

        # Fire for agent B
        result_b = await engine.fire(PRE_WRITE, _make_context(agent_id="agent_b"))
        assert result_b.proceed is True

        # Fire with no agent
        result_none = await engine.fire(PRE_WRITE, _make_context(agent_id=None))
        assert result_none.proceed is True

    @pytest.mark.asyncio
    async def test_agent_scoped_hook_fires_only_for_matching_agent(self) -> None:
        """Hook scoped to agent A should NOT fire for agent B."""
        engine = _make_engine()
        spec = HookSpec(
            phase=PRE_WRITE,
            handler_name="agent_a_only",
            agent_scope="agent_a",
        )
        await engine.register_hook(spec, _veto_handler)

        # Should veto for agent_a
        result_a = await engine.fire(PRE_WRITE, _make_context(agent_id="agent_a"))
        assert result_a.proceed is False

        # Should proceed for agent_b (hook doesn't apply)
        result_b = await engine.fire(PRE_WRITE, _make_context(agent_id="agent_b"))
        assert result_b.proceed is True

    @pytest.mark.asyncio
    async def test_agent_scoped_hook_does_not_fire_for_none_agent(self) -> None:
        """Agent-scoped hook should not fire when context has agent_id=None."""
        engine = _make_engine()
        spec = HookSpec(
            phase=PRE_WRITE,
            handler_name="scoped",
            agent_scope="agent_x",
        )
        await engine.register_hook(spec, _veto_handler)

        result = await engine.fire(PRE_WRITE, _make_context(agent_id=None))
        assert result.proceed is True

    @pytest.mark.asyncio
    async def test_global_and_agent_hooks_both_fire(self) -> None:
        """When both global and agent-scoped hooks exist, both fire for matching agent."""
        engine = _make_engine()

        fired: list[str] = []

        async def _track_global(ctx: HookContext) -> HookResult:
            fired.append("global")
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _track_agent(ctx: HookContext) -> HookResult:
            fired.append("agent")
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(HookSpec(phase=PRE_WRITE, handler_name="g"), _track_global)
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="a", agent_scope="agent_x"),
            _track_agent,
        )

        await engine.fire(PRE_WRITE, _make_context(agent_id="agent_x"))
        assert "global" in fired
        assert "agent" in fired

    @pytest.mark.asyncio
    async def test_multiple_agents_independent_scoping(self) -> None:
        """Hooks for agent A and agent B are independent."""
        engine = _make_engine()

        fired: list[str] = []

        async def _track_a(ctx: HookContext) -> HookResult:
            fired.append("a")
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _track_b(ctx: HookContext) -> HookResult:
            fired.append("b")
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="ha", agent_scope="agent_a"),
            _track_a,
        )
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="hb", agent_scope="agent_b"),
            _track_b,
        )

        fired.clear()
        await engine.fire(PRE_WRITE, _make_context(agent_id="agent_a"))
        assert fired == ["a"]

        fired.clear()
        await engine.fire(PRE_WRITE, _make_context(agent_id="agent_b"))
        assert fired == ["b"]


# ---------------------------------------------------------------------------
# Capability enforcement
# ---------------------------------------------------------------------------


class TestCapabilityEnforcement:
    """Verify ScopedHookEngine enforces declared capabilities."""

    @pytest.mark.asyncio
    async def test_can_veto_false_overrides_veto(self) -> None:
        """Handler declared can_veto=False that returns proceed=False is overridden."""
        engine = _make_engine()
        spec = HookSpec(
            phase=PRE_WRITE,
            handler_name="no_veto",
            capabilities=HookCapabilities(can_veto=False),
        )

        async def _attempts_veto(ctx: HookContext) -> HookResult:
            return HookResult(proceed=False, modified_context=None, error="denied")

        await engine.register_hook(spec, _attempts_veto)

        result = await engine.fire(PRE_WRITE, _make_context())
        # Engine should override: proceed=True because can_veto=False
        assert result.proceed is True

    @pytest.mark.asyncio
    async def test_can_modify_false_ignores_modification(self) -> None:
        """Handler declared can_modify_context=False: modification is ignored."""
        engine = _make_engine()
        spec = HookSpec(
            phase=PRE_WRITE,
            handler_name="no_modify",
            capabilities=HookCapabilities(can_modify_context=False),
        )
        await engine.register_hook(spec, _modify_handler)

        result = await engine.fire(PRE_WRITE, _make_context())
        assert result.proceed is True
        # modified_context should be stripped
        assert result.modified_context is None

    @pytest.mark.asyncio
    async def test_timeout_enforcement_skips_slow_handler(self) -> None:
        """Handler exceeding max_timeout_ms is skipped (fail-safe)."""
        engine = _make_engine()
        spec = HookSpec(
            phase=PRE_WRITE,
            handler_name="slow",
            capabilities=HookCapabilities(max_timeout_ms=50),
        )
        await engine.register_hook(spec, _slow_handler)

        result = await engine.fire(PRE_WRITE, _make_context())
        # Should proceed (slow handler timed out, treated as noop)
        assert result.proceed is True

    @pytest.mark.asyncio
    async def test_default_capabilities_allow_everything(self) -> None:
        """Default HookCapabilities allow veto and modify."""
        engine = _make_engine()
        spec = HookSpec(phase=PRE_WRITE, handler_name="default_caps")
        await engine.register_hook(spec, _veto_handler)

        result = await engine.fire(PRE_WRITE, _make_context())
        assert result.proceed is False
        assert result.error == "vetoed"


# ---------------------------------------------------------------------------
# Concurrent POST hooks
# ---------------------------------------------------------------------------


class TestConcurrentPostHooks:
    """Verify POST hooks run concurrently while PRE hooks run sequentially."""

    @pytest.mark.asyncio
    async def test_pre_hooks_sequential_respects_priority(self) -> None:
        """PRE hooks execute in priority order (higher first)."""
        engine = _make_engine()
        order: list[int] = []

        async def _p10(ctx: HookContext) -> HookResult:
            order.append(10)
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _p0(ctx: HookContext) -> HookResult:
            order.append(0)
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(HookSpec(phase=PRE_WRITE, handler_name="low", priority=0), _p0)
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="high", priority=10), _p10
        )

        await engine.fire(PRE_WRITE, _make_context())
        assert order == [10, 0]

    @pytest.mark.asyncio
    async def test_post_hooks_run_concurrently(self) -> None:
        """POST hooks should run concurrently (not blocked by each other)."""
        engine = _make_engine()
        entered = asyncio.Event()
        both_entered = asyncio.Event()
        count = 0

        async def _concurrent_a(ctx: HookContext) -> HookResult:
            nonlocal count
            count += 1
            if count >= 2:
                both_entered.set()
            entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=2.0)
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _concurrent_b(ctx: HookContext) -> HookResult:
            nonlocal count
            count += 1
            if count >= 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=2.0)
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(HookSpec(phase=POST_WRITE, handler_name="a"), _concurrent_a)
        await engine.register_hook(HookSpec(phase=POST_WRITE, handler_name="b"), _concurrent_b)

        result = await engine.fire(POST_WRITE, _make_context(phase=POST_WRITE))
        assert result.proceed is True
        assert both_entered.is_set()


# ---------------------------------------------------------------------------
# Unregistration
# ---------------------------------------------------------------------------


class TestUnregistration:
    """Verify hook unregistration."""

    @pytest.mark.asyncio
    async def test_unregister_removes_hook(self) -> None:
        engine = _make_engine()
        hook_id = await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="v"), _veto_handler
        )

        # Should veto
        result = await engine.fire(PRE_WRITE, _make_context())
        assert result.proceed is False

        # Unregister
        removed = await engine.unregister_hook(hook_id)
        assert removed is True

        # Should proceed now
        result = await engine.fire(PRE_WRITE, _make_context())
        assert result.proceed is True

    @pytest.mark.asyncio
    async def test_unregister_nonexistent_returns_false(self) -> None:
        engine = _make_engine()
        result = await engine.unregister_hook(HookId(id="nonexistent"))
        assert result is False

    @pytest.mark.asyncio
    async def test_unregister_agent_scoped_hook(self) -> None:
        engine = _make_engine()
        hook_id = await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="agent_hook",
                agent_scope="agent_a",
            ),
            _veto_handler,
        )

        # Should veto for agent_a
        result = await engine.fire(PRE_WRITE, _make_context(agent_id="agent_a"))
        assert result.proceed is False

        # Unregister
        await engine.unregister_hook(hook_id)

        # Should proceed now
        result = await engine.fire(PRE_WRITE, _make_context(agent_id="agent_a"))
        assert result.proceed is True


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Verify ScopedHookEngine satisfies HookEngineProtocol."""

    def test_isinstance_check(self) -> None:
        from nexus.services.protocols.hook_engine import HookEngineProtocol

        engine = _make_engine()
        assert isinstance(engine, HookEngineProtocol)


# ---------------------------------------------------------------------------
# Dual-index O(1) lookup
# ---------------------------------------------------------------------------


class TestDualIndex:
    """Verify the dual-index implementation returns correct hooks."""

    @pytest.mark.asyncio
    async def test_empty_engine_returns_proceed(self) -> None:
        engine = _make_engine()
        result = await engine.fire(PRE_WRITE, _make_context())
        assert result.proceed is True

    @pytest.mark.asyncio
    async def test_many_hooks_different_phases(self) -> None:
        """Hooks for different phases don't interfere."""
        engine = _make_engine()
        await engine.register_hook(HookSpec(phase=PRE_WRITE, handler_name="w"), _veto_handler)
        await engine.register_hook(HookSpec(phase=POST_WRITE, handler_name="r"), _noop_handler)

        # PRE_WRITE should be vetoed
        result = await engine.fire(PRE_WRITE, _make_context())
        assert result.proceed is False

        # POST_WRITE should proceed
        result = await engine.fire(POST_WRITE, _make_context(phase=POST_WRITE))
        assert result.proceed is True


# ---------------------------------------------------------------------------
# Agent lifecycle cleanup
# ---------------------------------------------------------------------------


class TestAgentLifecycleCleanup:
    """Verify cleanup_agent_hooks removes all hooks for a disconnected agent."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_all_hooks_for_agent(self) -> None:
        """cleanup_agent_hooks removes all hooks scoped to the given agent."""
        engine = _make_engine()

        # Register 2 hooks for agent_a, 1 for agent_b
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="a1", agent_scope="agent_a"),
            _veto_handler,
        )
        await engine.register_hook(
            HookSpec(phase=POST_WRITE, handler_name="a2", agent_scope="agent_a"),
            _noop_handler,
        )
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="b1", agent_scope="agent_b"),
            _veto_handler,
        )

        removed = await engine.cleanup_agent_hooks("agent_a")
        assert removed == 2

        # agent_a hooks should no longer fire
        result = await engine.fire(PRE_WRITE, _make_context(agent_id="agent_a"))
        assert result.proceed is True

        # agent_b hooks should still fire
        result = await engine.fire(PRE_WRITE, _make_context(agent_id="agent_b"))
        assert result.proceed is False

    @pytest.mark.asyncio
    async def test_cleanup_nonexistent_agent_returns_zero(self) -> None:
        engine = _make_engine()
        removed = await engine.cleanup_agent_hooks("no_such_agent")
        assert removed == 0

    @pytest.mark.asyncio
    async def test_cleanup_does_not_affect_global_hooks(self) -> None:
        """Global hooks are untouched when cleaning up an agent."""
        engine = _make_engine()

        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="global_v"), _veto_handler
        )
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="scoped_v", agent_scope="agent_x"),
            _noop_handler,
        )

        await engine.cleanup_agent_hooks("agent_x")

        # Global veto hook still fires
        result = await engine.fire(PRE_WRITE, _make_context(agent_id=None))
        assert result.proceed is False

    @pytest.mark.asyncio
    async def test_state_event_handler_triggers_cleanup(self) -> None:
        """Integration: AgentStateEvent for IDLE triggers cleanup."""
        from nexus.scheduler.events import AgentStateEmitter, AgentStateEvent
        from nexus.services.hook_engine import create_agent_cleanup_handler

        engine = _make_engine()
        emitter = AgentStateEmitter()
        emitter.add_handler(create_agent_cleanup_handler(engine))

        # Register agent-scoped hook
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="will_die", agent_scope="agent_z"),
            _veto_handler,
        )

        # Emit IDLE event
        event = AgentStateEvent(
            agent_id="agent_z",
            previous_state="CONNECTED",
            new_state="IDLE",
        )
        await emitter.emit(event)

        # Hook should be cleaned up
        result = await engine.fire(PRE_WRITE, _make_context(agent_id="agent_z"))
        assert result.proceed is True

    @pytest.mark.asyncio
    async def test_state_event_handler_ignores_connected(self) -> None:
        """Cleanup handler ignores transitions TO CONNECTED."""
        from nexus.scheduler.events import AgentStateEmitter, AgentStateEvent
        from nexus.services.hook_engine import create_agent_cleanup_handler

        engine = _make_engine()
        emitter = AgentStateEmitter()
        emitter.add_handler(create_agent_cleanup_handler(engine))

        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="stays", agent_scope="agent_q"),
            _veto_handler,
        )

        # CONNECTED event should NOT trigger cleanup
        event = AgentStateEvent(
            agent_id="agent_q",
            previous_state="IDLE",
            new_state="CONNECTED",
        )
        await emitter.emit(event)

        # Hook should still be there
        result = await engine.fire(PRE_WRITE, _make_context(agent_id="agent_q"))
        assert result.proceed is False
