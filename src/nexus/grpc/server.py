"""gRPC server lifecycle — start/stop for the unified Nexus gRPC server (#1249).

Manages a single ``grpc.aio.server()`` hosting ``NexusVFSService``.
The server defaults to port 2028 and can be changed via ``NEXUS_GRPC_PORT``.
Set ``NEXUS_GRPC_PORT=0`` to disable.

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
    """Resolve TLS config from ZoneManager or auto-detection.

    Priority:
    1. NEXUS_GRPC_TLS=false → disable TLS unconditionally
    2. ZoneManager.tls_config (Raft / federation)
    3. Auto-detect from {NEXUS_DATA_DIR}/tls/ (Raft-style PEM layout)
    4. NEXUS_TLS_CERT/KEY/CA env vars (OpenSSL-style, set by nexus up)
    5. NEXUS_GRPC_TLS=true but no certs → raise (fail closed)
    6. None → insecure (no certs, no explicit request)

    NEXUS_GRPC_TLS=false is an unconditional override (step 1).
    NEXUS_GRPC_TLS=true ensures TLS is required — if no certs are
    found after steps 2-3, startup fails rather than falling back
    to insecure.  When unset, existing behavior is preserved.
    """
    grpc_tls = os.environ.get("NEXUS_GRPC_TLS", "").lower()

    # 1. Explicit disable — takes precedence over everything
    if grpc_tls in ("false", "0", "no"):
        return None

    from nexus.security.tls.config import ZoneTlsConfig

    # 2. ZoneManager (federation / Raft)
    zone_mgr = getattr(app.state, "zone_manager", None)
    if zone_mgr is not None:
        tls_cfg: ZoneTlsConfig | None = getattr(zone_mgr, "tls_config", None)
        if tls_cfg is not None:
            return tls_cfg

    # 3. Load from NEXUS_DATA_DIR/tls/
    #    - Explicit true: check both Raft + OpenSSL layouts
    #    - Unset (auto-detect): Raft-only (backward compat)
    _explicit_on = grpc_tls in ("true", "1", "yes")
    data_dir = os.environ.get("NEXUS_DATA_DIR")
    if data_dir:
        cfg = (
            ZoneTlsConfig.from_data_dir_any(data_dir)
            if _explicit_on
            else ZoneTlsConfig.from_data_dir(data_dir)
        )
        if cfg is not None:
            return cfg

    # 4. NEXUS_TLS_CERT/KEY/CA env vars — only when explicitly requested
    #    (avoids stale env vars flipping a plaintext server to mTLS)
    if _explicit_on and os.environ.get("NEXUS_TLS_CERT"):
        cfg = ZoneTlsConfig.from_env()
        if cfg is not None:
            return cfg

    # 5. Explicit enable requested but no certs found — fail closed
    if _explicit_on:
        raise RuntimeError(
            "NEXUS_GRPC_TLS=true but no TLS certificates found. "
            "Provide certs via NEXUS_TLS_CERT/KEY/CA, "
            "in {NEXUS_DATA_DIR}/tls/, or configure ZoneManager."
        )

    return None


def _mark_grpc_done(app: "FastAPI") -> None:
    """Mark the GRPC startup phase complete on the startup tracker."""
    from nexus.server.health.startup_tracker import StartupPhase

    tracker = getattr(app.state, "startup_tracker", None)
    if tracker is not None:
        tracker.complete(StartupPhase.GRPC)


async def startup_grpc(app: "FastAPI", _svc: "LifespanServices") -> list[asyncio.Task]:
    """Start the gRPC server if configured."""
    port = int(os.environ.get("NEXUS_GRPC_PORT", "2028"))
    if not port:
        _mark_grpc_done(app)  # intentionally disabled
        return []

    nexus_fs = getattr(app.state, "nexus_fs", None)
    if nexus_fs is None:
        logger.warning("gRPC disabled: no nexus_fs on app.state")
        _mark_grpc_done(app)  # not applicable for this deployment
        return []

    exposed_methods = getattr(app.state, "exposed_methods", {})
    auth_provider = getattr(app.state, "auth_provider", None)
    api_key = getattr(app.state, "api_key", None)
    subscription_manager = getattr(app.state, "subscription_manager", None)

    import grpc.aio

    import nexus.grpc.vfs.vfs_pb2_grpc as vfs_pb2_grpc
    from nexus.grpc.servicer import VFSServicer

    # Get a content-addressable backend for ReadBlob (driver-to-driver
    # content fetch). After F2 MountTable migration, mounts live on the
    # DLC via PathRouter.list_mounts(); pick the first mount whose backend
    # supports read_content (CAS-style).
    _object_store = None
    _router = getattr(nexus_fs, "router", None)
    if _router is not None and hasattr(_router, "list_mounts"):
        for _mount in _router.list_mounts():
            _be = getattr(_mount, "backend", None)
            if _be is not None and hasattr(_be, "read_content"):
                _object_store = _be
                break

    servicer = VFSServicer(
        nexus_fs=nexus_fs,
        exposed_methods=exposed_methods,
        auth_provider=auth_provider,
        api_key=api_key,
        subscription_manager=subscription_manager,
        object_store=_object_store,
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
    _mark_grpc_done(app)  # gRPC listener is up

    # Enlist gRPC server (Q1 — infrastructure, manual start/stop)
    coord = getattr(_svc, "service_coordinator", None)
    if coord is not None:
        coord.enlist("grpc_server", server)

    return []


async def shutdown_grpc(app: "FastAPI", _svc: "LifespanServices") -> None:
    """Stop the gRPC server if running."""
    server = getattr(app.state, "grpc_server", None)
    if server is not None:
        await server.stop(grace=5)
        logger.info("gRPC server stopped")
