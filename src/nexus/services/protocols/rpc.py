"""RPC exposure decorator — services/protocols re-export (Issue #2035).

Canonical implementation lives in ``nexus.core.rpc_decorator``.
This re-export allows bricks to import ``rpc_expose`` from
``nexus.services.protocols.rpc`` without depending on nexus.core.

Both import paths are equivalent::

    from nexus.core.rpc_decorator import rpc_expose      # original
    from nexus.services.protocols.rpc import rpc_expose   # brick-safe
"""

from nexus.core.rpc_decorator import rpc_expose

__all__ = ["rpc_expose"]
