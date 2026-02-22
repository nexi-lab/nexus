"""Kernel protocol compliance tests (Issue #2133).

Issue #2359: Kernel protocol compliance tests for EntityRegistry,
PermissionEnforcer, ReBACManager, and WorkspaceManager have been moved to
tests/unit/services/test_protocol_compliance.py (their protocols now live
in services/protocols/).

Remaining test: WiredServices frozen dataclass validation.
"""

import pytest


def test_wired_services_is_frozen_dataclass() -> None:
    """WiredServices should be a frozen dataclass with expected fields."""
    import dataclasses

    from nexus.core.config import WiredServices

    assert dataclasses.is_dataclass(WiredServices)

    ws = WiredServices()
    # Frozen — assignment should raise
    with pytest.raises(dataclasses.FrozenInstanceError):
        ws.rebac_service = "nope"

    # All 17 fields should default to None
    for field in dataclasses.fields(ws):
        assert getattr(ws, field.name) is None, f"{field.name} should default to None"
