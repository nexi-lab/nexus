"""Unit tests for KernelDispatch (INTERCEPT + OBSERVE phases).

Tests dispatch mechanics: registration, hook invocation order,
error handling (warnings for INTERCEPT, log-and-continue for OBSERVE),
and write observer integration.

Issue #900.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nexus.contracts.vfs_hooks import (
    MutationEvent,
    MutationOp,
    ReadHookContext,
    RenameHookContext,
    WriteHookContext,
)
from nexus.core.kernel_dispatch import KernelDispatch


class _CountingReadHook:
    """Test hook that counts invocations."""

    def __init__(self, name: str = "counting_read") -> None:
        self._name = name
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    def on_post_read(self, ctx: ReadHookContext) -> None:
        self.call_count += 1


class _FilteringReadHook:
    """Test hook that transforms content."""

    @property
    def name(self) -> str:
        return "filtering_read"

    def on_post_read(self, ctx: ReadHookContext) -> None:
        if ctx.content is not None:
            ctx.content = ctx.content.upper()


class _FailingReadHook:
    """Test hook that always raises."""

    @property
    def name(self) -> str:
        return "failing_read"

    def on_post_read(self, ctx: ReadHookContext) -> None:
        raise RuntimeError("hook exploded")


class _CountingWriteHook:
    """Test hook that counts write invocations."""

    def __init__(self) -> None:
        self.call_count = 0
        self.last_ctx: WriteHookContext | None = None

    @property
    def name(self) -> str:
        return "counting_write"

    def on_post_write(self, ctx: WriteHookContext) -> None:
        self.call_count += 1
        self.last_ctx = ctx


class _FailingWriteHook:
    @property
    def name(self) -> str:
        return "failing_write"

    def on_post_write(self, ctx: WriteHookContext) -> None:
        raise ValueError("write hook failed")


class _CountingRenameHook:
    def __init__(self) -> None:
        self.call_count = 0

    @property
    def name(self) -> str:
        return "counting_rename"

    def on_post_rename(self, ctx: RenameHookContext) -> None:
        self.call_count += 1


class TestKernelDispatchRegistration:
    def test_empty_dispatch(self):
        d = KernelDispatch()
        assert d.read_hook_count == 0
        assert d.write_hook_count == 0
        assert d.delete_hook_count == 0
        assert d.rename_hook_count == 0
        assert d.mkdir_hook_count == 0
        assert d.rmdir_hook_count == 0
        assert d.observer_count == 0

    def test_register_read_hook(self):
        d = KernelDispatch()
        d.register_intercept_read(_CountingReadHook())
        assert d.read_hook_count == 1

    def test_register_multiple_hooks(self):
        d = KernelDispatch()
        d.register_intercept_read(_CountingReadHook("a"))
        d.register_intercept_read(_CountingReadHook("b"))
        assert d.read_hook_count == 2

    def test_register_observe(self):
        d = KernelDispatch()
        obs = MagicMock()
        d.register_observe(obs)
        assert d.observer_count == 1


class TestKernelDispatchInterceptRead:
    def test_post_read_calls_all_hooks(self):
        d = KernelDispatch()
        h1 = _CountingReadHook("h1")
        h2 = _CountingReadHook("h2")
        d.register_intercept_read(h1)
        d.register_intercept_read(h2)

        ctx = ReadHookContext(path="/test.txt", context=None, content=b"hello")
        d.intercept_post_read(ctx)

        assert h1.call_count == 1
        assert h2.call_count == 1

    def test_post_read_hook_transforms_content(self):
        d = KernelDispatch()
        d.register_intercept_read(_FilteringReadHook())

        ctx = ReadHookContext(path="/test.txt", context=None, content=b"hello")
        d.intercept_post_read(ctx)

        assert ctx.content == b"HELLO"

    def test_failing_hook_adds_warning(self):
        d = KernelDispatch()
        d.register_intercept_read(_FailingReadHook())

        ctx = ReadHookContext(path="/test.txt", context=None)
        d.intercept_post_read(ctx)

        assert len(ctx.warnings) == 1
        assert ctx.warnings[0].severity == "degraded"
        assert ctx.warnings[0].component == "failing_read"
        assert "hook exploded" in ctx.warnings[0].message

    def test_failing_hook_does_not_stop_subsequent_hooks(self):
        d = KernelDispatch()
        counter = _CountingReadHook()
        d.register_intercept_read(_FailingReadHook())
        d.register_intercept_read(counter)

        ctx = ReadHookContext(path="/test.txt", context=None)
        d.intercept_post_read(ctx)

        assert counter.call_count == 1  # still called despite earlier failure
        assert len(ctx.warnings) == 1

    def test_empty_dispatch_no_warnings(self):
        d = KernelDispatch()
        ctx = ReadHookContext(path="/test.txt", context=None)
        d.intercept_post_read(ctx)
        assert len(ctx.warnings) == 0


class TestKernelDispatchInterceptWrite:
    def test_post_write_calls_hook(self):
        d = KernelDispatch()
        h = _CountingWriteHook()
        d.register_intercept_write(h)

        ctx = WriteHookContext(path="/test.txt", content=b"data", context=None)
        d.intercept_post_write(ctx)

        assert h.call_count == 1
        assert h.last_ctx is ctx

    def test_failing_write_hook_adds_warning(self):
        d = KernelDispatch()
        d.register_intercept_write(_FailingWriteHook())

        ctx = WriteHookContext(path="/test.txt", content=b"data", context=None)
        d.intercept_post_write(ctx)

        assert len(ctx.warnings) == 1
        assert ctx.warnings[0].component == "failing_write"

    def test_write_hook_receives_metadata(self):
        d = KernelDispatch()
        h = _CountingWriteHook()
        d.register_intercept_write(h)

        ctx = WriteHookContext(
            path="/test.txt",
            content=b"data",
            context=None,
            is_new_file=True,
            content_hash="abc123",
            new_version=1,
            zone_id="test-zone",
        )
        d.intercept_post_write(ctx)

        assert h.last_ctx is not None
        assert h.last_ctx.is_new_file is True
        assert h.last_ctx.content_hash == "abc123"


class TestKernelDispatchInterceptRename:
    def test_post_rename_calls_hook(self):
        d = KernelDispatch()
        h = _CountingRenameHook()
        d.register_intercept_rename(h)

        ctx = RenameHookContext(old_path="/a.txt", new_path="/b.txt", context=None)
        d.intercept_post_rename(ctx)

        assert h.call_count == 1


class TestKernelDispatchObserve:
    def test_notify_calls_all_observers(self):
        d = KernelDispatch()
        obs1 = MagicMock()
        obs2 = MagicMock()
        d.register_observe(obs1)
        d.register_observe(obs2)

        event = MutationEvent(
            operation=MutationOp.WRITE,
            path="/test.txt",
            zone_id="root",
            revision=1,
        )
        d.notify(event)

        obs1.on_mutation.assert_called_once_with(event)
        obs2.on_mutation.assert_called_once_with(event)

    def test_failing_observer_does_not_stop_others(self):
        d = KernelDispatch()
        obs1 = MagicMock()
        obs1.on_mutation.side_effect = RuntimeError("boom")
        obs2 = MagicMock()
        d.register_observe(obs1)
        d.register_observe(obs2)

        event = MutationEvent(
            operation=MutationOp.DELETE,
            path="/gone.txt",
            zone_id="root",
            revision=2,
        )
        d.notify(event)

        obs2.on_mutation.assert_called_once_with(event)

    def test_no_observers_is_noop(self):
        d = KernelDispatch()
        event = MutationEvent(
            operation=MutationOp.WRITE,
            path="/x.txt",
            zone_id="root",
            revision=1,
        )
        d.notify(event)  # should not raise


class TestKernelDispatchWriteObserver:
    def test_write_observer_called_during_intercept_write(self):
        obs = MagicMock()
        d = KernelDispatch(write_observer=obs)

        ctx = WriteHookContext(path="/test.txt", content=b"data", context=None)
        d.intercept_post_write(ctx)

        obs.on_write.assert_called_once()

    def test_write_observer_called_during_intercept_delete(self):
        obs = MagicMock()
        d = KernelDispatch(write_observer=obs)

        from nexus.contracts.vfs_hooks import DeleteHookContext

        ctx = DeleteHookContext(path="/test.txt", context=None)
        d.intercept_post_delete(ctx)

        obs.on_delete.assert_called_once()

    def test_no_write_observer_is_noop(self):
        d = KernelDispatch(write_observer=None)

        ctx = WriteHookContext(path="/test.txt", content=b"data", context=None)
        d.intercept_post_write(ctx)  # should not raise

    def test_audit_strict_mode_raises_on_observer_failure(self):
        import pytest

        from nexus.contracts.exceptions import AuditLogError

        obs = MagicMock()
        obs.on_write.side_effect = RuntimeError("db down")
        d = KernelDispatch(write_observer=obs, audit_strict_mode=True)

        ctx = WriteHookContext(path="/test.txt", content=b"data", context=None)
        with pytest.raises(AuditLogError):
            d.intercept_post_write(ctx)

    def test_audit_non_strict_logs_but_continues(self):
        obs = MagicMock()
        obs.on_write.side_effect = RuntimeError("db down")
        d = KernelDispatch(write_observer=obs, audit_strict_mode=False)

        ctx = WriteHookContext(path="/test.txt", content=b"data", context=None)
        d.intercept_post_write(ctx)  # should not raise
