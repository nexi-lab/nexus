"""gRPC server lifecycle — start/stop for the unified Nexus gRPC server (#1249).

Manages a single ``grpc.aio.server()`` hosting ``NexusVFSService``.
The server is disabled by default (port 0) and enabled by setting
``NEXUS_GRPC_PORT`` to a non-zero port number.

A2A agent messaging is delivered over VFS (A2A-over-VFS), so there is
no separate A2A gRPC servicer — all traffic flows through a single port.
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


async def startup_grpc(app: "FastAPI", _svc: "LifespanServices") -> list[asyncio.Task]:
    """Start the gRPC server if configured."""
    port = int(os.environ.get("NEXUS_GRPC_PORT", "0"))
    if not port:
        return []

    nexus_fs = getattr(app.state, "nexus_fs", None)
    if nexus_fs is None:
        logger.warning("gRPC disabled: no nexus_fs on app.state")
        return []

    exposed_methods = getattr(app.state, "exposed_methods", {})
    auth_provider = getattr(app.state, "auth_provider", None)
    api_key = getattr(app.state, "api_key", None)
    subscription_manager = getattr(app.state, "subscription_manager", None)

    import grpc.aio

    import nexus.grpc.vfs.vfs_pb2_grpc as vfs_pb2_grpc
    from nexus.grpc.servicer import VFSServicer

    servicer = VFSServicer(
        nexus_fs=nexus_fs,
        exposed_methods=exposed_methods,
        auth_provider=auth_provider,
        api_key=api_key,
        subscription_manager=subscription_manager,
    )

    server = grpc.aio.server()
    vfs_pb2_grpc.add_NexusVFSServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{port}")
    await server.start()

    app.state.grpc_server = server
    logger.info("gRPC server started on port %d", port)
    return []


async def shutdown_grpc(app: "FastAPI", _svc: "LifespanServices") -> None:
    """Stop the gRPC server if running."""
    server = getattr(app.state, "grpc_server", None)
    if server is not None:
        await server.stop(grace=5)
        logger.info("gRPC server stopped")
