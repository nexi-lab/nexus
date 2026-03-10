"""Boot helper for REMOTE deployment profile — the ``mount -t nfs`` command.

Fills ServiceRegistry with RemoteServiceProxy instances, forwarding all
method calls to the server via the transport-agnostic ``call_rpc`` callback.

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


def _boot_remote_services(nfs: "NexusFS", call_rpc: Callable[..., Any]) -> None:
    """Wire RemoteServiceProxy instances into ServiceRegistry.

    Like ``mount -t nfs``: fills ServiceRegistry with RPC forwarders
    instead of local service implementations.

    Called by ``connect(profile=REMOTE)`` after NexusFS construction.

    Args:
        nfs: The NexusFS instance to wire services onto.
        call_rpc: Transport-agnostic RPC callback (today HTTP, future gRPC).
    """
    from nexus.remote.service_proxy import RemoteServiceProxy

    proxy = RemoteServiceProxy(call_rpc, service_name="universal")

    from nexus.factory.service_routing import _CANONICAL_NAMES, populate_service_registry

    wired_dict: dict[str, Any] = dict.fromkeys(_CANONICAL_NAMES.keys(), proxy)
    count = populate_service_registry(nfs._service_registry, wired_dict, is_remote=True)

    # BrickServices field not covered by WiredServices
    nfs.version_service = proxy

    logger.info(
        "REMOTE profile: registered %d services with RPC forwarders (kernel runs naturally)",
        count + 1,
    )
