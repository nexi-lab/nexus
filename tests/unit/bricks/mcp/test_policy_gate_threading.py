"""PolicyGate threading through MCPConnectionManager and MCPService.

Issue #3790, Task 18 follow-up: ensure the gate is forwarded all the way
down to ``MCPMountManager`` so the SSRF-via-gate hook actually fires in
production. Without this wiring the hook in ``mount.py`` is dead code.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nexus.bricks.approvals.policy_gate import PolicyGate
from nexus.bricks.mcp.connection_manager import MCPConnectionManager
from nexus.bricks.mcp.mcp_service import MCPService


def test_connection_manager_forwards_policy_gate_to_mount_manager() -> None:
    """MCPConnectionManager(policy_gate=...) wires the gate into its mount manager."""
    gate = MagicMock(spec=PolicyGate)
    cm = MCPConnectionManager(filesystem=None, policy_gate=gate)
    # The gate is stored on both this manager and its embedded mount manager.
    assert cm._policy_gate is gate
    assert cm.mount_manager._policy_gate is gate


def test_connection_manager_default_policy_gate_is_none() -> None:
    """When no gate is passed, both managers fail-closed (gate=None)."""
    cm = MCPConnectionManager(filesystem=None)
    assert cm._policy_gate is None
    assert cm.mount_manager._policy_gate is None


def test_connection_manager_set_policy_gate_updates_mount_manager() -> None:
    """set_policy_gate() retroactively wires the gate into the existing mount manager."""
    cm = MCPConnectionManager(filesystem=None)
    assert cm.mount_manager._policy_gate is None

    gate = MagicMock(spec=PolicyGate)
    cm.set_policy_gate(gate)
    assert cm._policy_gate is gate
    assert cm.mount_manager._policy_gate is gate

    # Detaching the gate restores fail-closed.
    cm.set_policy_gate(None)
    assert cm._policy_gate is None
    assert cm.mount_manager._policy_gate is None


def test_mcp_service_stores_policy_gate() -> None:
    """MCPService(policy_gate=...) keeps the gate for forwarding into mount managers."""
    gate = MagicMock(spec=PolicyGate)
    svc = MCPService(filesystem=None, policy_gate=gate)
    assert svc._policy_gate is gate


def test_mcp_service_default_policy_gate_is_none() -> None:
    """No gate at construction time → fail-closed (gate=None)."""
    svc = MCPService(filesystem=None)
    assert svc._policy_gate is None


def test_mcp_service_set_policy_gate_updates_internal_state() -> None:
    """set_policy_gate() lets the lifespan wire the gate post-construction."""
    svc = MCPService(filesystem=None)
    assert svc._policy_gate is None

    gate = MagicMock(spec=PolicyGate)
    svc.set_policy_gate(gate)
    assert svc._policy_gate is gate

    svc.set_policy_gate(None)
    assert svc._policy_gate is None


def test_mcp_service_get_mount_manager_forwards_policy_gate() -> None:
    """_get_mcp_mount_manager() must forward the gate to every MCPMountManager it builds."""
    gate = MagicMock(spec=PolicyGate)
    fake_fs = MagicMock()
    svc = MCPService(filesystem=fake_fs, policy_gate=gate)

    manager = svc._get_mcp_mount_manager()
    assert manager._policy_gate is gate

    # Detach via setter — newly constructed managers reflect the new state.
    svc.set_policy_gate(None)
    manager2 = svc._get_mcp_mount_manager()
    assert manager2._policy_gate is None
