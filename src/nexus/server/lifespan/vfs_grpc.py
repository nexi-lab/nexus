"""VFS gRPC transport lifespan hook (#1133, #1202).

Starts and stops the optional gRPC server for the NexusVFSService.
The server is disabled by default (port 0) and enabled by setting
``NEXUS_GRPC_PORT`` to a non-zero port number.

Follows the same pattern as ``a2a_grpc.py``.
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


async def startup_vfs_grpc(app: "FastAPI", _svc: "LifespanServices") -> list[asyncio.Task]:
    """Start the VFS gRPC server if configured."""
    port = int(os.environ.get("NEXUS_GRPC_PORT", "0"))
    if not port:
        return []

    nexus_fs = getattr(app.state, "nexus_fs", None)
    if nexus_fs is None:
        logger.warning("VFS gRPC disabled: no nexus_fs on app.state")
        return []

    exposed_methods = getattr(app.state, "exposed_methods", {})
    auth_provider = getattr(app.state, "auth_provider", None)
    api_key = getattr(app.state, "api_key", None)
    subscription_manager = getattr(app.state, "subscription_manager", None)

    import grpc.aio

    import nexus.grpc.vfs.vfs_pb2_grpc as vfs_pb2_grpc
    from nexus.server.rpc.grpc_servicer import VFSServicer

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

    app.state.vfs_grpc_server = server
    logger.info("VFS gRPC transport started on port %d", port)
    return []


async def shutdown_vfs_grpc(app: "FastAPI", _svc: "LifespanServices") -> None:
    """Stop the VFS gRPC server if running."""
    server = getattr(app.state, "vfs_grpc_server", None)
    if server is not None:
        await server.stop(grace=5)
        logger.info("VFS gRPC transport stopped")
