"""Hypothesis property-based tests for HookEngine protocol invariants (Issue #1303).

Tests against an in-memory stub to validate the protocol contract.

Invariants proven:
  1. Registration returns unique HookId
  2. Unregistration is idempotent: unregister(id) twice = second returns False
  3. Fire always returns a valid HookResult
  4. Registered hooks are discoverable (fire calls them)
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from hypothesis import given, settings
from hypothesis import strategies as st

from nexus.services.protocols.hook_engine import (
    HookContext,
    HookEngineProtocol,
    HookId,
    HookResult,
    HookSpec,
)
from tests.strategies.kernel import hook_context, hook_spec

# ---------------------------------------------------------------------------
# In-memory HookEngine stub (protocol conformance target)
# ---------------------------------------------------------------------------


class InMemoryHookEngine:
    """Minimal HookEngine implementation for protocol invariant testing.

    Not production-grade — purely for validating the HookEngineProtocol contract.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, tuple[HookSpec, Callable[..., Awaitable[HookResult]]]] = {}

    async def register_hook(
        self,
        spec: HookSpec,
        handler: Callable[..., Awaitable[HookResult]],
    ) -> HookId:
        hook_id = HookId(id=str(uuid.uuid4()))
        self._hooks[hook_id.id] = (spec, handler)
        return hook_id

    async def unregister_hook(self, hook_id: HookId) -> bool:
        if hook_id.id in self._hooks:
            del self._hooks[hook_id.id]
            return True
        return False

    async def fire(self, phase: str, context: HookContext) -> HookResult:
        # Collect matching hooks, sorted by priority (higher first)
        matching = [
            (spec, handler) for spec, handler in self._hooks.values() if spec.phase == phase
        ]
        matching.sort(key=lambda x: x[0].priority, reverse=True)

        for _spec, handler in matching:
            result = await handler(context)
            if not result.proceed:
                return result

        return HookResult(proceed=True, modified_context=None, error=None)


# Verify protocol conformance at import time
assert isinstance(InMemoryHookEngine(), HookEngineProtocol)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously for Hypothesis compatibility."""
    return asyncio.run(coro)


async def _noop_handler(ctx: HookContext) -> HookResult:
    """No-op hook handler that always proceeds."""
    return HookResult(proceed=True, modified_context=None, error=None)


async def _veto_handler(ctx: HookContext) -> HookResult:
    """Hook handler that vetoes the operation."""
    return HookResult(proceed=False, modified_context=None, error="vetoed")


# ---------------------------------------------------------------------------
# Invariant 1: Registration returns unique HookId
# ---------------------------------------------------------------------------


class TestHookRegistrationInvariants:
    """Hook registration properties."""

    @given(specs=st.lists(hook_spec(), min_size=2, max_size=20))
    @settings(deadline=None)
    def test_registration_returns_unique_ids(self, specs: list[HookSpec]) -> None:
        """Each register_hook call returns a unique HookId."""

        async def _inner():
            engine = InMemoryHookEngine()
            ids = set()
            for spec in specs:
                hook_id = await engine.register_hook(spec, _noop_handler)
                assert hook_id.id not in ids, f"Duplicate hook ID: {hook_id.id}"
                ids.add(hook_id.id)

        _run(_inner())


# ---------------------------------------------------------------------------
# Invariant 2: Unregistration idempotency
# ---------------------------------------------------------------------------


class TestHookUnregistrationInvariants:
    """Hook unregistration properties."""

    @given(spec=hook_spec())
    @settings(deadline=None)
    def test_unregister_twice_returns_false_second_time(self, spec: HookSpec) -> None:
        """Unregistering the same hook twice: first True, second False."""

        async def _inner():
            engine = InMemoryHookEngine()
            hook_id = await engine.register_hook(spec, _noop_handler)

            first = await engine.unregister_hook(hook_id)
            assert first is True

            second = await engine.unregister_hook(hook_id)
            assert second is False

        _run(_inner())

    @given(specs=st.lists(hook_spec(), min_size=1, max_size=10))
    @settings(deadline=None)
    def test_unregister_nonexistent_returns_false(self, specs: list[HookSpec]) -> None:
        """Unregistering an ID that was never registered returns False."""

        async def _inner():
            engine = InMemoryHookEngine()
            for spec in specs:
                await engine.register_hook(spec, _noop_handler)

            fake_id = HookId(id="nonexistent_" + str(uuid.uuid4()))
            assert await engine.unregister_hook(fake_id) is False

        _run(_inner())


# ---------------------------------------------------------------------------
# Invariant 3: Fire always returns valid HookResult
# ---------------------------------------------------------------------------


class TestHookFireInvariants:
    """Hook fire properties."""

    @given(ctx=hook_context())
    @settings(deadline=None)
    def test_fire_with_no_hooks_proceeds(self, ctx: HookContext) -> None:
        """Firing with no hooks registered always returns proceed=True."""

        async def _inner():
            engine = InMemoryHookEngine()
            result = await engine.fire(ctx.phase, ctx)
            assert result.proceed is True
            assert result.error is None

        _run(_inner())

    @given(
        specs=st.lists(hook_spec(), min_size=1, max_size=10),
        ctx=hook_context(),
    )
    @settings(deadline=None)
    def test_fire_with_noop_hooks_proceeds(self, specs: list[HookSpec], ctx: HookContext) -> None:
        """Firing with only no-op hooks always returns proceed=True."""

        async def _inner():
            engine = InMemoryHookEngine()
            for spec in specs:
                await engine.register_hook(spec, _noop_handler)

            result = await engine.fire(ctx.phase, ctx)
            assert result.proceed is True

        _run(_inner())

    @given(ctx=hook_context())
    @settings(deadline=None)
    def test_fire_with_veto_hook_stops(self, ctx: HookContext) -> None:
        """A veto hook causes fire to return proceed=False."""

        async def _inner():
            engine = InMemoryHookEngine()
            spec = HookSpec(phase=ctx.phase, handler_name="veto", priority=0)
            await engine.register_hook(spec, _veto_handler)

            result = await engine.fire(ctx.phase, ctx)
            assert result.proceed is False
            assert result.error is not None

        _run(_inner())


# ---------------------------------------------------------------------------
# Invariant 4: Unregistered hooks are not fired
# ---------------------------------------------------------------------------


class TestHookLifecycleInvariants:
    """Hook lifecycle properties."""

    @given(ctx=hook_context())
    @settings(deadline=None)
    def test_unregistered_hook_not_fired(self, ctx: HookContext) -> None:
        """After unregistering a veto hook, fire should proceed."""

        async def _inner():
            engine = InMemoryHookEngine()
            spec = HookSpec(phase=ctx.phase, handler_name="veto", priority=0)
            hook_id = await engine.register_hook(spec, _veto_handler)

            # Verify it vetoes
            result = await engine.fire(ctx.phase, ctx)
            assert result.proceed is False

            # Unregister and verify it no longer vetoes
            await engine.unregister_hook(hook_id)
            result = await engine.fire(ctx.phase, ctx)
            assert result.proceed is True

        _run(_inner())
