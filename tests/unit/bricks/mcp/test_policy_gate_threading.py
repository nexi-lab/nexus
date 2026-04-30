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


# ---------------------------------------------------------------------------
# F4 (Issue #3790) — zone_id threading. Without these the gate hook would
# silently charge approvals to ROOT_ZONE_ID (cross-zone privilege issue).
# ---------------------------------------------------------------------------


def test_connection_manager_forwards_zone_id_to_mount_manager() -> None:
    """MCPConnectionManager(zone_id=...) wires the zone into its mount manager."""
    cm = MCPConnectionManager(filesystem=None, zone_id="zoneA")
    assert cm._zone_id == "zoneA"
    assert cm.mount_manager._zone_id == "zoneA"


def test_connection_manager_default_zone_id_is_none() -> None:
    """No zone at construction → fail-closed at the gate hook."""
    cm = MCPConnectionManager(filesystem=None)
    assert cm._zone_id is None
    assert cm.mount_manager._zone_id is None


def test_connection_manager_set_zone_updates_mount_manager() -> None:
    """set_zone() retroactively wires the zone into the existing mount manager."""
    cm = MCPConnectionManager(filesystem=None)
    cm.set_zone("zoneB")
    assert cm._zone_id == "zoneB"
    assert cm.mount_manager._zone_id == "zoneB"

    cm.set_zone(None)
    assert cm._zone_id is None
    assert cm.mount_manager._zone_id is None


def test_mcp_service_stores_zone_id() -> None:
    """MCPService(zone_id=...) keeps the zone for forwarding into mount managers."""
    svc = MCPService(filesystem=None, zone_id="z1")
    assert svc._zone_id == "z1"


def test_mcp_service_set_zone_updates_internal_state() -> None:
    svc = MCPService(filesystem=None)
    assert svc._zone_id is None
    svc.set_zone("z2")
    assert svc._zone_id == "z2"
    svc.set_zone(None)
    assert svc._zone_id is None


def test_mcp_service_get_mount_manager_forwards_zone_id() -> None:
    fake_fs = MagicMock()
    svc = MCPService(filesystem=fake_fs, zone_id="zX")
    mgr = svc._get_mcp_mount_manager()
    assert mgr._zone_id == "zX"

    svc.set_zone(None)
    mgr2 = svc._get_mcp_mount_manager()
    assert mgr2._zone_id is None
