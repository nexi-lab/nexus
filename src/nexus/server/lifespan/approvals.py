"""Approvals brick startup/shutdown — feature-flag-gated.

Wires the `ApprovalsStack` (service + PolicyGate) into the FastAPI server
when ``NEXUS_APPROVALS_ENABLED=1`` is set. When disabled (the default),
this module is a near-no-op: it attaches ``app.state.policy_gate = None``
and ``app.state.approvals_stack`` carries a stack with
``service=None, gate=None``. The MCP egress hook (Task 18) and the hub
zone-access hook (Task 19) treat ``policy_gate is None`` as "approvals
disabled" by contract, so existing servers without the env var see no
behavior change.

When enabled:
  - constructs an asyncpg pool from ``svc.database_url``
  - calls ``build_approvals_stack(...)`` which starts the
    NotifyBridge (LISTEN/NOTIFY) and the auto-deny sweeper
  - registers the read-only ``GET /hub/approvals/dump`` diag router
  - starts a process-local Python ``grpc.aio.Server`` carrying the
    ApprovalsV1 servicer on ``NEXUS_APPROVALS_GRPC_PORT`` (default
    ``2029``) when ``NEXUS_APPROVALS_ADMIN_TOKEN`` is set. The Rust-
    native VFS server on ``:2028`` is left untouched. The bearer-token
    auth gates every RPC; ReBAC integration is a follow-up TODO in
    ``grpc_auth.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

    from nexus.server.lifespan.services_container import LifespanServices

logger = logging.getLogger(__name__)


def _approvals_enabled() -> bool:
    """Read NEXUS_APPROVALS_ENABLED — opt-in only, default False."""
    return os.environ.get("NEXUS_APPROVALS_ENABLED", "").lower() in ("true", "1", "yes")


async def startup_approvals(app: "FastAPI", svc: "LifespanServices") -> list[asyncio.Task]:
    """Build and attach the approvals stack to ``app.state``.

    Always sets ``app.state.policy_gate`` and ``app.state.approvals_stack``
    so downstream hooks have a stable shape to read against. Returns no
    background tasks (the sweeper/listener are owned by ApprovalService).
    """
    from nexus.bricks.approvals.bootstrap import (
        ApprovalsStack,
        build_approvals_stack,
    )
    from nexus.bricks.approvals.config import ApprovalConfig

    enabled = _approvals_enabled()
    cfg = ApprovalConfig(enabled=enabled)

    # Disabled: attach the canonical (service=None, gate=None) stack directly
    # without exercising the build helper (no DB pool needed for a no-op).
    if not enabled:
        app.state.approvals_stack = ApprovalsStack(config=cfg, service=None, gate=None)
        app.state.policy_gate = None
        logger.debug("[APPROVALS] disabled (NEXUS_APPROVALS_ENABLED not set)")
        return []

    # --- Enabled path -----------------------------------------------------
    async_session_factory = getattr(app.state, "async_session_factory", None)
    if async_session_factory is None:
        logger.warning(
            "[APPROVALS] NEXUS_APPROVALS_ENABLED=1 but async_session_factory is None; "
            "approvals stack NOT started (record store missing or sqlite-only)."
        )
        app.state.approvals_stack = None
        app.state.policy_gate = None
        return []

    if not svc.database_url:
        logger.warning(
            "[APPROVALS] NEXUS_APPROVALS_ENABLED=1 but database_url is unset; "
            "approvals stack NOT started (LISTEN/NOTIFY needs PostgreSQL)."
        )
        app.state.approvals_stack = None
        app.state.policy_gate = None
        return []

    try:
        import asyncpg

        from nexus.core.db_utils import sqlalchemy_url_to_asyncpg_dsn
    except ImportError as e:
        logger.warning("[APPROVALS] asyncpg not installed; approvals disabled: %s", e)
        app.state.approvals_stack = None
        app.state.policy_gate = None
        return []

    pg_dsn = sqlalchemy_url_to_asyncpg_dsn(svc.database_url)
    try:
        _min_size = svc.profile_tuning.pool.asyncpg_min_size
        _max_size = svc.profile_tuning.pool.asyncpg_max_size
    except AttributeError:
        _min_size, _max_size = 2, 5

    try:
        pool = await asyncpg.create_pool(pg_dsn, min_size=_min_size, max_size=_max_size)
    except Exception as e:
        logger.warning("[APPROVALS] failed to create asyncpg pool: %s", e, exc_info=True)
        app.state.approvals_stack = None
        app.state.policy_gate = None
        return []

    app.state._approvals_asyncpg_pool = pool

    try:
        stack = await build_approvals_stack(
            cfg,
            session_factory=async_session_factory,
            asyncpg_pool=pool,
        )
    except Exception as e:
        logger.warning("[APPROVALS] build_approvals_stack failed: %s", e, exc_info=True)
        await pool.close()
        app.state._approvals_asyncpg_pool = None
        app.state.approvals_stack = None
        app.state.policy_gate = None
        return []

    app.state.approvals_stack = stack
    app.state.policy_gate = stack.gate

    # Diag HTTP router — bearer-token-gated by NEXUS_APPROVALS_DIAG_TOKEN.
    if stack.service is not None:
        try:
            from nexus.bricks.approvals.http_diag import register_diag_router

            diag_token = os.environ.get("NEXUS_APPROVALS_DIAG_TOKEN") or None
            register_diag_router(app, stack.service, allow_subject=diag_token)
            logger.info(
                "[APPROVALS] diag router registered at GET /hub/approvals/dump (auth=%s)",
                "bearer" if diag_token else "none (LOCAL DEV ONLY)",
            )
        except Exception as e:
            logger.warning("[APPROVALS] failed to register diag router: %s", e, exc_info=True)

    # Python gRPC server (separate from the Rust-native VFS server on :2028).
    # We bind a fresh `grpc.aio.Server` on `NEXUS_APPROVALS_GRPC_PORT` (default
    # :2029) and register the ApprovalsV1 servicer behind a bearer-token auth
    # implementation. Skipped when:
    #   - ApprovalService didn't start (stack.service is None)
    #   - NEXUS_APPROVALS_ADMIN_TOKEN is unset (no auth secret == no gRPC)
    # TODO(#3790): replace BearerTokenCapabilityAuth with a ReBAC-backed
    # CapabilityAuth so individual subjects can be granted scoped approvals
    # capabilities (see grpc_auth.py).
    if stack.service is not None:
        admin_token = os.environ.get("NEXUS_APPROVALS_ADMIN_TOKEN") or None
        grpc_port_str = os.environ.get("NEXUS_APPROVALS_GRPC_PORT", "2029")
        try:
            grpc_port = int(grpc_port_str)
        except ValueError:
            logger.warning(
                "[APPROVALS] NEXUS_APPROVALS_GRPC_PORT=%r is not an int; gRPC disabled",
                grpc_port_str,
            )
            grpc_port = 0

        if admin_token is None:
            logger.warning(
                "[APPROVALS] NEXUS_APPROVALS_ADMIN_TOKEN is unset; "
                "Python gRPC server NOT started (HTTP diag + in-process PolicyGate "
                "still work). Set the env var to enable the gRPC surface."
            )
        elif grpc_port <= 0:
            logger.info(
                "[APPROVALS] gRPC port disabled (port=%d); Python gRPC server NOT started",
                grpc_port,
            )
        else:
            try:
                from nexus.bricks.approvals.grpc_auth import BearerTokenCapabilityAuth
                from nexus.bricks.approvals.grpc_server_lifespan import start_grpc_server

                bind_all = os.environ.get("NEXUS_APPROVALS_GRPC_BIND_ALL", "").lower() in (
                    "true",
                    "1",
                    "yes",
                )
                host = "0.0.0.0" if bind_all else "127.0.0.1"
                auth = BearerTokenCapabilityAuth(admin_token=admin_token)
                grpc_server = await start_grpc_server(
                    stack.service,
                    auth,
                    port=grpc_port,
                    host=host,
                )
                app.state._approvals_grpc_server = grpc_server
                logger.info(
                    "[APPROVALS] Python gRPC server listening on %s:%d (auth=admin-token)",
                    host,
                    grpc_port,
                )
            except Exception as e:
                logger.warning(
                    "[APPROVALS] failed to start Python gRPC server: %s",
                    e,
                    exc_info=True,
                )
                app.state._approvals_grpc_server = None

    logger.info("[APPROVALS] enabled — service started, PolicyGate wired to app.state")
    return []


async def shutdown_approvals(app: "FastAPI", _svc: "LifespanServices") -> None:
    """Stop the approvals service and close its asyncpg pool."""
    from nexus.bricks.approvals.bootstrap import shutdown_approvals_stack

    # Stop the Python gRPC server first so in-flight RPCs drain before the
    # backing ApprovalService disappears under them.
    grpc_server = getattr(app.state, "_approvals_grpc_server", None)
    if grpc_server is not None:
        try:
            from nexus.bricks.approvals.grpc_server_lifespan import stop_grpc_server

            await stop_grpc_server(grpc_server)
            logger.debug("[APPROVALS] Python gRPC server stopped")
        except Exception as e:
            logger.warning("[APPROVALS] gRPC server stop failed: %s", e, exc_info=True)
        app.state._approvals_grpc_server = None

    stack = getattr(app.state, "approvals_stack", None)
    if stack is not None:
        try:
            await shutdown_approvals_stack(stack)
        except Exception as e:
            logger.warning("[APPROVALS] shutdown_approvals_stack failed: %s", e, exc_info=True)

    pool = getattr(app.state, "_approvals_asyncpg_pool", None)
    if pool is not None:
        try:
            await pool.close()
            logger.debug("[APPROVALS] asyncpg pool closed")
        except Exception as e:
            logger.warning("[APPROVALS] pool.close failed: %s", e, exc_info=True)
        app.state._approvals_asyncpg_pool = None
