"""Unit tests for Rust-dispatched post hooks and async OBSERVE dispatch.

Tests:
- POST hook dispatch via Rust dispatch_post_hooks (sync, fire-and-forget)
- Async OBSERVE dispatch with ObserverRegistry + event_mask filtering
- Hybrid inline/deferred OBSERVE dispatch (Issue #3391)
- Background task lifecycle (tracking, exception logging, shutdown)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.contracts.vfs_hooks import WriteHookContext
from nexus.core.file_events import ALL_FILE_EVENTS, FILE_EVENT_BIT, FileEvent, FileEventType
from nexus.core.nexus_fs_dispatch import DispatchMixin


class _TestDispatch(DispatchMixin):
    def __init__(self):
        from nexus_kernel import Kernel

        self._kernel = Kernel()
        self._init_dispatch()


@pytest.fixture()
def dispatch() -> _TestDispatch:
    return _TestDispatch()


def _make_sync_hook(*, name: str = "sync_hook", side_effect: Exception | None = None):
    hook = MagicMock()
    hook.name = name
    hook.TRIE_PATTERN = None
    if side_effect:
        hook.on_post_write.side_effect = side_effect
    return hook


def _write_ctx(**kwargs) -> WriteHookContext:
    defaults = {
        "path": "/test",
        "content": b"data",
        "context": None,
        "zone_id": "z1",
        "agent_id": None,
        "is_new_file": True,
        "content_hash": "abc",
        "metadata": None,
        "old_metadata": None,
        "new_version": 1,
    }
    defaults.update(kwargs)
    return WriteHookContext(**defaults)


class TestSyncPostHooks:
    """Post-hooks dispatched via Rust dispatch_post_hooks (all sync, fire-and-forget)."""

    def test_sync_hook_called(self, dispatch: _TestDispatch) -> None:
        hook = _make_sync_hook()
        dispatch.register_intercept_write(hook)
        ctx = _write_ctx()
        dispatch._kernel.dispatch_post_hooks("write", ctx)
        hook.on_post_write.assert_called_once_with(ctx)

    def test_sync_hook_fault_isolation(self, dispatch: _TestDispatch) -> None:
        """Bad hook failure is fire-and-forget — good hook still called."""
        bad = _make_sync_hook(name="bad", side_effect=RuntimeError("boom"))
        good = _make_sync_hook(name="good")
        dispatch.register_intercept_write(bad)
        dispatch.register_intercept_write(good)

        ctx = _write_ctx()
        # Rust dispatch is fire-and-forget — exceptions don't propagate
        dispatch._kernel.dispatch_post_hooks("write", ctx)
        good.on_post_write.assert_called_once()

    def test_no_hooks_noop(self, dispatch: _TestDispatch) -> None:
        """dispatch_post_hooks with no hooks registered is a no-op."""
        ctx = _write_ctx()
        dispatch._kernel.dispatch_post_hooks("write", ctx)  # should not raise


# ── Async OBSERVE dispatch tests (Issue #1748, #1812) ─────────────────


def _make_async_observer(
    *,
    name: str = "TestObserver",
    event_mask: int = ALL_FILE_EVENTS,
    side_effect: Exception | None = None,
    delay: float = 0.0,
    observe_inline: bool = True,
):
    """Create a sync observer with event_mask.

    §11 Phase 2 deleted OBSERVE_INLINE — all observers are fire-and-forget
    by definition. The ``observe_inline`` parameter is accepted but ignored
    for backward compatibility with existing test call sites. ``delay`` is
    also ignored (was used for async sleep simulation).
    """

    class _Obs:
        pass

    obs = _Obs()
    obs.__class__.__name__ = name
    obs.event_mask = event_mask

    calls: list = []

    def _on_mutation(event):
        if side_effect:
            raise side_effect
        calls.append(event)

    obs.on_mutation = _on_mutation
    obs._calls = calls
    return obs


class TestAsyncObserveDispatch:
    """Tests for the async OBSERVE phase with ObserverRegistry."""

    async def test_observer_called(self, dispatch: _TestDispatch) -> None:
        obs = _make_async_observer()
        dispatch.register_observe(obs)
        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test")
        await dispatch.notify(event)
        assert len(obs._calls) == 1
        assert obs._calls[0] is event

    async def test_event_mask_filtering(self, dispatch: _TestDispatch) -> None:
        """CAS observer with WRITE|DELETE mask should NOT fire for DIR_CREATE."""
        cas_obs = _make_async_observer(
            name="CAS",
            event_mask=FILE_EVENT_BIT[FileEventType.FILE_WRITE]
            | FILE_EVENT_BIT[FileEventType.FILE_DELETE],
        )
        dispatch.register_observe(cas_obs)
        event = FileEvent(type=FileEventType.DIR_CREATE, path="/mydir")
        await dispatch.notify(event)
        assert len(cas_obs._calls) == 0

    async def test_event_mask_allows_matching_events(self, dispatch: _TestDispatch) -> None:
        cas_obs = _make_async_observer(
            name="CAS",
            event_mask=FILE_EVENT_BIT[FileEventType.FILE_WRITE]
            | FILE_EVENT_BIT[FileEventType.FILE_DELETE],
        )
        dispatch.register_observe(cas_obs)
        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt")
        await dispatch.notify(event)
        assert len(cas_obs._calls) == 1

    async def test_fault_isolation(self, dispatch: _TestDispatch) -> None:
        """One observer raising should not prevent others from firing."""
        bad = _make_async_observer(name="Bad", side_effect=RuntimeError("kaboom"))
        good = _make_async_observer(name="Good")
        dispatch.register_observe(bad)
        dispatch.register_observe(good)

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test")
        await dispatch.notify(event)
        assert len(good._calls) == 1

    async def test_concurrent_execution(self, dispatch: _TestDispatch) -> None:
        """Multiple observers sleeping should run in parallel (gather), not serial."""
        import time

        a = _make_async_observer(name="A", delay=0.1)
        b = _make_async_observer(name="B", delay=0.1)
        dispatch.register_observe(a)
        dispatch.register_observe(b)

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test")
        t0 = time.monotonic()
        await dispatch.notify(event)
        elapsed = time.monotonic() - t0

        assert len(a._calls) == 1
        assert len(b._calls) == 1
        assert elapsed < 0.18, f"Expected parallel (~0.1s), got {elapsed:.3f}s"

    async def test_unregister(self, dispatch: _TestDispatch) -> None:
        obs = _make_async_observer()
        dispatch.register_observe(obs)
        assert dispatch.observer_count == 1
        removed = dispatch.unregister_observe(obs)
        assert removed is True
        assert dispatch.observer_count == 0

    async def test_observer_count(self, dispatch: _TestDispatch) -> None:
        a = _make_async_observer(name="A")
        b = _make_async_observer(name="B")
        dispatch.register_observe(a)
        dispatch.register_observe(b)
        assert dispatch.observer_count == 2


# ── OBSERVE dispatch tests (§11 Phase 6: fire-and-forget, no inline/deferred split) ──


def _write_event(path: str = "/test") -> FileEvent:
    return FileEvent(type=FileEventType.FILE_WRITE, path=path)


class TestObserverDispatch:
    """OBSERVE-phase: all observers fire synchronously in notify(), fire-and-forget.

    §11 Phase 2 deleted OBSERVE_INLINE — there is no inline/deferred split.
    All observers are called via ``obs.on_mutation(event)`` (sync) in a
    single loop. Observers needing background I/O schedule their own
    async work internally (e.g. EventBusObserver uses ``create_task``).
    """

    async def test_observer_called_after_notify(self, dispatch: _TestDispatch) -> None:
        obs = _make_async_observer(name="Sync")
        dispatch.register_observe(obs)
        await dispatch.notify(_write_event())
        assert len(obs._calls) == 1

    async def test_multiple_observers_all_called(self, dispatch: _TestDispatch) -> None:
        a = _make_async_observer(name="A")
        b = _make_async_observer(name="B")
        dispatch.register_observe(a)
        dispatch.register_observe(b)
        await dispatch.notify(_write_event())
        assert len(a._calls) == 1
        assert len(b._calls) == 1

    async def test_fault_isolation(self, dispatch: _TestDispatch) -> None:
        bad = _make_async_observer(name="Bad", side_effect=RuntimeError("boom"))
        good = _make_async_observer(name="Good")
        dispatch.register_observe(bad)
        dispatch.register_observe(good)
        await dispatch.notify(_write_event())
        assert len(good._calls) == 1

    async def test_deferred_fault_isolation(self, dispatch: _TestDispatch) -> None:
        """Deferred observer failure should not affect the caller."""
        bad = _make_async_observer(
            name="Bad", side_effect=RuntimeError("boom"), observe_inline=False
        )
        dispatch.register_observe(bad)
        # Should not raise — error is logged in background
        await dispatch.notify(_write_event())
        await asyncio.sleep(0.05)
        assert len(dispatch._background_tasks) == 0


class TestHybridDispatch:
    """Mix of inline and deferred observers in the same notify() call."""

    async def test_inline_fires_immediately_deferred_fires_later(
        self, dispatch: _TestDispatch
    ) -> None:
        inline = _make_async_observer(name="Inline", observe_inline=True)
        deferred = _make_async_observer(name="Deferred", delay=0.05, observe_inline=False)
        dispatch.register_observe(inline)
        dispatch.register_observe(deferred)

        await dispatch.notify(_write_event())

        # Inline: already delivered
        assert len(inline._calls) == 1
        # Deferred: not yet delivered
        assert len(deferred._calls) == 0

        # Wait for deferred to complete
        await asyncio.sleep(0.1)
        assert len(deferred._calls) == 1

    async def test_deferred_failure_does_not_affect_inline(self, dispatch: _TestDispatch) -> None:
        inline = _make_async_observer(name="Inline", observe_inline=True)
        bad_deferred = _make_async_observer(
            name="BadDeferred", side_effect=RuntimeError("boom"), observe_inline=False
        )
        dispatch.register_observe(inline)
        dispatch.register_observe(bad_deferred)

        await dispatch.notify(_write_event())
        assert len(inline._calls) == 1

    async def test_notify_returns_fast_with_slow_deferred(self, dispatch: _TestDispatch) -> None:
        """notify() should return in <10ms even with a 500ms deferred observer."""
        import time

        slow = _make_async_observer(name="Slow", delay=0.5, observe_inline=False)
        dispatch.register_observe(slow)

        t0 = time.monotonic()
        await dispatch.notify(_write_event())
        elapsed = time.monotonic() - t0

        assert elapsed < 0.01, f"notify() blocked for {elapsed:.3f}s — should be fire-and-forget"
        # Clean up: cancel the slow background task
        await dispatch.shutdown(timeout=0.01)


# ── Background task lifecycle tests (Issue #3391) ────────────────────


class TestBackgroundTaskLifecycle:
    """Tests for _background_tasks tracking, exception logging, and shutdown."""

    async def test_task_appears_in_tracking_set(self, dispatch: _TestDispatch) -> None:
        obs = _make_async_observer(name="Tracked", delay=0.1, observe_inline=False)
        dispatch.register_observe(obs)
        await dispatch.notify(_write_event())
        assert len(dispatch._background_tasks) == 1

    async def test_failed_task_logs_warning(self, dispatch: _TestDispatch, caplog) -> None:
        import logging

        obs = _make_async_observer(
            name="Failing", side_effect=RuntimeError("test-error"), observe_inline=False
        )
        dispatch.register_observe(obs)

        with caplog.at_level(logging.WARNING, logger="nexus.core.nexus_fs_dispatch"):
            await dispatch.notify(_write_event())
            await asyncio.sleep(0.05)

        assert any("test-error" in r.message for r in caplog.records)

    async def test_shutdown_drains_pending_tasks(self, dispatch: _TestDispatch) -> None:
        obs = _make_async_observer(name="Slow", delay=0.05, observe_inline=False)
        dispatch.register_observe(obs)
        await dispatch.notify(_write_event())
        assert len(dispatch._background_tasks) == 1

        await dispatch.shutdown(timeout=1.0)
        assert len(dispatch._background_tasks) == 0
        assert len(obs._calls) == 1  # task completed, not cancelled

    async def test_shutdown_cancels_stragglers(self, dispatch: _TestDispatch) -> None:
        obs = _make_async_observer(name="VerySlowObs", delay=10.0, observe_inline=False)
        dispatch.register_observe(obs)
        await dispatch.notify(_write_event())

        await dispatch.shutdown(timeout=0.01)
        # Task was cancelled — event not delivered
        assert len(obs._calls) == 0

    async def test_shutdown_noop_when_empty(self, dispatch: _TestDispatch) -> None:
        """shutdown() with no pending tasks should return immediately."""
        await dispatch.shutdown()  # should not raise
