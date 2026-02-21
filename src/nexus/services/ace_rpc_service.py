"""ACE (Agentic Context Engineering) RPC Service.

Replaces NexusFS ACE facades (trajectories + playbooks).
No dependency on NexusFS.

Issue #2033 — Phase 3 of LEGO microkernel decomposition.
"""

import logging
from typing import Any, Literal

from nexus.contracts.rpc import rpc_expose
from nexus.contracts.types import OperationContext, parse_operation_context

logger = logging.getLogger(__name__)


class ACERPCService:
    """RPC surface for ACE trajectory and playbook operations.

    Replaces ~330 LOC of facades in NexusFS (ace_* methods).
    """

    def __init__(
        self,
        *,
        session_factory: Any,
        backend: Any,
        default_context: OperationContext,
        entity_registry: Any | None = None,
        ensure_entity_registry_fn: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._backend = backend
        self._default_context = default_context
        self._entity_registry = entity_registry
        self._ensure_entity_registry_fn = ensure_entity_registry_fn

    def _get_memory_api(self, context: dict | None = None) -> Any:
        """Get Memory API instance with context-specific configuration."""
        from nexus.bricks.memory.service import Memory

        if self._ensure_entity_registry_fn is not None:
            self._ensure_entity_registry_fn()

        session = self._session_factory()
        ctx = parse_operation_context(context)

        return Memory(
            session=session,
            backend=self._backend,
            zone_id=ctx.zone_id or self._default_context.zone_id,
            user_id=ctx.user_id or self._default_context.user_id,
            agent_id=ctx.agent_id or self._default_context.agent_id,
            entity_registry=self._entity_registry,
        )

    def _parse_context(self, context: dict | None = None) -> OperationContext:
        """Parse context dict into OperationContext."""
        return parse_operation_context(context)

    # ------------------------------------------------------------------
    # Trajectory RPC Methods
    # ------------------------------------------------------------------

    @rpc_expose(description="Start a new execution trajectory")
    def ace_start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Start tracking a new execution trajectory for ACE learning."""
        memory_api = self._get_memory_api(context)
        trajectory_id = memory_api.start_trajectory(task_description, task_type)
        return {"trajectory_id": trajectory_id}

    @rpc_expose(description="Log a step in a trajectory")
    def ace_log_trajectory_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
        context: dict | None = None,
    ) -> dict:
        """Log a step in an execution trajectory."""
        memory_api = self._get_memory_api(context)
        memory_api.log_trajectory_step(trajectory_id, step_type, description, result)
        return {"success": True}

    @rpc_expose(description="Complete a trajectory")
    def ace_complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Complete a trajectory with outcome."""
        memory_api = self._get_memory_api(context)
        completed_id = memory_api.complete_trajectory(
            trajectory_id, status, success_score, error_message
        )
        return {"trajectory_id": completed_id}

    @rpc_expose(description="Add feedback to a trajectory")
    def ace_add_feedback(
        self,
        trajectory_id: str,
        feedback_type: str,
        score: float | None = None,
        source: str | None = None,
        message: str | None = None,
        metrics: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        """Add feedback to a completed trajectory."""
        memory_api = self._get_memory_api(context)
        feedback_id = memory_api.add_feedback(
            trajectory_id, feedback_type, score, source, message, metrics
        )
        return {"feedback_id": feedback_id}

    @rpc_expose(description="Get feedback for a trajectory")
    def ace_get_trajectory_feedback(
        self, trajectory_id: str, context: dict | None = None
    ) -> list[dict[str, Any]]:
        """Get all feedback for a trajectory."""
        memory_api = self._get_memory_api(context)
        result: list[dict[str, Any]] = memory_api.get_trajectory_feedback(trajectory_id)
        return result

    @rpc_expose(description="Get effective score for a trajectory")
    def ace_get_effective_score(
        self,
        trajectory_id: str,
        strategy: Literal["latest", "average", "weighted"] = "latest",
        context: dict | None = None,
    ) -> dict:
        """Get effective score for a trajectory."""
        memory_api = self._get_memory_api(context)
        score = memory_api.get_effective_score(trajectory_id, strategy)
        return {"effective_score": score}

    @rpc_expose(description="Mark trajectory for re-learning")
    def ace_mark_for_relearning(
        self,
        trajectory_id: str,
        reason: str,
        priority: int = 5,
        context: dict | None = None,
    ) -> dict:
        """Mark trajectory for re-learning."""
        memory_api = self._get_memory_api(context)
        memory_api.mark_for_relearning(trajectory_id, reason, priority)
        return {"success": True}

    @rpc_expose(description="Query trajectories")
    def ace_query_trajectories(
        self,
        task_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        context: dict | None = None,
    ) -> list[dict]:
        """Query execution trajectories."""
        from nexus.services.ace.trajectory import TrajectoryManager

        session = self._session_factory()
        try:
            ctx = self._parse_context(context)
            traj_mgr = TrajectoryManager(
                session,
                self._backend,
                ctx.user_id or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.zone_id or self._default_context.zone_id,
            )
            return traj_mgr.query_trajectories(
                agent_id=ctx.agent_id or self._default_context.agent_id,
                task_type=task_type,
                status=status,
                limit=limit,
            )
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Playbook RPC Methods
    # ------------------------------------------------------------------

    @rpc_expose(description="Create a new playbook")
    def ace_create_playbook(
        self,
        name: str,
        description: str | None = None,
        scope: str = "agent",
        context: dict | None = None,
    ) -> dict:
        """Create a new playbook."""
        from nexus.services.ace.playbook import PlaybookManager

        session = self._session_factory()
        try:
            ctx = self._parse_context(context)
            playbook_mgr = PlaybookManager(
                session,
                self._backend,
                ctx.user_id or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.zone_id or self._default_context.zone_id,
            )
            playbook_id = playbook_mgr.create_playbook(name, description, scope)  # type: ignore
            return {"playbook_id": playbook_id}
        finally:
            session.close()

    @rpc_expose(description="Get playbook details")
    def ace_get_playbook(self, playbook_id: str, context: dict | None = None) -> dict | None:
        """Get playbook details."""
        from nexus.services.ace.playbook import PlaybookManager

        session = self._session_factory()
        try:
            ctx = self._parse_context(context)
            playbook_mgr = PlaybookManager(
                session,
                self._backend,
                ctx.user_id or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.zone_id or self._default_context.zone_id,
            )
            return playbook_mgr.get_playbook(playbook_id)
        finally:
            session.close()

    @rpc_expose(description="Query playbooks")
    def ace_query_playbooks(
        self,
        scope: str | None = None,
        limit: int = 50,
        context: dict | None = None,
    ) -> list[dict]:
        """Query playbooks."""
        from nexus.services.ace.playbook import PlaybookManager

        session = self._session_factory()
        try:
            ctx = self._parse_context(context)
            playbook_mgr = PlaybookManager(
                session,
                self._backend,
                ctx.user_id or "system",
                ctx.agent_id or self._default_context.agent_id,
                ctx.zone_id or self._default_context.zone_id,
            )
            return playbook_mgr.query_playbooks(
                agent_id=ctx.agent_id or self._default_context.agent_id,
                scope=scope,
                limit=limit,
            )
        finally:
            session.close()
