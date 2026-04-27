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

    def _coerce_bytes(result: Any) -> bytes:
        if isinstance(result, bytes):
            return result
        if isinstance(result, bytearray):
            return bytes(result)
        if isinstance(result, memoryview):
            return result.tobytes()
        if isinstance(result, str):
            return result.encode("utf-8")
        return bytes(result)

    def _write_params(
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        if isinstance(buf, str):
            buf = buf.encode("utf-8")
        if count is not None:
            buf = buf[:count]
        params: dict[str, Any] = {"path": path, "buf": buf}
        if offset:
            params["offset"] = offset
        return params

    def _remote_sys_read(
        _self: Any,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: Any = None,  # noqa: ARG001
    ) -> bytes:
        # NexusFS methods are sync (Phase 7). transport calls are blocking gRPC.
        # Use generic Call for both full and range reads so the server applies
        # the same auth, zone scoping, and permission hooks as HTTP RPC.
        params: dict[str, Any] = {"path": path}
        if count is not None:
            params["count"] = count
        if offset:
            params["offset"] = offset
        result = transport.call_rpc("sys_read", params)
        return _coerce_bytes(result)

    def _remote_sys_write(
        _self: Any,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: Any = None,  # noqa: ARG001
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            transport.call_rpc("sys_write", _write_params(path, buf, count=count, offset=offset)),
        )

    def _remote_write(
        _self: Any,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: Any = None,  # noqa: ARG001
        ttl: float | None = None,  # noqa: ARG001
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            transport.call_rpc("write", _write_params(path, buf, count=count, offset=offset)),
        )

    def _remote_sys_rename(
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

    def _remote_sys_readdir(
        _self: Any,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        *,
        context: Any = None,  # noqa: ARG001
        limit: int | None = None,
        cursor: str | None = None,
        **_: Any,
    ) -> Any:
        # The in-memory Redb metastore is empty in REMOTE profile, so
        # sys_readdir returns [] for all paths. Forward to server via RPC.
        params: dict[str, Any] = {"path": path, "recursive": recursive, "details": details}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        result: Any = transport.call_rpc("sys_readdir", params)
        if isinstance(result, dict):
            if limit is not None:
                # Paginated call: convert RPC envelope to PaginatedResult so
                # callers get the same contract as the local kernel.
                from nexus.core.pagination import PaginatedResult

                return PaginatedResult(
                    items=result.get("files", []),
                    next_cursor=result.get("next_cursor"),
                    has_more=result.get("has_more", False),
                    total_count=result.get("total_count"),
                )
            return result.get("files", [])
        return result if isinstance(result, list) else []

    cast(Any, nfs).sys_read = types.MethodType(_remote_sys_read, nfs)
    cast(Any, nfs).sys_write = types.MethodType(_remote_sys_write, nfs)
    cast(Any, nfs).write = types.MethodType(_remote_write, nfs)
    cast(Any, nfs).sys_rename = types.MethodType(_remote_sys_rename, nfs)
    cast(Any, nfs).sys_readdir = types.MethodType(_remote_sys_readdir, nfs)


def _boot_remote_services(nfs: "NexusFS", call_rpc: Callable[..., Any]) -> None:
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
    enlist_wired_services(nfs, wired_dict)

    # version_service — enlist into ServiceRegistry
    nfs.sys_setattr("/__sys__/services/version_service", service=proxy)

    logger.info(
        "REMOTE profile: wired %d service slots with RPC forwarders (kernel runs naturally)",
        len(_CANONICAL_NAMES) + 1,
    )
