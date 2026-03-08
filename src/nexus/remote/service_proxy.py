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

from nexus.remote.rpc_proxy import RPCProxyBase

logger = logging.getLogger(__name__)


class RemoteServiceProxy(RPCProxyBase):
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

    __slots__ = ("_call_rpc_cb", "_service_name")

    def __init__(
        self,
        call_rpc: Callable[..., Any],
        service_name: str = "",
    ) -> None:
        # Use object.__setattr__ to avoid triggering our __getattr__
        object.__setattr__(self, "_call_rpc_cb", call_rpc)
        object.__setattr__(self, "_service_name", service_name)

    def _call_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> Any:
        """Call RPC callback. Satisfies RPCProxyBase interface."""
        # Use object.__getattribute__ to avoid __getattr__ loops
        call_rpc = object.__getattribute__(self, "_call_rpc_cb")
        return call_rpc(method, params, read_timeout=read_timeout)

    def __getattr__(self, name: str) -> Any:
        """Return an RPC forwarder for any public method access.

        Private/dunder attributes raise AttributeError immediately so
        Python internals (pickle, copy, repr) don't accidentally trigger
        RPC calls.
        """
        if name.startswith("_"):
            raise AttributeError(name)

        from nexus.remote.method_registry import METHOD_REGISTRY

        spec = METHOD_REGISTRY.get(name)

        # Server exposes async @rpc_expose methods; client-side sync
        # wrappers append "_sync" — strip suffix for RPC dispatch.
        rpc_name = name
        if rpc_name.endswith("_sync"):
            rpc_name = rpc_name[:-5]
            # Update spec if it was found under the original name
            if spec:
                from dataclasses import replace

                spec = replace(spec, rpc_name=rpc_name)

        def _proxy(*args: Any, **kwargs: Any) -> Any:
            return self._dispatch_rpc(name, spec, args, kwargs)

        # Preserve method name for debugging
        _proxy.__name__ = name
        _proxy.__qualname__ = f"RemoteServiceProxy.{name}"
        return _proxy

    def __repr__(self) -> str:
        name = object.__getattribute__(self, "_service_name")
        return f"<RemoteServiceProxy({name or 'universal'})>"
