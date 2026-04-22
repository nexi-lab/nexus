"""RPC method discovery via @rpc_expose decorator scanning.

Extracted from fastapi_server.py (Issue #2131).  Scans NexusFS and any
additional service instances for methods decorated with ``@rpc_expose``
so that services can expose RPC endpoints without NexusFS delegation
boilerplate (Issue #2035, Follow-up 1).
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


def discover_exposed_methods(nexus_fs: "NexusFS", *additional_sources: Any) -> dict[str, Any]:
    """Discover all methods marked with @rpc_expose decorator.

    Scans NexusFS and any additional sources for methods decorated with
    @rpc_expose. This allows services to expose RPC methods directly
    without NexusFS delegation boilerplate (Issue #2035, Follow-up 1).

    Args:
        nexus_fs: The NexusFS kernel instance (always scanned).
        *additional_sources: Service instances to scan for @rpc_expose.
    """
    exposed: dict[str, Any] = {}

    for source in (nexus_fs, *additional_sources):
        if source is None:
            continue

        # service_lookup() returns raw instances — no unwrapping needed.
        scan_target = source

        source_name = type(scan_target).__name__
        for name in dir(scan_target):
            if name.startswith("_"):
                continue

            try:
                attr = getattr(scan_target, name)
                if callable(attr) and hasattr(attr, "_rpc_exposed"):
                    method_name = getattr(attr, "_rpc_name", name)
                    # Issue #2136: Block rpc_name bypass — skip private method names
                    if method_name.startswith("_"):
                        logger.warning(
                            "Skipping RPC method with private rpc_name: %s -> %s",
                            name,
                            method_name,
                        )
                        continue
                    if method_name in exposed:
                        logger.debug(
                            "RPC method %s from %s overrides previous source",
                            method_name,
                            source_name,
                        )
                    exposed[method_name] = attr
                    logger.debug("Discovered RPC method: %s (from %s)", method_name, source_name)
            except Exception:
                continue

    logger.info("Auto-discovered %d RPC methods", len(exposed))
    return exposed
