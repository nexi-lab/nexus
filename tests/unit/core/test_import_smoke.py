"""Dynamic import smoke test (Issue #2133).

Uses importlib.import_module() on key nexus.core and nexus.factory modules
to catch runtime import failures that AST analysis misses.
"""

from __future__ import annotations

import importlib

import pytest

_CORE_MODULES = [
    "nexus.core.config",
    "nexus.core.nexus_fs",
    "nexus.core.async_bridge",
    "nexus.core.protocols",
    "nexus.core.protocols.rebac_manager",
    "nexus.core.protocols.permission_enforcer",
    "nexus.core.protocols.entity_registry",
    "nexus.core.protocols.workspace_manager",
    "nexus.core.protocols.wirable_fs",
    "nexus.core.protocols.vfs_router",
    "nexus.core.protocols.vfs_core",
    "nexus.contracts.agent_utils",
]

_FACTORY_MODULES = [
    "nexus.factory._wired",
    "nexus.factory._kernel",
    "nexus.factory.orchestrator",
]


@pytest.mark.parametrize("module_name", _CORE_MODULES, ids=_CORE_MODULES)
def test_core_module_import(module_name: str) -> None:
    """Core modules should import without circular import errors."""
    mod = importlib.import_module(module_name)
    assert mod is not None


@pytest.mark.parametrize("module_name", _FACTORY_MODULES, ids=_FACTORY_MODULES)
def test_factory_module_import(module_name: str) -> None:
    """Factory modules should import without circular import errors."""
    mod = importlib.import_module(module_name)
    assert mod is not None
