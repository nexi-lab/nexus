"""Universal service proxy for REMOTE deployment profile.

Fills kernel service slots with RPC forwarders — like an NFS client module
filling VFS inode_operations with RPC stubs. Any method call is forwarded
to the server via the transport-agnostic ``call_rpc`` callback.

Works with NexusFS.__getattr__ dispatch (Issue #2033):
    nx.workspace_snapshot(...)
      → self._workspace_rpc_service.workspace_snapshot(...)
      → proxy.__getattr__("workspace_snapshot")
      → proxy._call_rpc("workspace_snapshot", kwargs)

Issue #1171: Service-layer RPC proxy for REMOTE profile.
Issue #844:  Part of NexusFS(profile=REMOTE) convergence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class RemoteServiceProxy:
    """Universal RPC proxy injected as every service attribute in REMOTE mode.

    A single instance fills all 25+ service slots on NexusFS. The proxy
    intercepts any method call via ``__getattr__`` and forwards it to
    the server using the ``call_rpc`` callback.

    The proxy doesn't need to know which service it stands in for —
    method name dispatch is handled server-side by the RPC dispatch table.

    Args:
        call_rpc: Transport-agnostic RPC callback. Today this is
            ``RemoteBackend._call_rpc`` (HTTP/JSON-RPC); the callable
            interface allows future gRPC transport (Task #1133) with
            zero proxy code changes.
        service_name: Optional label for debug logging (e.g. "universal").
    """

    __slots__ = ("_call_rpc", "_service_name")

    def __init__(
        self,
        call_rpc: Callable[..., Any],
        service_name: str = "",
    ) -> None:
        # Use object.__setattr__ to avoid triggering our __getattr__
        object.__setattr__(self, "_call_rpc", call_rpc)
        object.__setattr__(self, "_service_name", service_name)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """Return an RPC forwarder for any public method access.

        Private/dunder attributes raise AttributeError immediately so
        Python internals (pickle, copy, repr) don't accidentally trigger
        RPC calls.
        """
        if name.startswith("_"):
            raise AttributeError(name)

        # Lazy import to avoid circular dependency at module load
        from nexus.remote.rpc_proxy import RPCProxyBase

        def rpc_forwarder(*args: Any, **kwargs: Any) -> Any:
            # Map positional args to keyword args using ABC signature
            if args:
                param_names = RPCProxyBase._get_param_names(name)
                for i, val in enumerate(args):
                    if i < len(param_names):
                        kwargs[param_names[i]] = val

            # Strip context params (handled server-side via auth headers)
            kwargs.pop("context", None)
            kwargs.pop("_context", None)

            return self._call_rpc(name, kwargs or None)

        # Preserve method name for debugging
        rpc_forwarder.__name__ = name
        rpc_forwarder.__qualname__ = f"RemoteServiceProxy.{name}"
        return rpc_forwarder

    def __repr__(self) -> str:
        name = object.__getattribute__(self, "_service_name")
        return f"<RemoteServiceProxy({name or 'universal'})>"
