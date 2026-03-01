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
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

# All fields accepted by NexusFS._bind_wired_services() (dict path).
# Derived from nexus_fs.py:290-326.
_WIRED_FIELDS: list[str] = [
    # Services
    "rebac_service",
    "mount_service",
    "gateway",
    "mount_core_service",
    "sync_service",
    "sync_job_service",
    "mount_persist_service",
    "mcp_service",
    "llm_service",
    "oauth_service",
    "search_service",
    "share_link_service",
    "events_service",
    "workspace_rpc_service",
    "agent_rpc_service",
    "user_provisioning_service",
    "sandbox_rpc_service",
    "metadata_export_service",
    "descendant_checker",
    "memory_provider",
]


def _boot_remote_services(nfs: "NexusFS", call_rpc: Callable[..., Any]) -> None:
    """Wire RemoteServiceProxy instances as all service attributes.

    Like ``mount -t nfs``: fills VFS service slots with RPC forwarders
    instead of local service implementations.

    Called by ``connect(mode="remote")`` after NexusFS construction.

    Args:
        nfs: The NexusFS instance to wire services onto.
        call_rpc: Transport-agnostic RPC callback (today HTTP, future gRPC).
    """
    from nexus.remote.service_proxy import RemoteServiceProxy

    proxy = RemoteServiceProxy(call_rpc, service_name="universal")

    # Fill all wired service slots via _bind_wired_services (dict path)
    wired_dict: dict[str, Any] = dict.fromkeys(_WIRED_FIELDS, proxy)
    nfs._bind_wired_services(wired_dict)

    # BrickServices field not covered by WiredServices
    nfs.version_service = proxy

    logger.info(
        "REMOTE profile: wired %d service slots with RPC forwarders (kernel runs naturally)",
        len(_WIRED_FIELDS) + 1,
    )
