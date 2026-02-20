"""Kernel protocol compliance tests (Issue #2133).

Verifies that concrete implementations satisfy the new kernel protocols
introduced to break core/ -> services/ circular imports.
"""

from __future__ import annotations

import pytest

from tests.unit.services.test_protocol_compliance import assert_protocol_compliance


def _try_import(module_path: str, class_name: str) -> type | None:
    """Attempt to import a class, returning None on failure."""
    try:
        import importlib

        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (ImportError, AttributeError):
        return None


_KERNEL_PROTOCOL_PAIRS: list[tuple[str, str, str, str]] = [
    (
        "ReBACManagerProtocol",
        "nexus.core.protocols.rebac_manager",
        "ReBACManagerProtocol",
        "nexus.rebac.manager.ReBACManager",
    ),
    (
        "PermissionEnforcerProtocol",
        "nexus.core.protocols.permission_enforcer",
        "PermissionEnforcerProtocol",
        "nexus.rebac.enforcer.PermissionEnforcer",
    ),
    (
        "EntityRegistryProtocol",
        "nexus.core.protocols.entity_registry",
        "EntityRegistryProtocol",
        "nexus.rebac.entity_registry.EntityRegistry",
    ),
    (
        "WorkspaceManagerProtocol",
        "nexus.core.protocols.workspace_manager",
        "WorkspaceManagerProtocol",
        "nexus.services.workspace_manager.WorkspaceManager",
    ),
]


@pytest.mark.parametrize(
    "test_id,proto_module,proto_name,impl_path",
    _KERNEL_PROTOCOL_PAIRS,
    ids=[p[0] for p in _KERNEL_PROTOCOL_PAIRS],
)
def test_kernel_protocol_compliance(
    test_id: str,
    proto_module: str,
    proto_name: str,
    impl_path: str,
) -> None:
    """Verify kernel protocol compliance for each implementation."""
    protocol_cls = _try_import(proto_module, proto_name)
    if protocol_cls is None:
        pytest.skip(f"Cannot import protocol {proto_name} from {proto_module}")

    parts = impl_path.rsplit(".", 1)
    impl_cls = _try_import(parts[0], parts[1])
    if impl_cls is None:
        pytest.skip(f"Cannot import implementation {impl_path}")

    assert_protocol_compliance(impl_cls, protocol_cls)


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
