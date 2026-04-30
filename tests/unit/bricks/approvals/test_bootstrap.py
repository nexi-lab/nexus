"""Bootstrap returns a working service + gate stack with feature flag respect."""

from unittest.mock import MagicMock

import pytest

from nexus.bricks.approvals.bootstrap import (
    build_approvals_stack,
    shutdown_approvals_stack,
)
from nexus.bricks.approvals.config import ApprovalConfig


@pytest.mark.asyncio
async def test_disabled_returns_no_gate():
    cfg = ApprovalConfig(enabled=False)
    stack = await build_approvals_stack(
        cfg,
        session_factory=MagicMock(),
        asyncpg_pool=MagicMock(),
    )
    assert stack.gate is None
    assert stack.service is None
    assert stack.config is cfg


@pytest.mark.asyncio
async def test_shutdown_no_op_on_disabled_stack():
    """shutdown_approvals_stack must be safe on a (None, None) stack."""
    cfg = ApprovalConfig(enabled=False)
    stack = await build_approvals_stack(
        cfg,
        session_factory=MagicMock(),
        asyncpg_pool=MagicMock(),
    )
    # Must not raise.
    await shutdown_approvals_stack(stack)


@pytest.mark.asyncio
async def test_enabled_returns_gate_and_service(monkeypatch):
    cfg = ApprovalConfig(enabled=True)

    started = {"called": False}

    class FakeService:
        async def start(self):
            started["called"] = True

        async def stop(self):
            started["called"] = False

    monkeypatch.setattr(
        "nexus.bricks.approvals.bootstrap.ApprovalService",
        lambda *a, **kw: FakeService(),
    )

    stack = await build_approvals_stack(
        cfg,
        session_factory=MagicMock(),
        asyncpg_pool=MagicMock(),
    )
    assert stack.gate is not None
    assert stack.service is not None
    assert started["called"] is True

    # Shutdown cycles the stop() hook.
    await shutdown_approvals_stack(stack)
    assert started["called"] is False
