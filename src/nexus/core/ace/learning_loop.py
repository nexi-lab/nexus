"""Learning loop - main ACE integration."""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from nexus.core.ace.consolidation import ConsolidationEngine
from nexus.core.ace.curation import Curator
from nexus.core.ace.feedback import FeedbackManager
from nexus.core.ace.playbook import PlaybookManager
from nexus.core.ace.reflection import Reflector
from nexus.core.ace.trajectory import TrajectoryManager
from nexus.llm.provider import LLMProvider


class LearningLoop:
    """Main ACE learning loop integration.

    Provides high-level API for executing tasks with automatic learning:
    1. Track execution as trajectory
    2. Reflect on outcome
    3. Update playbooks
    4. Consolidate memories
    """

    def __init__(
        self,
        session: Session,
        backend: Any,
        llm_provider: LLMProvider,
        user_id: str,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        context: Any = None,
    ):
        """Initialize learning loop.

        Args:
            session: Database session
            backend: Storage backend for CAS content
            llm_provider: LLM provider for reflection
            user_id: User ID for ownership
            agent_id: Optional agent ID
            tenant_id: Optional tenant ID
            context: Optional operation context for permission checks
        """
        self.session = session
        self.backend = backend
        self.llm_provider = llm_provider
        self.user_id = user_id
        self.agent_id = agent_id
        self.tenant_id = tenant_id
        self.context = context

        # Initialize components with context for permission checks
        self.trajectory_manager = TrajectoryManager(
            session, backend, user_id, agent_id, tenant_id, context
        )
        self.playbook_manager = PlaybookManager(
            session, backend, user_id, agent_id, tenant_id, context
        )
        self.reflector = Reflector(
            session, backend, llm_provider, self.trajectory_manager, user_id, agent_id, tenant_id
        )
        self.curator = Curator(session, backend, self.playbook_manager)
        self.consolidation_engine = ConsolidationEngine(
            session, backend, llm_provider, user_id, agent_id, tenant_id
        )
        self.feedback_manager = FeedbackManager(session)

    async def execute_with_learning_async(
        self,
        task_description: str,
        task_fn: Callable,
        task_type: str | None = None,
        playbook_id: str | None = None,
        enable_reflection: bool = True,
        enable_curation: bool = True,
        **task_kwargs: Any,
    ) -> dict[str, Any]:
        """Execute a task with automatic learning (async).

        Args:
            task_description: Description of the task
            task_fn: Async function to execute
            task_type: Optional task type
            playbook_id: Optional playbook to update with learnings
            enable_reflection: Whether to reflect on outcome (default True)
            enable_curation: Whether to update playbook (default True)
            **task_kwargs: Arguments to pass to task_fn

        Returns:
            Dictionary with execution results:
                - result: Task function result
                - trajectory_id: Trajectory ID
                - success: Whether task succeeded
                - reflection_id: Reflection memory ID (if enabled)

        Example:
            >>> async def deploy_cache():
            ...     # Deploy caching strategy
            ...     return {"deployed": True}
            >>>
            >>> result = await learning_loop.execute_with_learning_async(
            ...     "Deploy caching strategy",
            ...     deploy_cache,
            ...     playbook_id="playbook_123"
            ... )
        """
        # Start trajectory
        trajectory_id = self.trajectory_manager.start_trajectory(
            task_description=task_description,
            task_type=task_type,
        )

        start_time = datetime.now(UTC)
        result = None
        error = None
        status = "success"
        success_score = 1.0

        try:
            # Execute task
            if asyncio.iscoroutinefunction(task_fn):
                result = await task_fn(**task_kwargs)
            else:
                result = task_fn(**task_kwargs)

            # Log successful outcome
            self.trajectory_manager.log_step(
                trajectory_id,
                step_type="observation",
                description="Task completed successfully",
                result=result,
            )

        except Exception as e:
            # Log failure
            error = str(e)
            status = "failure"
            success_score = 0.0

            self.trajectory_manager.log_step(
                trajectory_id,
                step_type="observation",
                description="Task failed with error",
                result={"error": error, "traceback": traceback.format_exc()},
            )

        # Calculate metrics
        duration_ms = int((datetime.now(UTC) - start_time).total_seconds() * 1000)

        # Complete trajectory
        self.trajectory_manager.complete_trajectory(
            trajectory_id,
            status=status,
            success_score=success_score,
            error_message=error,
            metrics={"duration_ms": duration_ms},
        )

        # Reflect on outcome if enabled
        reflection_id = None
        if enable_reflection:
            try:
                reflection = await self.reflector.reflect_async(trajectory_id)
                reflection_id = reflection.get("memory_id")

                # Curate playbook if enabled and playbook specified
                if enable_curation and playbook_id and reflection_id:
                    self.curator.curate_playbook(
                        playbook_id,
                        reflection_memory_ids=[reflection_id],
                    )
            except Exception as e:
                # Log but don't fail entire execution
                print(f"Reflection/curation failed: {e}")

        return {
            "result": result,
            "trajectory_id": trajectory_id,
            "success": status == "success",
            "error": error,
            "reflection_id": reflection_id,
            "duration_ms": duration_ms,
        }

    def execute_with_learning(
        self,
        task_description: str,
        task_fn: Callable,
        task_type: str | None = None,
        playbook_id: str | None = None,
        enable_reflection: bool = True,
        enable_curation: bool = True,
        **task_kwargs: Any,
    ) -> dict[str, Any]:
        """Execute a task with automatic learning (sync wrapper).

        Args:
            task_description: Description of the task
            task_fn: Function to execute (can be sync or async)
            task_type: Optional task type
            playbook_id: Optional playbook to update
            enable_reflection: Whether to reflect on outcome
            enable_curation: Whether to update playbook
            **task_kwargs: Arguments to pass to task_fn

        Returns:
            Execution results
        """
        return asyncio.run(
            self.execute_with_learning_async(
                task_description,
                task_fn,
                task_type,
                playbook_id,
                enable_reflection,
                enable_curation,
                **task_kwargs,
            )
        )

    async def process_relearning_queue_async(
        self,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Process trajectories flagged for re-learning (async).

        Args:
            limit: Maximum trajectories to process

        Returns:
            List of re-learning results

        Example:
            >>> results = await learning_loop.process_relearning_queue_async()
            >>> print(f"Processed {len(results)} trajectories")
        """
        # Get relearning queue
        queue = self.feedback_manager.get_relearning_queue(limit=limit)

        results = []
        for item in queue:
            trajectory_id = item["trajectory_id"]

            try:
                # Re-reflect with updated effective score
                reflection = await self.reflector.reflect_async(trajectory_id)

                # Clear relearning flag
                self.feedback_manager.clear_relearning_flag(trajectory_id)

                results.append(
                    {
                        "trajectory_id": trajectory_id,
                        "success": True,
                        "reflection_id": reflection.get("memory_id"),
                    }
                )

            except Exception as e:
                results.append(
                    {
                        "trajectory_id": trajectory_id,
                        "success": False,
                        "error": str(e),
                    }
                )

        return results

    def process_relearning_queue(self, limit: int = 10) -> list[dict[str, Any]]:
        """Process relearning queue (sync wrapper).

        Args:
            limit: Maximum trajectories to process

        Returns:
            List of re-learning results
        """
        return asyncio.run(self.process_relearning_queue_async(limit))

    def get_playbook_strategies(
        self,
        playbook_id: str,
        task_description: str,
        strategy_type: Literal["helpful", "harmful", "neutral"] | None = None,
    ) -> list[dict[str, Any]]:
        """Get relevant strategies from playbook for a task.

        Args:
            playbook_id: Playbook ID
            task_description: Task description
            strategy_type: Filter by type ('helpful', 'harmful', 'neutral')

        Returns:
            List of relevant strategies
        """
        return self.playbook_manager.get_relevant_strategies(
            playbook_id,
            task_description,
            strategy_type,
        )

    def consolidate_memories(
        self,
        memory_type: str | None = None,
        importance_max: float = 0.5,
        batch_size: int = 10,
    ) -> list[dict[str, Any]]:
        """Consolidate low-importance memories.

        Args:
            memory_type: Filter by memory type
            importance_max: Maximum importance threshold
            batch_size: Memories per consolidation batch

        Returns:
            List of consolidation results
        """
        return self.consolidation_engine.consolidate_by_criteria(
            memory_type=memory_type,
            importance_max=importance_max,
            batch_size=batch_size,
        )
