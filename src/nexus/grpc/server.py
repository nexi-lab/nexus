"""gRPC server lifecycle — boots the Rust-native VFS gRPC server.

Phase 1 of the Python→Rust VFS server migration: ``:2028`` is now owned by
a tonic server inside ``nexus_runtime``. This module is a thin lifespan
hook that:

  1. Resolves config (port / bind / TLS / API key) — same env-var contract
     as before, so deployment configs don't change.
  2. Builds Python sync wrappers around ``dispatch_method`` and the auth
     provider so the Rust server can invoke them via PyO3 callbacks for
     the ``Call`` RPC (the typed ``Read`` / ``Write`` / ``Delete`` /
     ``Ping`` paths are pure Rust, zero PyO3 cost).
  3. Calls ``nexus_runtime.start_vfs_grpc_server(...)``, stores the handle
     on ``app.state`` for shutdown.

Set ``NEXUS_GRPC_PORT=0`` to disable.
"""

from __future__ import annotations

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
    """
    grpc_tls = os.environ.get("NEXUS_GRPC_TLS", "").lower()

    if grpc_tls in ("false", "0", "no"):
        return None

    from nexus.security.tls.config import ZoneTlsConfig

    zone_mgr = getattr(app.state, "zone_manager", None)
    if zone_mgr is not None:
        tls_cfg: ZoneTlsConfig | None = getattr(zone_mgr, "tls_config", None)
        if tls_cfg is not None:
            return tls_cfg

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

    if _explicit_on and os.environ.get("NEXUS_TLS_CERT"):
        cfg = ZoneTlsConfig.from_env()
        if cfg is not None:
            return cfg

    if _explicit_on:
        raise RuntimeError(
            "NEXUS_GRPC_TLS=true but no TLS certificates found. "
            "Provide certs via NEXUS_TLS_CERT/KEY/CA, "
            "in {NEXUS_DATA_DIR}/tls/, or configure ZoneManager."
        )

    return None


def _mark_grpc_done(app: "FastAPI") -> None:
    from nexus.server.health.startup_tracker import StartupPhase

    tracker = getattr(app.state, "startup_tracker", None)
    if tracker is not None:
        tracker.complete(StartupPhase.GRPC)


async def startup_grpc(app: "FastAPI", _svc: "LifespanServices") -> list[asyncio.Task]:
    """Boot the Rust-native VFS gRPC server (Phase 1 of #2667 follow-up)."""
    port = int(os.environ.get("NEXUS_GRPC_PORT", "2028"))
    if not port:
        _mark_grpc_done(app)
        return []

    nexus_fs = getattr(app.state, "nexus_fs", None)
    if nexus_fs is None:
        logger.warning("gRPC disabled: no nexus_fs on app.state")
        _mark_grpc_done(app)
        return []

    # The Rust server holds an `Arc<Kernel>` directly. We pull the inner
    # PyKernel off NexusFS — same handle every Python syscall already uses,
    # so SSOT is preserved (no shadow kernel inside the gRPC server).
    py_kernel = getattr(nexus_fs, "_kernel", None)
    if py_kernel is None:
        logger.warning("gRPC disabled: NexusFS has no _kernel attribute (slim profile?)")
        _mark_grpc_done(app)
        return []

    bind_all = os.environ.get("NEXUS_GRPC_BIND_ALL", "").lower() in ("true", "1")
    host = "0.0.0.0" if bind_all else "127.0.0.1"
    bind_addr = f"{host}:{port}"

    tls_config = _resolve_tls_config(app)
    tls_cert_pem: bytes | None = None
    tls_key_pem: bytes | None = None
    tls_ca_pem: bytes | None = None
    if tls_config is not None:
        tls_cert_pem = tls_config.node_cert_pem
        tls_key_pem = tls_config.node_key_pem
        tls_ca_pem = tls_config.ca_pem

    api_key = getattr(app.state, "api_key", None)

    # Build Python callbacks for the Rust server. The dispatcher needs the
    # FastAPI event loop (where dispatch_method's async work runs) — capture
    # it now so the sync wrapper can schedule onto it via
    # `asyncio.run_coroutine_threadsafe`.
    from nexus.grpc.servicer import VFSCallDispatcher

    dispatcher = VFSCallDispatcher(
        nexus_fs=nexus_fs,
        exposed_methods=getattr(app.state, "exposed_methods", {}),
        auth_provider=getattr(app.state, "auth_provider", None),
        api_key=api_key,
        subscription_manager=getattr(app.state, "subscription_manager", None),
        loop=asyncio.get_running_loop(),
    )

    # nexus version for Ping. Best-effort — fall back to "unknown" when
    # the version metadata is unavailable (e.g. editable installs without
    # importlib.metadata records).
    try:
        from importlib.metadata import version as _version

        server_version = _version("nexus-ai-fs")
    except Exception:
        server_version = "unknown"

    import nexus_runtime

    handle = nexus_runtime.start_vfs_grpc_server(
        py_kernel,
        bind_addr,
        api_key,
        tls_cert_pem,
        tls_key_pem,
        tls_ca_pem,
        server_version,
        dispatcher.authenticate_sync,
        dispatcher.dispatch_call_sync,
    )

    if tls_config is not None:
        logger.info("Rust VFS gRPC server started on %s (mTLS)", bind_addr)
    elif bind_all:
        logger.warning(
            "Rust VFS gRPC server started on %s (insecure, all interfaces). "
            "Use only in trusted networks or containers.",
            bind_addr,
        )
    else:
        logger.warning(
            "Rust VFS gRPC server started on %s (insecure, loopback only). "
            "Configure TLS to bind on all interfaces.",
            bind_addr,
        )

    app.state.grpc_server_handle = handle
    app.state.grpc_dispatcher = dispatcher  # keep the loop bridge alive
    _mark_grpc_done(app)

    # Enlist via the existing service registry surface so health probes
    # and shutdown hooks see the same handle Python used to create.
    nx = getattr(_svc, "nexus_fs", _svc)
    if hasattr(nx, "sys_setattr"):
        nx.sys_setattr("/__sys__/services/grpc_server", service=handle)

    return []


async def shutdown_grpc(app: "FastAPI", _svc: "LifespanServices") -> None:
    """Stop the Rust gRPC server if running."""
    handle = getattr(app.state, "grpc_server_handle", None)
    if handle is not None:
        # `shutdown` is sync — the Rust runtime wait happens inside the
        # PyO3 method. Run on default executor so we don't block the
        # FastAPI event loop while tonic flushes in-flight responses.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, handle.shutdown)
        logger.info("Rust VFS gRPC server stopped")
