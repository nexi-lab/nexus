"""Import isolation test for brick modules (Issue #2131, Phase 6.4).

Verifies that brick modules don't import forbidden core modules at
import time. Bricks must be self-contained with no dependencies on
nexus.core.nexus_fs or other bricks.
"""

import importlib
import sys

import pytest

# Brick modules to test
_BRICK_MODULES = [
    "nexus.bricks.delegation.models",
    "nexus.bricks.delegation.errors",
    "nexus.bricks.delegation.derivation",
    "nexus.bricks.reputation.errors",
    "nexus.bricks.reputation.reputation_math",
    "nexus.bricks.reputation.reputation_records",
    "nexus.bricks.snapshot.errors",
    "nexus.bricks.snapshot.registry",
    # ReBAC brick (Issue #2179)
    "nexus.bricks.rebac.rebac_tracing",
    "nexus.bricks.rebac.domain",
    "nexus.bricks.rebac.types",
    "nexus.bricks.rebac.circuit_breaker",
    "nexus.bricks.rebac.entity_registry",
    "nexus.bricks.rebac.memory_permission_enforcer",
    "nexus.bricks.rebac.permission_cache",
    "nexus.bricks.rebac.hotspot_detector",
    "nexus.bricks.rebac.namespace_manager",
    "nexus.bricks.rebac._path_utils",
]

# Forbidden modules that bricks must NOT pull in at import time
_FORBIDDEN_MODULES = [
    "nexus.core.nexus_fs",
    "nexus.server.telemetry",
    "nexus.server.app",
]


@pytest.mark.parametrize("brick_module", _BRICK_MODULES)
def test_brick_does_not_import_forbidden_modules(brick_module: str) -> None:
    """Verify that importing a brick module does not pull in forbidden core modules."""
    # Snapshot sys.modules before import
    pre_import = set(sys.modules.keys())

    # Import the brick module (may already be cached; reload to be safe)
    if brick_module in sys.modules:
        importlib.reload(sys.modules[brick_module])
    else:
        importlib.import_module(brick_module)

    # Check which new modules were loaded
    post_import = set(sys.modules.keys())
    newly_loaded = post_import - pre_import

    for forbidden in _FORBIDDEN_MODULES:
        assert forbidden not in newly_loaded, (
            f"Brick {brick_module} imported forbidden module {forbidden}. "
            f"Bricks must not depend on core NexusFS modules."
        )
