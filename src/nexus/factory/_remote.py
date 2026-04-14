"""Boot helper for REMOTE deployment profile — the ``mount -t nfs`` command.

Fills NexusFS kernel service slots with RemoteServiceProxy instances,
forwarding all method calls to the server via the transport-agnostic
``call_rpc`` callback.

The kernel runs its natural VFS pipeline (permission → route → backend →
metadata) identically to standalone/federation modes.  RemoteMetastore and
RemoteBackend are complete ABC implementations that proxy every operation
to the server — the kernel is never bypassed or hollowed out.

Deployment-profile invariant: any distro ≥ kernel.
  REMOTE = kernel + remote services (RemoteServiceProxy for all slots).

Issue #1171: Service-layer RPC proxy for REMOTE profile.
Issue #844:  Part of NexusFS(profile=REMOTE) convergence.
Issue #1708: Uses coordinator.enlist() — same entry point as all profiles.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS
    from nexus.remote.rpc_transport import RPCTransport

logger = logging.getLogger(__name__)


def install_remote_kernel_rpc_overrides(nfs: "NexusFS", transport: "RPCTransport") -> None:
    """Route kernel ops that require server-side hooks through direct RPC.

    The Rust kernel's internal Redb metastore is empty in the REMOTE profile
    (no local data_dir), so kernel.sys_read / kernel.sys_write return hit=False
    for all paths. Override these to call the authoritative server RPCs directly.

    sys_rename is also overridden because the client-side kernel emulates it
    as metadata put/delete, bypassing server-side post-rename hooks.
    """
    import types

    async def _remote_sys_read(
        _self: Any,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: Any = None,  # noqa: ARG001
    ) -> bytes:
        # transport.read_file() is a synchronous blocking gRPC call — run it in a
        # thread so it never stalls the asyncio event loop under slow/lossy networks.
        import asyncio as _asyncio

        if offset or count is not None:
            # Range read: use the JSON Call RPC so offset/count are forwarded to
            # nexus_fs.sys_read() server-side. The typed read_file proto has no
            # offset/count fields, so it can only do full-file reads.
            params: dict[str, Any] = {"path": path, "offset": offset}
            if count is not None:
                params["count"] = count
            result = await _asyncio.to_thread(transport.call_rpc, "read", params)
            # call_rpc + decode_rpc_message already unwraps {"__type__":"bytes","data":...}
            return result if isinstance(result, bytes) else bytes(result)
        # Full-file read: use the efficient typed ReadRequest proto (no JSON/base64 overhead).
        return await _asyncio.to_thread(transport.read_file, path)

    async def _remote_sys_rename(
        _self: Any,
        old_path: str,
        new_path: str,
        *,
        force: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        transport.call_rpc(
            "sys_rename", {"old_path": old_path, "new_path": new_path, "force": force}
        )
        return {}

    cast(Any, nfs).sys_read = types.MethodType(_remote_sys_read, nfs)
    cast(Any, nfs).sys_rename = types.MethodType(_remote_sys_rename, nfs)


async def _boot_remote_services(nfs: "NexusFS", call_rpc: Callable[..., Any]) -> None:
    """Wire RemoteServiceProxy instances via coordinator.enlist().

    Like ``mount -t nfs``: fills VFS service slots with RPC forwarders
    instead of local service implementations.

    Called by ``connect(profile="remote")`` after NexusFS construction.

    Issue #1708: Coordinator is always created (BLM=None for REMOTE).
    Single entry point — no fallback to register_wired_services().

    Args:
        nfs: The NexusFS instance to wire services onto.
        call_rpc: Transport-agnostic RPC callback (today HTTP, future gRPC).
    """
    from nexus.remote.service_proxy import RemoteServiceProxy

    proxy = RemoteServiceProxy(call_rpc, service_name="universal")

    # Issue #1708: ServiceRegistry now has integrated lifecycle.
    # REMOTE profile: no BLM needed.

    # Enlist all canonical services via kernel (Issue #1708)
    from nexus.factory.service_routing import _CANONICAL_NAMES, enlist_wired_services

    wired_dict: dict[str, Any] = dict.fromkeys(_CANONICAL_NAMES.keys(), proxy)
    await enlist_wired_services(nfs, wired_dict)

    # version_service — enlist into ServiceRegistry
    nfs.sys_setattr("/__sys__/services/version_service", service=proxy)

    logger.info(
        "REMOTE profile: wired %d service slots with RPC forwarders (kernel runs naturally)",
        len(_CANONICAL_NAMES) + 1,
    )
