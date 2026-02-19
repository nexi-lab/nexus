"""RPC exposure decorator — backward compatibility re-export.

Canonical home is now ``nexus.contracts.rpc``.  This module re-exports
``rpc_expose`` so existing ``from nexus.core.rpc_decorator import rpc_expose``
imports continue to work.
"""

from nexus.contracts.rpc import rpc_expose

__all__ = ["rpc_expose"]
