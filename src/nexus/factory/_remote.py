"""Remote profile filesystem binding and service registration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS
    from nexus.remote.rpc_transport import RPCTransport

logger = logging.getLogger(__name__)


def wire_remote_filesystem(nfs: "NexusFS", transport: "RPCTransport") -> None:
    """Bind filesystem operations to the authoritative daemon's typed RPCs."""
    from nexus.remote.vfs_client import RemoteFilesystemClient

    client = RemoteFilesystemClient(transport)
    for name in (
        "sys_read",
        "sys_write",
        "sys_stat",
        "write",
        "sys_rename",
        "sys_unlink",
        "mkdir",
        "rmdir",
        "sys_readdir",
    ):
        setattr(nfs, name, getattr(client, name))


def _boot_remote_services(nfs: "NexusFS", call_rpc: Callable[..., Any]) -> None:
    """Wire RemoteServiceProxy instances via coordinator.enlist().

    Like ``mount -t nfs``: fills VFS service slots with RPC forwarders
    instead of local service implementations.

    Called by ``connect(profile="remote")`` after NexusFS construction.

    Issue #1708: Coordinator is always created (BLM=None for REMOTE).
    Single entry point — no fallback to register_wired_services().

    Args:
        nfs: The NexusFS instance to wire services onto.
        call_rpc: Service RPC callback on the filesystem transport.
    """
    from nexus.remote.service_proxy import RemoteServiceProxy

    proxy = RemoteServiceProxy(call_rpc, service_name="universal")
    for method in (
        "glob",
        "grep",
        "semantic_search",
        "semantic_search_index",
        "semantic_search_stats",
    ):
        setattr(nfs, method, getattr(proxy, method))

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
