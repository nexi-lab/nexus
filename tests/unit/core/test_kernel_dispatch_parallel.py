"""Unit tests for async parallel dispatch (Issue #1317, #1748, #1812, #3391).

Tests:
- POST hook dispatch (sync/async/mixed/fault isolation)
- Async OBSERVE dispatch with ObserverRegistry + event_mask filtering
- Hybrid inline/deferred OBSERVE dispatch (Issue #3391)
- Background task lifecycle (tracking, exception logging, shutdown)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.contracts.exceptions import AuditLogError
from nexus.contracts.vfs_hooks import WriteHookContext
from nexus.core.file_events import ALL_FILE_EVENTS, FILE_EVENT_BIT, FileEvent, FileEventType
from nexus.core.kernel_dispatch import KernelDispatch, _PythonObserverRegistry


@pytest.fixture()
def dispatch() -> KernelDispatch:
    from nexus_fast import Kernel

    d = KernelDispatch()
    d._kernel = Kernel()
    return d


def _make_sync_hook(*, name: str = "sync_hook", side_effect: Exception | None = None):
    hook = MagicMock()
    hook.name = name
    hook.TRIE_PATTERN = None
    if side_effect:
        hook.on_post_write.side_effect = side_effect
    return hook


def _make_async_hook(
    *,
    name: str = "async_hook",
    delay: float = 0.0,
    side_effect: Exception | None = None,
):
    """Create a hook with async on_post_write."""
    hook = MagicMock()
    hook.name = name
    hook.TRIE_PATTERN = None

    async def _async_post_write(ctx):
        if delay > 0:
            await asyncio.sleep(delay)
        if side_effect:
            raise side_effect

    hook.on_post_write = _async_post_write
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
    async def test_sync_hook_called(self, dispatch: KernelDispatch) -> None:
        hook = _make_sync_hook()
        dispatch.register_intercept_write(hook)
        ctx = _write_ctx()
        await dispatch.intercept_post_write(ctx)
        hook.on_post_write.assert_called_once_with(ctx)

    async def test_sync_hook_fault_isolation(self, dispatch: KernelDispatch) -> None:
        bad = _make_sync_hook(name="bad", side_effect=RuntimeError("boom"))
        good = _make_sync_hook(name="good")
        dispatch.register_intercept_write(bad)
        dispatch.register_intercept_write(good)

        ctx = _write_ctx()
        await dispatch.intercept_post_write(ctx)

        good.on_post_write.assert_called_once()
        assert len(ctx.warnings) == 1
        assert "boom" in ctx.warnings[0].message

    async def test_audit_log_error_aborts(self, dispatch: KernelDispatch) -> None:
        hook = _make_sync_hook(side_effect=AuditLogError("critical"))
        dispatch.register_intercept_write(hook)

        with pytest.raises(AuditLogError, match="critical"):
            await dispatch.intercept_post_write(_write_ctx())


class TestAsyncPostHooks:
    async def test_async_hook_called(self, dispatch: KernelDispatch) -> None:
        hook = _make_async_hook()
        dispatch.register_intercept_write(hook)
        ctx = _write_ctx()
        await dispatch.intercept_post_write(ctx)
        assert len(ctx.warnings) == 0

    async def test_async_hooks_run_parallel(self, dispatch: KernelDispatch) -> None:
        """Two hooks each sleeping 0.1s should complete in ~0.1s, not ~0.2s."""
        dispatch.register_intercept_write(_make_async_hook(name="a", delay=0.1))
        dispatch.register_intercept_write(_make_async_hook(name="b", delay=0.1))

        ctx = _write_ctx()
        import time

        t0 = time.monotonic()
        await dispatch.intercept_post_write(ctx)
        elapsed = time.monotonic() - t0

        assert elapsed < 0.18, f"Expected parallel (~0.1s), got {elapsed:.3f}s"

    async def test_async_hook_fault_isolation(self, dispatch: KernelDispatch) -> None:
        bad = _make_async_hook(name="bad", side_effect=RuntimeError("async boom"))
        good = _make_async_hook(name="good", delay=0.01)
        dispatch.register_intercept_write(bad)
        dispatch.register_intercept_write(good)

        ctx = _write_ctx()
        await dispatch.intercept_post_write(ctx)

        assert len(ctx.warnings) == 1
        assert "async boom" in ctx.warnings[0].message

    async def test_async_hook_timeout(self, dispatch: KernelDispatch) -> None:
        slow = _make_async_hook(name="slow", delay=10.0)
        dispatch.register_intercept_write(slow)

        ctx = _write_ctx()
        await dispatch._post_dispatch("write", "on_post_write", ctx, timeout=0.1)

        assert len(ctx.warnings) == 1
        assert "slow" in ctx.warnings[0].component

    async def test_async_audit_log_error_aborts(self, dispatch: KernelDispatch) -> None:
        hook = _make_async_hook(side_effect=AuditLogError("async critical"))
        dispatch.register_intercept_write(hook)

        with pytest.raises(AuditLogError, match="async critical"):
            await dispatch.intercept_post_write(_write_ctx())


class TestMixedHooks:
    async def test_sync_and_async_together(self, dispatch: KernelDispatch) -> None:
        sync_hook = _make_sync_hook(name="sync")
        async_hook = _make_async_hook(name="async", delay=0.01)
        dispatch.register_intercept_write(sync_hook)
        dispatch.register_intercept_write(async_hook)

        ctx = _write_ctx()
        await dispatch.intercept_post_write(ctx)

        sync_hook.on_post_write.assert_called_once_with(ctx)
        assert len(ctx.warnings) == 0


# ── Async OBSERVE dispatch tests (Issue #1748, #1812) ─────────────────


def _make_async_observer(
    *,
    name: str = "TestObserver",
    event_mask: int = ALL_FILE_EVENTS,
    side_effect: Exception | None = None,
    delay: float = 0.0,
    observe_inline: bool = True,
):
    """Create an async observer with event_mask and OBSERVE_INLINE control."""

    class _Obs:
        pass

    obs = _Obs()
    obs.__class__.__name__ = name
    obs.event_mask = event_mask
    obs.OBSERVE_INLINE = observe_inline

    calls: list = []

    async def _on_mutation(event):
        if delay > 0:
            await asyncio.sleep(delay)
        if side_effect:
            raise side_effect
        calls.append(event)

    obs.on_mutation = _on_mutation
    obs._calls = calls
    return obs


class TestAsyncObserveDispatch:
    """Tests for the async OBSERVE phase with ObserverRegistry."""

    async def test_observer_called(self, dispatch: KernelDispatch) -> None:
        obs = _make_async_observer()
        dispatch.register_observe(obs)
        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test")
        await dispatch.notify(event)
        assert len(obs._calls) == 1
        assert obs._calls[0] is event

    async def test_event_mask_filtering(self, dispatch: KernelDispatch) -> None:
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

    async def test_event_mask_allows_matching_events(self, dispatch: KernelDispatch) -> None:
        cas_obs = _make_async_observer(
            name="CAS",
            event_mask=FILE_EVENT_BIT[FileEventType.FILE_WRITE]
            | FILE_EVENT_BIT[FileEventType.FILE_DELETE],
        )
        dispatch.register_observe(cas_obs)
        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt")
        await dispatch.notify(event)
        assert len(cas_obs._calls) == 1

    async def test_fault_isolation(self, dispatch: KernelDispatch) -> None:
        """One observer raising should not prevent others from firing."""
        bad = _make_async_observer(name="Bad", side_effect=RuntimeError("kaboom"))
        good = _make_async_observer(name="Good")
        dispatch.register_observe(bad)
        dispatch.register_observe(good)

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test")
        await dispatch.notify(event)
        assert len(good._calls) == 1

    async def test_concurrent_execution(self, dispatch: KernelDispatch) -> None:
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

    async def test_unregister(self, dispatch: KernelDispatch) -> None:
        obs = _make_async_observer()
        dispatch.register_observe(obs)
        assert dispatch.observer_count == 1
        removed = dispatch.unregister_observe(obs)
        assert removed is True
        assert dispatch.observer_count == 0

    async def test_observer_count(self, dispatch: KernelDispatch) -> None:
        a = _make_async_observer(name="A")
        b = _make_async_observer(name="B")
        dispatch.register_observe(a)
        dispatch.register_observe(b)
        assert dispatch.observer_count == 2


class TestPythonObserverRegistryFallback:
    """Tests for the pure-Python fallback ObserverRegistry."""

    def test_register_and_get_matching(self) -> None:
        reg = _PythonObserverRegistry()
        obs = MagicMock()
        reg.register(obs, 0x03)  # FILE_WRITE | FILE_DELETE
        assert len(reg.get_matching(0x01)) == 1  # FILE_WRITE matches
        assert len(reg.get_matching(0x10)) == 0  # DIR_CREATE does not

    def test_unregister(self) -> None:
        reg = _PythonObserverRegistry()
        obs = MagicMock()
        reg.register(obs, 0x03)
        assert reg.count() == 1
        assert reg.unregister(obs) is True
        assert reg.count() == 0

    def test_unregister_not_found(self) -> None:
        reg = _PythonObserverRegistry()
        obs = MagicMock()
        assert reg.unregister(obs) is False


# ── Hybrid inline/deferred OBSERVE dispatch tests (Issue #3391) ──────


def _write_event(path: str = "/test") -> FileEvent:
    return FileEvent(type=FileEventType.FILE_WRITE, path=path)


class TestInlineObservers:
    """OBSERVE_INLINE=True observers run on the caller's path."""

    async def test_inline_observer_called_before_notify_returns(
        self, dispatch: KernelDispatch
    ) -> None:
        obs = _make_async_observer(name="Inline", observe_inline=True)
        dispatch.register_observe(obs)
        await dispatch.notify(_write_event())
        # Event delivered synchronously — available immediately after notify returns
        assert len(obs._calls) == 1

    async def test_inline_observers_run_concurrently(self, dispatch: KernelDispatch) -> None:
        """Two inline observers each sleeping 0.1s should complete in ~0.1s."""
        import time

        a = _make_async_observer(name="A", delay=0.1, observe_inline=True)
        b = _make_async_observer(name="B", delay=0.1, observe_inline=True)
        dispatch.register_observe(a)
        dispatch.register_observe(b)

        t0 = time.monotonic()
        await dispatch.notify(_write_event())
        elapsed = time.monotonic() - t0

        assert len(a._calls) == 1
        assert len(b._calls) == 1
        assert elapsed < 0.18, f"Expected parallel (~0.1s), got {elapsed:.3f}s"

    async def test_inline_fault_isolation(self, dispatch: KernelDispatch) -> None:
        bad = _make_async_observer(
            name="Bad", side_effect=RuntimeError("boom"), observe_inline=True
        )
        good = _make_async_observer(name="Good", observe_inline=True)
        dispatch.register_observe(bad)
        dispatch.register_observe(good)

        await dispatch.notify(_write_event())
        assert len(good._calls) == 1


