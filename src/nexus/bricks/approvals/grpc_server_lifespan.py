"""Process-local Python gRPC server hosting ApprovalsV1.

The Rust-native VFS gRPC server on ``:2028`` does not accept Python
``add_*Servicer_to_server`` calls, so the approvals brick spins up its
own ``grpc.aio.Server`` on a separate port (default ``:2029``). The
public surface is two coroutines:

  - ``start_grpc_server(service, auth, port, host)``: build the server,
    register the servicer, bind, ``await server.start()``, and return the
    handle.
  - ``stop_grpc_server(server, grace_seconds)``: ``await server.stop(...)``
    with a grace period so in-flight RPCs can finish.

The lifespan code in ``src/nexus/server/lifespan/approvals.py`` owns the
admin-token wiring and the feature-flag gate; this module is a thin shim
focused on the gRPC-server boilerplate so it stays reusable from tests.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import grpc.aio

from nexus.bricks.approvals.grpc_server import ApprovalsServicer
from nexus.grpc.approvals import approvals_pb2_grpc

if TYPE_CHECKING:
    from nexus.bricks.approvals.grpc_server import CapabilityAuth
    from nexus.bricks.approvals.service import ApprovalService

logger = logging.getLogger(__name__)


async def start_grpc_server(
    service: "ApprovalService",
    auth: "CapabilityAuth",
    port: int,
    host: str = "127.0.0.1",
) -> grpc.aio.Server:
    """Bind a ``grpc.aio.Server`` carrying ApprovalsV1 and start it.

    Args:
        service: An already-started ``ApprovalService`` instance.
        auth: A ``CapabilityAuth`` implementation (e.g.
            ``BearerTokenCapabilityAuth``) — every RPC will call
            ``await auth.authorize(context, capability, zone_id)``
            (or ``check_capability`` for Get/Decide/Cancel which need
            a zone-scoped denial folded into NOT_FOUND).
        port: TCP port to bind. Use ``0`` to let the kernel pick a free
            port — read it back via ``server.add_insecure_port`` is
            internalized here, but for ad-hoc binds prefer the test
            helper that constructs the server directly.
        host: Bind address. Defaults to loopback (``127.0.0.1``); pass
            ``0.0.0.0`` only when the caller explicitly wants the server
            reachable off-host.

    Returns:
        The started ``grpc.aio.Server``. Caller MUST eventually call
        ``stop_grpc_server`` (or ``server.stop(...)`` directly).
    """
    server = grpc.aio.server()
    servicer = ApprovalsServicer(service, auth=auth)
    approvals_pb2_grpc.add_ApprovalsV1Servicer_to_server(servicer, server)

    bind_addr = f"{host}:{port}"
    bound_port = server.add_insecure_port(bind_addr)
    if bound_port == 0:
        # add_insecure_port returns 0 on bind failure (per gRPC docs).
        # Surface that as a real exception rather than silently starting
        # a dangling server.
        raise RuntimeError(f"failed to bind approvals gRPC server to {bind_addr}")

    await server.start()
    logger.info(
        "[APPROVALS] python gRPC server started on %s:%d",
        host,
        bound_port,
    )
    return server


async def stop_grpc_server(
    server: grpc.aio.Server,
    grace_seconds: float = 5.0,
) -> None:
    """Gracefully stop the gRPC server, allowing in-flight RPCs to finish.

    Args:
        server: The handle returned by ``start_grpc_server``.
        grace_seconds: How long to let in-flight RPCs finish before a
            forced shutdown. ``5.0`` matches the pattern used by the Rust
            VFS server; tune lower in tests if needed.
    """
    try:
        await server.stop(grace=grace_seconds)
    except Exception as e:
        # Don't let shutdown blow up the FastAPI lifespan path — the next
        # process will rebind the port anyway.
        logger.warning("[APPROVALS] gRPC server stop failed: %s", e, exc_info=True)
