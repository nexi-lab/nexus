"""Dynamic import smoke test (Issue #2133).

Uses importlib.import_module() on key nexus.core and nexus.factory modules
to catch runtime import failures that AST analysis misses.
"""

import importlib

import pytest

_CORE_MODULES = [
    "nexus.core.config",
    "nexus.core.nexus_fs",
    "nexus.bricks.rebac.async_bridge",
    "nexus.core.protocols",
    "nexus.core.protocols.vfs_router",
    "nexus.core.protocols.vfs_core",
    # Moved to services/protocols/ per #2359:
    "nexus.contracts.protocols.rebac",
    "nexus.contracts.protocols.permission_enforcer",
    "nexus.contracts.protocols.entity_registry",
    "nexus.contracts.protocols.workspace_manager",
    # Moved to contracts/ per #2359:
    "nexus.contracts.describable",
    "nexus.contracts.wirable_fs",
    "nexus.contracts.agent_utils",
    # Config and SDK decoupled from optional bricks (#3230):
    "nexus.config",
    "nexus.sdk",
    "nexus.contracts.oauth_types",
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
