"""A2A gRPC transport lifespan hook (#1726).

Starts and stops the optional gRPC server for the A2A protocol.
The server is disabled by default (port 0) and enabled by setting
``NEXUS_A2A_GRPC_PORT`` to a non-zero port number.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


async def startup_a2a_grpc(app: FastAPI) -> list[asyncio.Task]:
    """Start the A2A gRPC server if configured."""
    port = int(os.environ.get("NEXUS_A2A_GRPC_PORT", "0"))
    if not port:
        return []

    task_manager = getattr(app.state, "a2a_task_manager", None)
    if task_manager is None:
        logger.warning("A2A gRPC disabled: no task manager on app.state")
        return []

    from nexus.bricks.a2a.grpc_server import create_grpc_server, start_grpc_server

    server = await create_grpc_server(task_manager, port=port)
    await start_grpc_server(server)
    app.state.a2a_grpc_server = server
    logger.info("A2A gRPC transport started on port %d", port)
    return []


async def shutdown_a2a_grpc(app: FastAPI) -> None:
    """Stop the A2A gRPC server if running."""
    server = getattr(app.state, "a2a_grpc_server", None)
    if server is not None:
        from nexus.bricks.a2a.grpc_server import stop_grpc_server

        await stop_grpc_server(server)
