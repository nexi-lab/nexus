"""Boot helper for REMOTE deployment profile — the ``mount -t nfs`` command.

Fills NexusFS kernel service slots with RemoteServiceProxy instances,
forwarding all method calls to the server via the transport-agnostic
``call_rpc`` callback.

The kernel runs its natural VFS pipeline (permission → route → backend →
metadata) identically to standalone/federation modes. Rust RemoteBackend
and RemoteMetastore (installed via ``sys_setattr(backend_type="remote")``)
proxy every operation to the server — the kernel is never bypassed.

Deployment-profile invariant: any distro ≥ kernel.
  REMOTE = kernel + remote services (RemoteServiceProxy for all slots).

Issue #1171: Service-layer RPC proxy for REMOTE profile.
Issue #844:  Part of NexusFS(profile=REMOTE) convergence.
Issue #1708: Uses coordinator.enlist() — same entry point as all profiles.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


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
