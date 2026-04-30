"""F4 (Issue #3790) — MCPMountManager.zone_id binding for SSRF gate hook.

Without a bound zone the gate hook MUST NOT fall back to ROOT_ZONE_ID;
it must fail closed (return False so the caller re-raises ``SSRFBlocked``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.approvals.models import Decision
from nexus.bricks.approvals.policy_gate import PolicyGate
from nexus.bricks.mcp.models import MCPMount
from nexus.bricks.mcp.mount import MCPMountManager


def _mount() -> MCPMount:
    return MCPMount(
        name="github",
        description="t",
        transport="sse",
        url="https://api.github.com/sse",
    )


@pytest.mark.asyncio
async def test_ssrf_gate_fail_closed_when_zone_unbound() -> None:
    """No zone_id → never call gate.check, return False (fail closed)."""
    gate = MagicMock(spec=PolicyGate)
    gate.check = AsyncMock(return_value=Decision.APPROVED)

    mgr = MCPMountManager(filesystem=None, policy_gate=gate, zone_id=None)
    mount = _mount()

    allowed = await mgr._ssrf_blocked_via_gate(
        mount, "https://api.github.com/sse", "mcp_mount_connect"
    )
    assert allowed is False
    gate.check.assert_not_awaited()


@pytest.mark.asyncio
async def test_ssrf_gate_uses_bound_zone_id() -> None:
    """zone_id wired → gate.check is called with that zone (not ROOT)."""
    gate = MagicMock(spec=PolicyGate)
    gate.check = AsyncMock(return_value=Decision.APPROVED)

    mgr = MCPMountManager(filesystem=None, policy_gate=gate, zone_id="zoneA")
    mount = _mount()

    allowed = await mgr._ssrf_blocked_via_gate(
        mount, "https://api.github.com/sse", "mcp_mount_connect"
    )
    assert allowed is True
    gate.check.assert_awaited_once()
    kwargs = gate.check.await_args.kwargs
    assert kwargs["zone_id"] == "zoneA"


@pytest.mark.asyncio
async def test_set_zone_after_construction_takes_effect() -> None:
    gate = MagicMock(spec=PolicyGate)
    gate.check = AsyncMock(return_value=Decision.APPROVED)

    mgr = MCPMountManager(filesystem=None, policy_gate=gate, zone_id=None)
    mount = _mount()

    # Pre-bind: fail closed.
    assert await mgr._ssrf_blocked_via_gate(mount, "https://x.example/sse", "op") is False
    gate.check.assert_not_awaited()

    mgr.set_zone("zoneB")
    assert await mgr._ssrf_blocked_via_gate(mount, "https://x.example/sse", "op") is True
    gate.check.assert_awaited_once()
    assert gate.check.await_args.kwargs["zone_id"] == "zoneB"
