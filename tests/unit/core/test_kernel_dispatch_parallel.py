"""Unit tests for async parallel dispatch (Issue #1317, #1748, #1812).

Tests:
- POST hook dispatch (sync/async/mixed/fault isolation)
- Async OBSERVE dispatch with ObserverRegistry + event_mask filtering
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
    return KernelDispatch()


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
):
    """Create an async observer with event_mask."""

    class _Obs:
        pass

    obs = _Obs()
    obs.__class__.__name__ = name
    obs.event_mask = event_mask

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