class TestDeferredObservers:
    """OBSERVE_INLINE=False observers run as background tasks."""

    async def test_deferred_observer_not_called_before_notify_returns(
        self, dispatch: KernelDispatch
    ) -> None:
        obs = _make_async_observer(name="Deferred", delay=0.05, observe_inline=False)
        dispatch.register_observe(obs)
        await dispatch.notify(_write_event())
        # Deferred: not yet delivered (still running as background task)
        assert len(obs._calls) == 0
        assert len(dispatch._background_tasks) == 1

    async def test_deferred_observer_eventually_called(self, dispatch: KernelDispatch) -> None:
        obs = _make_async_observer(name="Deferred", observe_inline=False)
        dispatch.register_observe(obs)
        await dispatch.notify(_write_event())
        # Give the background task a chance to complete
        await asyncio.sleep(0.05)
        assert len(obs._calls) == 1

    async def test_deferred_task_cleaned_up_after_completion(
        self, dispatch: KernelDispatch
    ) -> None:
        obs = _make_async_observer(name="Deferred", observe_inline=False)
        dispatch.register_observe(obs)
        await dispatch.notify(_write_event())
        assert len(dispatch._background_tasks) == 1
        # Wait for task to complete and done-callback to fire
        await asyncio.sleep(0.05)
        assert len(dispatch._background_tasks) == 0

    async def test_deferred_fault_isolation(self, dispatch: KernelDispatch) -> None:
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
        self, dispatch: KernelDispatch
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

    async def test_deferred_failure_does_not_affect_inline(self, dispatch: KernelDispatch) -> None:
        inline = _make_async_observer(name="Inline", observe_inline=True)
        bad_deferred = _make_async_observer(
            name="BadDeferred", side_effect=RuntimeError("boom"), observe_inline=False
        )
        dispatch.register_observe(inline)
        dispatch.register_observe(bad_deferred)

        await dispatch.notify(_write_event())
        assert len(inline._calls) == 1

    async def test_notify_returns_fast_with_slow_deferred(self, dispatch: KernelDispatch) -> None:
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

    async def test_task_appears_in_tracking_set(self, dispatch: KernelDispatch) -> None:
        obs = _make_async_observer(name="Tracked", delay=0.1, observe_inline=False)
        dispatch.register_observe(obs)
        await dispatch.notify(_write_event())
        assert len(dispatch._background_tasks) == 1

    async def test_failed_task_logs_warning(self, dispatch: KernelDispatch, caplog) -> None:
        import logging

        obs = _make_async_observer(
            name="Failing", side_effect=RuntimeError("test-error"), observe_inline=False
        )
        dispatch.register_observe(obs)

        with caplog.at_level(logging.WARNING, logger="nexus.core.kernel_dispatch"):
            await dispatch.notify(_write_event())
            await asyncio.sleep(0.05)

        assert any("test-error" in r.message for r in caplog.records)

    async def test_shutdown_drains_pending_tasks(self, dispatch: KernelDispatch) -> None:
        obs = _make_async_observer(name="Slow", delay=0.05, observe_inline=False)
        dispatch.register_observe(obs)
        await dispatch.notify(_write_event())
        assert len(dispatch._background_tasks) == 1

        await dispatch.shutdown(timeout=1.0)
        assert len(dispatch._background_tasks) == 0
        assert len(obs._calls) == 1  # task completed, not cancelled

    async def test_shutdown_cancels_stragglers(self, dispatch: KernelDispatch) -> None:
        obs = _make_async_observer(name="VerySlowObs", delay=10.0, observe_inline=False)
        dispatch.register_observe(obs)
        await dispatch.notify(_write_event())

        await dispatch.shutdown(timeout=0.01)
        # Task was cancelled — event not delivered
        assert len(obs._calls) == 0

    async def test_shutdown_noop_when_empty(self, dispatch: KernelDispatch) -> None:
        """shutdown() with no pending tasks should return immediately."""
        await dispatch.shutdown()  # should not raise
