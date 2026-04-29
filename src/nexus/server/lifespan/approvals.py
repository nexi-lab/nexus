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
  - **defers** the ApprovalsServicer gRPC registration: the only Python
    gRPC server the daemon owns today is the Rust-native VFS server
    (``nexus_kernel.start_vfs_grpc_server``), which doesn't accept Python
    `add_*Servicer_to_server` calls; ``app.state.capability_auth`` does
    not yet exist either. Tasks 21–23 (E2E) currently exercise the brick
    via ApprovalService directly, not via the gRPC surface — this is
    deferred until both pieces of plumbing land.
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

    # TODO(#3790 Task 20 follow-up): register ApprovalsServicer onto a Python
    # grpc.aio server once the daemon owns one + a CapabilityAuth implementation
    # lives on app.state. The current gRPC surface (port 2028) is the Rust-
    # native VFS server (`nexus_kernel.start_vfs_grpc_server`) which does not
    # accept Python `add_ApprovalsV1Servicer_to_server(...)` calls. The brick
    # E2E tests (Tasks 21–23) drive ApprovalService directly, not via gRPC, so
    # this is non-blocking for that work.

    logger.info("[APPROVALS] enabled — service started, PolicyGate wired to app.state")
    return []


async def shutdown_approvals(app: "FastAPI", _svc: "LifespanServices") -> None:
    """Stop the approvals service and close its asyncpg pool."""
    from nexus.bricks.approvals.bootstrap import shutdown_approvals_stack

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
