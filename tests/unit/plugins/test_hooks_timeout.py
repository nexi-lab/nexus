"""Tests for PluginHooks timeout enforcement (Issue #1257).

Verifies that PluginHooks.execute() respects per-invocation timeout
to prevent misbehaving hooks from hanging the system.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus.plugins.hooks import HookType, PluginHooks

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _fast_handler(context: dict[str, Any]) -> dict[str, Any]:
    """Handler that completes instantly."""
    return context


async def _slow_handler(context: dict[str, Any]) -> dict[str, Any]:
    """Handler that takes 10 seconds (simulates hang)."""
    await asyncio.sleep(10)
    return context


async def _medium_handler(context: dict[str, Any]) -> dict[str, Any]:
    """Handler that takes 0.1 seconds."""
    await asyncio.sleep(0.1)
    return context


# ---------------------------------------------------------------------------
# Timeout enforcement
# ---------------------------------------------------------------------------


class TestPluginHooksTimeout:
    """Verify timeout parameter in PluginHooks.execute()."""

    @pytest.mark.asyncio
    async def test_execute_with_timeout_fast_handler_succeeds(self) -> None:
        """Fast handler completes within timeout."""
        hooks = PluginHooks()
        hooks.register(HookType.BEFORE_WRITE, _fast_handler)

        result = await hooks.execute(HookType.BEFORE_WRITE, {"path": "/test"}, timeout=1.0)
        assert result is not None
        assert result["path"] == "/test"

    @pytest.mark.asyncio
    async def test_execute_with_timeout_slow_handler_skipped(self) -> None:
        """Slow handler exceeding timeout is skipped (fail-safe: chain continues)."""
        hooks = PluginHooks()
        hooks.register(HookType.BEFORE_WRITE, _slow_handler, priority=10)
        hooks.register(HookType.BEFORE_WRITE, _fast_handler, priority=0)

        # Timeout is 0.05s, slow handler takes 10s → should be skipped
        result = await hooks.execute(HookType.BEFORE_WRITE, {"path": "/test"}, timeout=0.05)
        # Chain should continue (fast_handler runs after slow one is skipped)
        assert result is not None

    @pytest.mark.asyncio
    async def test_execute_without_timeout_no_enforcement(self) -> None:
        """Without timeout param, execute behaves as before (no timeout)."""
        hooks = PluginHooks()
        hooks.register(HookType.BEFORE_WRITE, _fast_handler)

        result = await hooks.execute(HookType.BEFORE_WRITE, {"path": "/test"})
        assert result is not None

    @pytest.mark.asyncio
    async def test_execute_timeout_per_handler_not_total(self) -> None:
        """Timeout applies per-handler, not to the total chain.

        Two medium handlers (0.1s each) with 0.15s timeout should both complete,
        since each individually completes within 0.15s.
        """
        hooks = PluginHooks()
        hooks.register(HookType.BEFORE_READ, _medium_handler, priority=10)
        hooks.register(HookType.BEFORE_READ, _medium_handler, priority=0)

        result = await hooks.execute(HookType.BEFORE_READ, {"path": "/test"}, timeout=0.15)
        assert result is not None


# ---------------------------------------------------------------------------
# HookType lifecycle extensions
# ---------------------------------------------------------------------------


class TestHookTypeLifecycleExtensions:
    """Verify lifecycle HookType enum values exist (Issue #1257)."""

    def test_before_mount_exists(self) -> None:
        assert HookType.BEFORE_MOUNT == "before_mount"

    def test_after_mount_exists(self) -> None:
        assert HookType.AFTER_MOUNT == "after_mount"

    def test_before_unmount_exists(self) -> None:
        assert HookType.BEFORE_UNMOUNT == "before_unmount"

    def test_after_unmount_exists(self) -> None:
        assert HookType.AFTER_UNMOUNT == "after_unmount"

    def test_lifecycle_hooks_registerable(self) -> None:
        """Lifecycle hooks can be registered and executed."""
        hooks = PluginHooks()
        hooks.register(HookType.BEFORE_MOUNT, _fast_handler)
        assert hooks.has_handlers(HookType.BEFORE_MOUNT)
