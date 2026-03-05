"""Unit tests for KernelDispatch process hook support (Issue #2761).

Tests register_intercept_process, intercept_pre_proc_spawn,
intercept_post_proc_spawn, and intercept_post_proc_terminate.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.contracts.vfs_hooks import (
    ProcessSpawnHookContext,
    ProcessTerminateHookContext,
)
from nexus.core.kernel_dispatch import KernelDispatch

# ======================================================================
# Helpers
# ======================================================================


def _make_spawn_ctx(**overrides) -> ProcessSpawnHookContext:
    defaults = {
        "agent_id": "agent-a",
        "zone_id": "zone-1",
        "pid": "pid-1",
        "parent_pid": None,
    }
    defaults.update(overrides)
    return ProcessSpawnHookContext(**defaults)


def _make_terminate_ctx(**overrides) -> ProcessTerminateHookContext:
    defaults = {
        "pid": "pid-1",
        "agent_id": "agent-a",
        "zone_id": "zone-1",
        "reason": "terminated",
        "exit_code": 0,
    }
    defaults.update(overrides)
    return ProcessTerminateHookContext(**defaults)


def _make_hook(*, name="test_hook", fail_on=None):
    """Create a mock VFSProcessHook.

    Args:
        name: Hook name.
        fail_on: Method name to raise RuntimeError on (e.g. "on_post_proc_spawn").
    """
    hook = MagicMock()
    hook.name = name

    if fail_on:
        getattr(hook, fail_on).side_effect = RuntimeError("hook failed")

    return hook


# ======================================================================
# Registration
# ======================================================================


class TestRegisterInterceptProcess:
    """Tests for KernelDispatch.register_intercept_process."""

    def test_register_increases_count(self):
        dispatch = KernelDispatch()
        assert dispatch.process_hook_count == 0

        hook = _make_hook()
        dispatch.register_intercept_process(hook)
        assert dispatch.process_hook_count == 1

    def test_register_multiple_hooks(self):
        dispatch = KernelDispatch()
        dispatch.register_intercept_process(_make_hook(name="h1"))
        dispatch.register_intercept_process(_make_hook(name="h2"))
        dispatch.register_intercept_process(_make_hook(name="h3"))
        assert dispatch.process_hook_count == 3


# ======================================================================
# intercept_pre_proc_spawn
# ======================================================================


class TestInterceptPreProcSpawn:
    """Tests for KernelDispatch.intercept_pre_proc_spawn."""

    def test_calls_hooks_in_order(self):
        dispatch = KernelDispatch()
        call_order = []

        h1 = _make_hook(name="h1")
        h1.on_pre_proc_spawn.side_effect = lambda ctx: call_order.append("h1")
        h2 = _make_hook(name="h2")
        h2.on_pre_proc_spawn.side_effect = lambda ctx: call_order.append("h2")

        dispatch.register_intercept_process(h1)
        dispatch.register_intercept_process(h2)

        ctx = _make_spawn_ctx()
        dispatch.intercept_pre_proc_spawn(ctx)

        assert call_order == ["h1", "h2"]

    def test_abort_on_raise(self):
        dispatch = KernelDispatch()

        h1 = _make_hook(name="h1")
        h1.on_pre_proc_spawn.side_effect = PermissionError("denied")

        dispatch.register_intercept_process(h1)

        ctx = _make_spawn_ctx()
        with pytest.raises(PermissionError, match="denied"):
            dispatch.intercept_pre_proc_spawn(ctx)

    def test_skips_hooks_without_method(self):
        dispatch = KernelDispatch()

        hook = MagicMock(spec=[])  # No methods at all
        hook.name = "empty_hook"
        dispatch.register_intercept_process(hook)

        ctx = _make_spawn_ctx()
        # Should not raise — getattr returns None, skip
        dispatch.intercept_pre_proc_spawn(ctx)

    def test_no_hooks_is_noop(self):
        dispatch = KernelDispatch()
        ctx = _make_spawn_ctx()
        # Should not raise
        dispatch.intercept_pre_proc_spawn(ctx)
        assert ctx.warnings == []


# ======================================================================
# intercept_post_proc_spawn
# ======================================================================


class TestInterceptPostProcSpawn:
    """Tests for KernelDispatch.intercept_post_proc_spawn."""

    def test_calls_hooks(self):
        dispatch = KernelDispatch()
        hook = _make_hook()
        dispatch.register_intercept_process(hook)

        ctx = _make_spawn_ctx()
        dispatch.intercept_post_proc_spawn(ctx)

        hook.on_post_proc_spawn.assert_called_once_with(ctx)

    def test_warnings_on_failure(self):
        dispatch = KernelDispatch()
        hook = _make_hook(fail_on="on_post_proc_spawn")
        dispatch.register_intercept_process(hook)

        ctx = _make_spawn_ctx()
        dispatch.intercept_post_proc_spawn(ctx)

        assert len(ctx.warnings) == 1
        assert "hook failed" in ctx.warnings[0].message
        assert ctx.warnings[0].component == "test_hook"
        assert ctx.warnings[0].severity == "degraded"

    def test_multiple_hooks_continue_after_failure(self):
        dispatch = KernelDispatch()

        h1 = _make_hook(name="h1", fail_on="on_post_proc_spawn")
        h2 = _make_hook(name="h2")

        dispatch.register_intercept_process(h1)
        dispatch.register_intercept_process(h2)

        ctx = _make_spawn_ctx()
        dispatch.intercept_post_proc_spawn(ctx)

        # h1 failed with warning, h2 still called
        assert len(ctx.warnings) == 1
        h2.on_post_proc_spawn.assert_called_once_with(ctx)


# ======================================================================
# intercept_post_proc_terminate
# ======================================================================


class TestInterceptPostProcTerminate:
    """Tests for KernelDispatch.intercept_post_proc_terminate."""

    def test_calls_hooks(self):
        dispatch = KernelDispatch()
        hook = _make_hook()
        dispatch.register_intercept_process(hook)

        ctx = _make_terminate_ctx(reason="user-cancel", exit_code=-15)
        dispatch.intercept_post_proc_terminate(ctx)

        hook.on_post_proc_terminate.assert_called_once_with(ctx)

    def test_warnings_on_failure(self):
        dispatch = KernelDispatch()
        hook = _make_hook(fail_on="on_post_proc_terminate")
        dispatch.register_intercept_process(hook)

        ctx = _make_terminate_ctx()
        dispatch.intercept_post_proc_terminate(ctx)

        assert len(ctx.warnings) == 1
        assert "hook failed" in ctx.warnings[0].message

    def test_ctx_fields_preserved(self):
        dispatch = KernelDispatch()
        hook = _make_hook()
        dispatch.register_intercept_process(hook)

        ctx = _make_terminate_ctx(pid="pid-99", reason="oom", exit_code=137)
        dispatch.intercept_post_proc_terminate(ctx)

        passed_ctx = hook.on_post_proc_terminate.call_args[0][0]
        assert passed_ctx.pid == "pid-99"
        assert passed_ctx.reason == "oom"
        assert passed_ctx.exit_code == 137
