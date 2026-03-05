"""ProcessAuditHook — audit log for agent process lifecycle events (Issue #2761).

Implements VFSProcessHook to log spawn/terminate events for audit trail.
Registered at boot by the factory if ProcessManager is available.
"""

import logging

from nexus.contracts.vfs_hooks import ProcessSpawnHookContext, ProcessTerminateHookContext

logger = logging.getLogger(__name__)


class ProcessAuditHook:
    """Logs agent process lifecycle events for audit."""

    @property
    def name(self) -> str:
        return "process_audit"

    def on_pre_proc_spawn(self, ctx: ProcessSpawnHookContext) -> None:  # noqa: ARG002
        """No-op: audit does not block spawns."""

    def on_post_proc_spawn(self, ctx: ProcessSpawnHookContext) -> None:
        """Log process spawn event."""
        logger.info(
            "[AUDIT:PROC] spawned pid=%s agent=%s zone=%s parent=%s",
            ctx.pid,
            ctx.agent_id,
            ctx.zone_id,
            ctx.parent_pid,
        )

    def on_post_proc_terminate(self, ctx: ProcessTerminateHookContext) -> None:
        """Log process terminate event."""
        logger.info(
            "[AUDIT:PROC] terminated pid=%s agent=%s zone=%s reason=%s exit_code=%d",
            ctx.pid,
            ctx.agent_id,
            ctx.zone_id,
            ctx.reason,
            ctx.exit_code,
        )
