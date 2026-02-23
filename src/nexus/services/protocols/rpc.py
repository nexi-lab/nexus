"""RPC exposure decorator — services/protocols re-export (Issue #2035).

Canonical implementation lives in ``nexus.lib.rpc_decorator``.
This re-export allows bricks to import ``rpc_expose`` from
``nexus.services.protocols.rpc`` without depending on nexus.lib directly.

Both import paths are equivalent::

    from nexus.lib.rpc_decorator import rpc_expose       # canonical
    from nexus.services.protocols.rpc import rpc_expose   # brick-safe alias
"""

from nexus.lib.rpc_decorator import rpc_expose

__all__ = ["rpc_expose"]
