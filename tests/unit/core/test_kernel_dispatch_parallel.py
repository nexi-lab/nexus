"""Unit tests for async parallel POST hook dispatch (Issue #1317 Phase 3)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.contracts.exceptions import AuditLogError
from nexus.contracts.vfs_hooks import WriteHookContext
from nexus.core.kernel_dispatch import KernelDispatch


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
        # async fn was called (no assertion on mock — it's a real coroutine)
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


class TestAsyncNotify:
    async def test_notify_calls_observers(self, dispatch: KernelDispatch) -> None:
        from nexus.core.file_events import FileEvent, FileEventType

        obs = MagicMock()
        dispatch.register_observe(obs)
        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test")
        dispatch.notify(event)
        obs.on_mutation.assert_called_once_with(event)

    async def test_notify_fault_isolation(self, dispatch: KernelDispatch) -> None:
        from nexus.core.file_events import FileEvent, FileEventType

        bad = MagicMock()
        bad.on_mutation.side_effect = RuntimeError("observer boom")
        good = MagicMock()
        dispatch.register_observe(bad)
        dispatch.register_observe(good)

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test")
        dispatch.notify(event)

        good.on_mutation.assert_called_once_with(event)
