"""Tests for PolicyGate dependency wiring on the MCP app."""

from unittest.mock import MagicMock

from nexus.bricks.approvals.policy_gate import PolicyGate


def test_register_policy_gate_attaches_to_app_state():
    """register_policy_gate_dependency must attach gate to app.state.policy_gate."""
    from nexus.bricks.mcp.server import register_policy_gate_dependency

    app = MagicMock()
    gate = MagicMock(spec=PolicyGate)

    register_policy_gate_dependency(app, gate)

    assert app.state.policy_gate is gate
