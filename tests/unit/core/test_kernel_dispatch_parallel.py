"""Unit tests for Rust-dispatched post hooks and async OBSERVE dispatch.

Tests:
- POST hook dispatch via Rust dispatch_post_hooks (sync, fire-and-forget)
- Async OBSERVE dispatch with ObserverRegistry + event_mask filtering
- Hybrid inline/deferred OBSERVE dispatch (Issue #3391)
- Background task lifecycle (tracking, exception logging, shutdown)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.contracts.vfs_hooks import WriteHookContext
from nexus.core.file_events import ALL_FILE_EVENTS, FILE_EVENT_BIT, FileEvent, FileEventType
from nexus.core.nexus_fs_dispatch import DispatchMixin


class _TestDispatch(DispatchMixin):
    def __init__(self):
        from nexus_runtime import PyKernel

        self._kernel = PyKernel()
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
        "content_id": "abc",
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

    def test_observer_called(self, dispatch: _TestDispatch) -> None:
        obs = _make_async_observer()
        dispatch.register_observe(obs)
        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test")
        dispatch.notify(event)
        assert len(obs._calls) == 1
        assert obs._calls[0] is event

    def test_event_mask_filtering(self, dispatch: _TestDispatch) -> None:
        """CAS observer with WRITE|DELETE mask should NOT fire for DIR_CREATE."""
        cas_obs = _make_async_observer(
            name="CAS",
            event_mask=FILE_EVENT_BIT[FileEventType.FILE_WRITE]
            | FILE_EVENT_BIT[FileEventType.FILE_DELETE],
        )
        dispatch.register_observe(cas_obs)
        event = FileEvent(type=FileEventType.DIR_CREATE, path="/mydir")
        dispatch.notify(event)
        assert len(cas_obs._calls) == 0

    def test_event_mask_allows_matching_events(self, dispatch: _TestDispatch) -> None:
        cas_obs = _make_async_observer(
            name="CAS",
            event_mask=FILE_EVENT_BIT[FileEventType.FILE_WRITE]
            | FILE_EVENT_BIT[FileEventType.FILE_DELETE],
        )
        dispatch.register_observe(cas_obs)
        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt")
        dispatch.notify(event)
        assert len(cas_obs._calls) == 1

    def test_fault_isolation(self, dispatch: _TestDispatch) -> None:
        """One observer raising should not prevent others from firing."""
        bad = _make_async_observer(name="Bad", side_effect=RuntimeError("kaboom"))
        good = _make_async_observer(name="Good")
        dispatch.register_observe(bad)
        dispatch.register_observe(good)

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test")
        dispatch.notify(event)
        assert len(good._calls) == 1

    def test_concurrent_execution(self, dispatch: _TestDispatch) -> None:
        """Multiple observers sleeping should run in parallel (gather), not serial."""
        import time

        a = _make_async_observer(name="A", delay=0.1)
        b = _make_async_observer(name="B", delay=0.1)
        dispatch.register_observe(a)
        dispatch.register_observe(b)

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test")
        t0 = time.monotonic()
        dispatch.notify(event)
        elapsed = time.monotonic() - t0

        assert len(a._calls) == 1
        assert len(b._calls) == 1
        assert elapsed < 0.18, f"Expected parallel (~0.1s), got {elapsed:.3f}s"

    def test_unregister(self, dispatch: _TestDispatch) -> None:
        obs = _make_async_observer()
        dispatch.register_observe(obs)
        assert dispatch.observer_count == 1
        removed = dispatch.unregister_observe(obs)
        assert removed is True
        assert dispatch.observer_count == 0

    def test_observer_count(self, dispatch: _TestDispatch) -> None:
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

    def test_observer_called_after_notify(self, dispatch: _TestDispatch) -> None:
        obs = _make_async_observer(name="Sync")
        dispatch.register_observe(obs)
        dispatch.notify(_write_event())
        assert len(obs._calls) == 1

    def test_multiple_observers_all_called(self, dispatch: _TestDispatch) -> None:
        a = _make_async_observer(name="A")
        b = _make_async_observer(name="B")
        dispatch.register_observe(a)
        dispatch.register_observe(b)
        dispatch.notify(_write_event())
        assert len(a._calls) == 1
        assert len(b._calls) == 1

    def test_fault_isolation(self, dispatch: _TestDispatch) -> None:
        bad = _make_async_observer(name="Bad", side_effect=RuntimeError("boom"))
        good = _make_async_observer(name="Good")
        dispatch.register_observe(bad)
        dispatch.register_observe(good)

        dispatch.notify(_write_event())
        assert len(good._calls) == 1

    def test_failed_observer_logs_warning(self, dispatch: _TestDispatch, caplog) -> None:
        """Observer exceptions are caught and logged, never raised to caller."""
        import logging

        obs = _make_async_observer(name="Failing", side_effect=RuntimeError("test-error"))
        dispatch.register_observe(obs)

        with caplog.at_level(logging.WARNING, logger="nexus.core.nexus_fs_dispatch"):
            dispatch.notify(_write_event())

        assert any("test-error" in r.message for r in caplog.records)

    async def test_shutdown_noop(self, dispatch: _TestDispatch) -> None:
        """shutdown() should return immediately."""
        dispatch.shutdown()  # should not raise
