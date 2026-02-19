"""Unit tests for _rust_available property — zone isolation safety guard.

Covers Issue 11A: Verifying the FUSE zone isolation guard cannot be bypassed.
The _rust_available property MUST return False when a namespace context is
present, preventing zone-unaware Rust delegation for agent mounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest


@dataclass
class FakeOperationContext:
    """Minimal OperationContext for testing."""

    agent_id: str = "agent_1"
    zone_id: str = "zone_alpha"


@pytest.fixture()
def make_ops():
    """Factory to create NexusFUSEOperations with controlled attributes.

    After the ops/ decomposition (Issue #2079), _use_rust and _rust_client are
    properties proxying to _ctx, so we create a minimal FUSESharedContext.
    """

    def _make(*, use_rust: bool = False, rust_client: Any = None, context: Any = None):
        from nexus.fuse.operations import NexusFUSEOperations
        from nexus.fuse.ops._shared import FUSESharedContext

        mock_fs = MagicMock()
        mock_fs.list_mounts.return_value = []

        ops = NexusFUSEOperations.__new__(NexusFUSEOperations)
        ops._ctx = FUSESharedContext(
            nexus_fs=mock_fs,
            mode=MagicMock(),
            context=context,
            namespace_manager=None,
            cache=MagicMock(),
            local_disk_cache=None,
            readahead=None,
            rust_client=rust_client,
            use_rust=use_rust,
            events=MagicMock(),
            cache_config={},
            dir_cache=MagicMock(),
        )
        ops._context = context
        return ops

    return _make


class TestRustAvailableGuard:
    """Tests for _rust_available property behavior."""

    def test_all_conditions_met_returns_true(self, make_ops):
        """use_rust=True, client connected, no context -> True (global mount)."""
        ops = make_ops(use_rust=True, rust_client=MagicMock(), context=None)
        assert ops._rust_available is True

    def test_context_present_returns_false(self, make_ops):
        """use_rust=True, client connected, context present -> False (CRITICAL).

        This is the security-critical test. When a namespace context is present
        (agent mount with ReBAC zone isolation), Rust delegation MUST be disabled
        because the Rust daemon has no per-request zone_id support.
        """
        agent_context = FakeOperationContext(agent_id="agent_1", zone_id="zone_alpha")
        ops = make_ops(use_rust=True, rust_client=MagicMock(), context=agent_context)
        assert ops._rust_available is False

    def test_use_rust_false_returns_false(self, make_ops):
        """use_rust=False -> False regardless of other conditions."""
        ops = make_ops(use_rust=False, rust_client=MagicMock(), context=None)
        assert ops._rust_available is False

    def test_no_client_returns_false(self, make_ops):
        """Rust client is None -> False."""
        ops = make_ops(use_rust=True, rust_client=None, context=None)
        assert ops._rust_available is False

    def test_all_false_returns_false(self, make_ops):
        """All conditions false -> False."""
        ops = make_ops(use_rust=False, rust_client=None, context=FakeOperationContext())
        assert ops._rust_available is False
