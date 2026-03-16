"""gRPC server lifecycle — start/stop for the unified Nexus gRPC server (#1249).

Manages a single ``grpc.aio.server()`` hosting ``NexusVFSService``.
The server is disabled by default (port 0) and enabled by setting
``NEXUS_GRPC_PORT`` to a non-zero port number.

All agent messaging flows through a single port via VFS.
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.security.tls.config import ZoneTlsConfig
    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


def _resolve_tls_config(app: "FastAPI") -> "ZoneTlsConfig | None":
    """Resolve TLS config from env vars, ZoneManager, or auto-detection.

    Priority:
    0. NEXUS_GRPC_INSECURE=true → skip all TLS (for demo/local dev)
    1. Explicit env vars: NEXUS_TLS_CERT / NEXUS_TLS_KEY / NEXUS_TLS_CA
    2. ZoneManager.tls_config (auto-generated or passed via --tls-* flags)
    3. Auto-detect from {NEXUS_DATA_DIR}/tls/
    """
    # 0. Allow explicit insecure mode for demo/dev (Issue #2961)
    if os.environ.get("NEXUS_GRPC_INSECURE", "").lower() in ("true", "1", "yes"):
        return None

    from nexus.security.tls.config import ZoneTlsConfig

    # 1. Explicit env vars
    cert = os.environ.get("NEXUS_TLS_CERT")
    key = os.environ.get("NEXUS_TLS_KEY")
    ca = os.environ.get("NEXUS_TLS_CA")
    if cert and key and ca:
        from pathlib import Path

        return ZoneTlsConfig(
            ca_cert_path=Path(ca),
            node_cert_path=Path(cert),
            node_key_path=Path(key),
            known_zones_path=Path(ca).parent / "known_zones",
        )

    # 2. ZoneManager (if running federation / Raft)
    zone_mgr = getattr(app.state, "zone_manager", None)
    if zone_mgr is not None:
        tls_cfg: ZoneTlsConfig | None = getattr(zone_mgr, "tls_config", None)
        if tls_cfg is not None:
            return tls_cfg

    # 3. Auto-detect from NEXUS_DATA_DIR
    data_dir = os.environ.get("NEXUS_DATA_DIR")
    if data_dir:
        return ZoneTlsConfig.from_data_dir(data_dir)

    return None


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

    tls_config = _resolve_tls_config(app)
    if tls_config is not None:
        creds = grpc.ssl_server_credentials(
            [(tls_config.node_key_pem, tls_config.node_cert_pem)],
            root_certificates=tls_config.ca_pem,
            require_client_auth=True,
        )
        server.add_secure_port(f"[::]:{port}", creds)
        logger.info("gRPC server started on port %d (mTLS)", port)
    else:
        bind_all = os.environ.get("NEXUS_GRPC_BIND_ALL", "").lower() in ("true", "1")
        bind_addr = "0.0.0.0" if bind_all else "127.0.0.1"
        server.add_insecure_port(f"{bind_addr}:{port}")
        if bind_all:
            logger.warning(
                "gRPC server started on port %d (insecure, all interfaces). "
                "Use only in trusted networks or containers.",
                port,
            )
        else:
            logger.warning(
                "gRPC server started on port %d (insecure, loopback only). "
                "Configure TLS to bind on all interfaces.",
                port,
            )

    await server.start()

    app.state.grpc_server = server

    # Enlist gRPC server (Q1 — infrastructure, manual start/stop)
    coord = getattr(_svc, "service_coordinator", None)
    if coord is not None:
        await coord.enlist("grpc_server", server)

    return []


async def shutdown_grpc(app: "FastAPI", _svc: "LifespanServices") -> None:
    """Stop the gRPC server if running."""
    server = getattr(app.state, "grpc_server", None)
    if server is not None:
        await server.stop(grace=5)
        logger.info("gRPC server stopped")
