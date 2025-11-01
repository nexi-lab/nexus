"""Memory API for AI Agent Memory Management (v0.4.0).

High-level API for storing, querying, and searching agent memories
with identity-based relationships and semantic search.
"""

from __future__ import annotations

import builtins
from typing import Any, Literal

from sqlalchemy.orm import Session

from nexus.core.entity_registry import EntityRegistry
from nexus.core.memory_permission_enforcer import MemoryPermissionEnforcer
from nexus.core.memory_router import MemoryViewRouter
from nexus.core.permissions import OperationContext, Permission


class Memory:
    """High-level Memory API for AI agents.

    Provides simple methods for storing, querying, and searching memories
    with automatic permission checks and identity management.
    """

    def __init__(
        self,
        session: Session,
        backend: Any,
        tenant_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        entity_registry: EntityRegistry | None = None,
        llm_provider: Any = None,
    ):
        """Initialize Memory API.

        Args:
            session: Database session.
            backend: Storage backend for content.
            tenant_id: Current tenant ID.
            user_id: Current user ID.
            agent_id: Current agent ID.
            entity_registry: Entity registry instance.
            llm_provider: Optional LLM provider for reflection/learning.
        """
        self.session = session
        self.backend = backend
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.agent_id = agent_id
        self.llm_provider = llm_provider

        # Initialize components
        self.entity_registry = entity_registry or EntityRegistry(session)
        self.memory_router = MemoryViewRouter(session, self.entity_registry)

        # Initialize ReBAC manager for permission checks
        from sqlalchemy import Engine

        from nexus.core.rebac_manager import ReBACManager

        bind = session.get_bind()
        assert isinstance(bind, Engine), "Expected Engine, got Connection"
        self.rebac_manager = ReBACManager(bind)

        self.permission_enforcer = MemoryPermissionEnforcer(
            memory_router=self.memory_router,
            entity_registry=self.entity_registry,
            rebac_manager=self.rebac_manager,  # type: ignore[arg-type]
        )

        # Create operation context
        self.context = OperationContext(
            user=agent_id or user_id or "system",
            groups=[],
            is_admin=False,
        )

    def store(
        self,
        content: str | bytes,
        scope: str = "user",
        memory_type: str | None = None,
        importance: float | None = None,
        _metadata: dict[str, Any] | None = None,
        context: OperationContext | None = None,
    ) -> str:
        """Store a memory.

        Args:
            content: Memory content (text or bytes).
            scope: Memory scope ('agent', 'user', 'tenant', 'global').
            memory_type: Type of memory ('fact', 'preference', 'experience').
            importance: Importance score (0.0-1.0).
            metadata: Additional metadata.
            context: Optional operation context to override identity (v0.7.1+).

        Returns:
            memory_id: The created memory ID.

        Example:
            >>> memory = nx.memory
            >>> memory_id = memory.store(
            ...     "User prefers Python over JavaScript",
            ...     scope="user",
            ...     memory_type="preference",
            ...     importance=0.9
            ... )
        """
        # Convert content to bytes
        content_bytes = content.encode("utf-8") if isinstance(content, str) else content

        # v0.7.1: Use context identity if provided, otherwise fall back to instance identity
        tenant_id = context.tenant_id if context else self.tenant_id
        user_id = context.user_id if context else self.user_id
        agent_id = context.agent_id if context else self.agent_id
        # Store content in backend (CAS)
        # LocalBackend.write_content() handles hashing and storage
        try:
            backend_context = context if context else self.context
            content_hash = self.backend.write_content(content_bytes, context=backend_context)
        except Exception as e:
            # If backend write fails, we can't proceed
            raise RuntimeError(f"Failed to store content in backend: {e}") from e

        # Create memory record
        memory = self.memory_router.create_memory(
            content_hash=content_hash,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            memory_type=memory_type,
            importance=importance,
        )

        return memory.memory_id

    def query(
        self,
        user_id: str | None = None,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int | None = None,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """Query memories by relationships and metadata.

        Args:
            user_id: Filter by user ID (defaults to current user).
            agent_id: Filter by agent ID.
            tenant_id: Filter by tenant ID (defaults to current tenant).
            scope: Filter by scope.
            memory_type: Filter by memory type.
            limit: Maximum number of results.
            context: Optional operation context to override identity (v0.7.1+).

        Returns:
            List of memory dictionaries with metadata.

        Example:
            >>> memories = memory.query(scope="user", memory_type="preference")
            >>> for mem in memories:
            ...     print(f"{mem['memory_id']}: {mem['content']}")
        """
        # v0.7.1: Use context identity if provided, otherwise fall back to instance identity or explicit params
        if user_id is None:
            user_id = context.user_id if context else self.user_id
        if tenant_id is None:
            tenant_id = context.tenant_id if context else self.tenant_id

        # Query memories
        memories = self.memory_router.query_memories(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
        )

        # Filter by permissions first (before fetching content)
        accessible_memories = []
        for memory in memories:
            # Check read permission
            if self.permission_enforcer.check_memory(memory, Permission.READ, self.context):
                accessible_memories.append(memory)

        # Batch read all content hashes (optimization: single operation instead of N queries)
        content_hashes = [memory.content_hash for memory in accessible_memories]
        content_map = self.backend.batch_read_content(content_hashes)

        # Build results with enriched content
        results = []
        for memory in accessible_memories:
            # Get content from batch read result
            content_bytes = content_map.get(memory.content_hash)

            if content_bytes is not None:
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    content = content_bytes.hex()  # Binary content
            else:
                content = f"<content not available: {memory.content_hash}>"

            results.append(
                {
                    "memory_id": memory.memory_id,
                    "content": content,
                    "content_hash": memory.content_hash,
                    "tenant_id": memory.tenant_id,
                    "user_id": memory.user_id,
                    "agent_id": memory.agent_id,
                    "scope": memory.scope,
                    "visibility": memory.visibility,
                    "memory_type": memory.memory_type,
                    "importance": memory.importance,
                    "created_at": memory.created_at.isoformat() if memory.created_at else None,
                    "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
                }
            )

        return results

    def search(
        self,
        query: str,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Semantic search over memories.

        Args:
            query: Search query text.
            scope: Filter by scope.
            memory_type: Filter by memory type.
            limit: Maximum number of results.

        Returns:
            List of memory dictionaries with relevance scores.

        Example:
            >>> results = memory.search("Python programming preferences")
            >>> for mem in results:
            ...     print(f"Score: {mem['score']:.2f} - {mem['content']}")

        Note:
            Semantic search requires vector embeddings. If not available,
            falls back to simple text matching.
        """
        # TODO: Implement vector-based semantic search
        # For now, fall back to query-based filtering with text matching

        # Get all memories matching filters
        memories = self.query(
            scope=scope,
            memory_type=memory_type,
            limit=limit * 3,  # Get more to filter
        )

        # Simple text matching (fallback until vector search is integrated)
        query_lower = query.lower()
        scored_results = []

        for memory in memories:
            content = memory.get("content", "")
            if not content or isinstance(content, bytes):
                continue

            content_lower = str(content).lower()

            # Simple relevance scoring
            score = 0.0
            if query_lower in content_lower:
                score = 1.0
            else:
                # Count word matches
                query_words = query_lower.split()
                matches = sum(1 for word in query_words if word in content_lower)
                score = matches / len(query_words) if query_words else 0.0

            if score > 0:
                memory["score"] = score
                scored_results.append(memory)

        # Sort by score and limit
        scored_results.sort(key=lambda x: x["score"], reverse=True)
        return scored_results[:limit]

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """Get a specific memory by ID.

        Args:
            memory_id: Memory ID.

        Returns:
            Memory dictionary or None if not found or no permission.

        Example:
            >>> mem = memory.get("mem_123")
            >>> print(mem['content'])
        """
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return None

        # Check permission
        if not self.permission_enforcer.check_memory(memory, Permission.READ, self.context):
            return None

        # Read content
        content = None
        try:
            content_bytes = self.backend.read_content(memory.content_hash, context=self.context)
            try:
                content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                content = content_bytes.hex()
        except Exception:
            content = f"<content not available: {memory.content_hash}>"

        return {
            "memory_id": memory.memory_id,
            "content": content,
            "content_hash": memory.content_hash,
            "tenant_id": memory.tenant_id,
            "user_id": memory.user_id,
            "agent_id": memory.agent_id,
            "scope": memory.scope,
            "visibility": memory.visibility,
            "memory_type": memory.memory_type,
            "importance": memory.importance,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
            "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
        }

    def delete(self, memory_id: str) -> bool:
        """Delete a memory.

        Args:
            memory_id: Memory ID to delete.

        Returns:
            True if deleted, False if not found or no permission.

        Example:
            >>> memory.delete("mem_123")
            True
        """
        memory = self.memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        # Check permission
        if not self.permission_enforcer.check_memory(memory, Permission.WRITE, self.context):
            return False

        return self.memory_router.delete_memory(memory_id)

    def list(
        self,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int | None = 100,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List memories for current user/agent.

        Args:
            scope: Filter by scope.
            memory_type: Filter by memory type.
            limit: Maximum number of results.
            context: Optional operation context to override identity (v0.7.1+).

        Returns:
            List of memory dictionaries (without full content for efficiency).

        Example:
            >>> memories = memory.list(scope="user")
            >>> print(f"Found {len(memories)} memories")
        """
        # v0.7.1: Use context identity if provided, otherwise fall back to instance identity
        tenant_id = context.tenant_id if context else self.tenant_id
        user_id = context.user_id if context else self.user_id
        agent_id = context.agent_id if context else self.agent_id

        memories = self.memory_router.query_memories(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_id=agent_id,
            scope=scope,
            memory_type=memory_type,
            limit=limit,
        )

        results = []
        for memory in memories:
            # Check permission
            if not self.permission_enforcer.check_memory(memory, Permission.READ, self.context):
                continue

            results.append(
                {
                    "memory_id": memory.memory_id,
                    "content_hash": memory.content_hash,
                    "tenant_id": memory.tenant_id,
                    "user_id": memory.user_id,
                    "agent_id": memory.agent_id,
                    "scope": memory.scope,
                    "visibility": memory.visibility,
                    "memory_type": memory.memory_type,
                    "importance": memory.importance,
                    "created_at": memory.created_at.isoformat() if memory.created_at else None,
                    "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
                }
            )

        return results

    # ========== ACE (Agentic Context Engineering) Integration (v0.5.0) ==========

    def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
    ) -> str:
        """Start tracking a new execution trajectory.

        Args:
            task_description: Description of the task
            task_type: Optional task type

        Returns:
            trajectory_id: ID of the created trajectory

        Example:
            >>> traj_id = memory.start_trajectory("Deploy caching strategy")
            >>> # ... execute task ...
            >>> memory.complete_trajectory(traj_id, "success", success_score=0.95)
        """
        from nexus.core.ace.trajectory import TrajectoryManager

        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.tenant_id,
        )
        return traj_mgr.start_trajectory(task_description, task_type)

    def log_trajectory_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
    ) -> None:
        """Log a step in the trajectory.

        Args:
            trajectory_id: Trajectory ID
            step_type: Type of step ('action', 'decision', 'observation')
            description: Step description
            result: Optional result data
        """
        from nexus.core.ace.trajectory import TrajectoryManager

        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.tenant_id,
        )
        traj_mgr.log_step(trajectory_id, step_type, description, result)

    def log_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
    ) -> None:
        """Alias for log_trajectory_step() to match #303 spec.

        Args:
            trajectory_id: Trajectory ID
            step_type: Type of step ('action', 'decision', 'observation')
            description: Step description
            result: Optional result data

        Example:
            >>> memory.log_step(traj_id, "decision", "Checking data format")
        """
        self.log_trajectory_step(trajectory_id, step_type, description, result)

    def complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> str:
        """Complete a trajectory with outcome.

        Args:
            trajectory_id: Trajectory ID
            status: Status ('success', 'failure', 'partial')
            success_score: Success score (0.0-1.0)
            error_message: Error message if failed
            metrics: Performance metrics (rows_processed, duration_ms, etc.)

        Returns:
            trajectory_id: The completed trajectory ID

        Example:
            >>> memory.complete_trajectory(
            ...     traj_id,
            ...     status="success",
            ...     success_score=0.95,
            ...     metrics={"rows_processed": 1000, "duration_ms": 2500}
            ... )
        """
        from nexus.core.ace.trajectory import TrajectoryManager

        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.tenant_id,
        )
        return traj_mgr.complete_trajectory(
            trajectory_id,
            status,
            success_score,
            error_message,
            metrics,
        )

    def add_feedback(
        self,
        trajectory_id: str,
        feedback_type: str,
        score: float | None = None,
        source: str | None = None,
        message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> str:
        """Add feedback to a completed trajectory.

        Args:
            trajectory_id: Trajectory to add feedback to
            feedback_type: Category of feedback
            score: Revised success score (0.0-1.0)
            source: Identifier of feedback source
            message: Human-readable explanation
            metrics: Additional metrics

        Returns:
            feedback_id: ID of the feedback entry

        Example:
            >>> memory.add_feedback(
            ...     traj_id,
            ...     feedback_type="monitoring_alert",
            ...     score=0.3,
            ...     source="datadog",
            ...     message="Error rate spiked to 15%",
            ... )
        """
        from nexus.core.ace.feedback import FeedbackManager

        feedback_mgr = FeedbackManager(self.session)
        return feedback_mgr.add_feedback(
            trajectory_id,
            feedback_type,
            score,
            source,
            message,
            metrics,
        )

    def get_trajectory_feedback(
        self,
        trajectory_id: str,
    ) -> builtins.list[dict[str, Any]]:
        """Get all feedback for a trajectory.

        Returns feedback in chronological order:
        - Initial completion score
        - All subsequent feedback entries

        Args:
            trajectory_id: Trajectory ID

        Returns:
            List of feedback dicts with score, type, source, timestamp

        Example:
            >>> feedback_list = memory.get_trajectory_feedback(traj_id)
            >>> for f in feedback_list:
            ...     print(f"{f['created_at']}: {f['message']}")
        """
        from nexus.core.ace.feedback import FeedbackManager

        feedback_mgr = FeedbackManager(self.session)
        return feedback_mgr.get_trajectory_feedback(trajectory_id)

    def get_effective_score(
        self,
        trajectory_id: str,
        strategy: Literal["latest", "average", "weighted"] = "latest",
    ) -> float:
        """Get current effective score for trajectory.

        Strategies:
        - 'latest': Most recent feedback score
        - 'average': Mean of all feedback scores
        - 'weighted': Time-weighted (recent = higher weight)

        Args:
            trajectory_id: Trajectory to score
            strategy: Scoring strategy

        Returns:
            Effective score (0.0-1.0)

        Example:
            >>> score = memory.get_effective_score(traj_id, strategy="weighted")
            >>> print(f"Effective score: {score:.2f}")
        """
        from nexus.core.ace.feedback import FeedbackManager

        feedback_mgr = FeedbackManager(self.session)
        return feedback_mgr.get_effective_score(trajectory_id, strategy)

    def mark_for_relearning(
        self,
        trajectory_id: str,
        reason: str,
        priority: int = 5,
    ) -> None:
        """Flag trajectory for re-reflection.

        Used when new feedback significantly changes outcome:
        - Production failure detected
        - Human feedback indicates error
        - A/B test shows different results

        Args:
            trajectory_id: Trajectory to re-learn from
            reason: Why re-learning is needed
            priority: Urgency (1=low, 10=critical)

        Example:
            >>> memory.mark_for_relearning(
            ...     traj_id,
            ...     reason="production_failure",
            ...     priority=9
            ... )
        """
        from nexus.core.ace.feedback import FeedbackManager

        feedback_mgr = FeedbackManager(self.session)
        feedback_mgr.mark_for_relearning(trajectory_id, reason, priority)

    def batch_add_feedback(
        self,
        feedback_items: builtins.list[dict[str, Any]],
    ) -> builtins.list[str]:
        """Add feedback to multiple trajectories at once.

        Useful for:
        - Batch processing monitoring alerts
        - Bulk human feedback collection
        - A/B test result imports

        Args:
            feedback_items: List of dicts with trajectory_id, feedback_type, score, etc.

        Returns:
            List of feedback_ids

        Example:
            >>> feedback_items = [
            ...     {
            ...         "trajectory_id": "traj_1",
            ...         "feedback_type": "ab_test_result",
            ...         "score": 0.7,
            ...         "source": "ab_testing_framework",
            ...         "metrics": {"user_sat": 3.2}
            ...     },
            ...     {
            ...         "trajectory_id": "traj_2",
            ...         "feedback_type": "ab_test_result",
            ...         "score": 0.95,
            ...         "source": "ab_testing_framework",
            ...         "metrics": {"user_sat": 4.5}
            ...     }
            ... ]
            >>> feedback_ids = memory.batch_add_feedback(feedback_items)
        """
        from nexus.core.ace.feedback import FeedbackManager

        feedback_mgr = FeedbackManager(self.session)
        return feedback_mgr.batch_add_feedback(feedback_items)

    async def reflect_async(
        self,
        trajectory_id: str,
        context: str | None = None,
    ) -> dict[str, Any]:
        """Reflect on a single trajectory (async).

        Args:
            trajectory_id: Trajectory ID to reflect on
            context: Optional additional context

        Returns:
            Dictionary with reflection results:
                - helpful_strategies: Successful patterns
                - harmful_patterns: Failure patterns
                - observations: Neutral observations
                - memory_id: ID of reflection memory

        Example:
            >>> reflection = await memory.reflect_async(traj_id)
            >>> for strategy in reflection['helpful_strategies']:
            ...     print(f"✓ {strategy['description']}")
        """
        from nexus.core.ace.reflection import Reflector
        from nexus.core.ace.trajectory import TrajectoryManager

        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.tenant_id,
        )

        reflector = Reflector(
            self.session,
            self.backend,
            self.llm_provider,
            traj_mgr,
            self.user_id or "system",
            self.agent_id,
            self.tenant_id,
        )

        return await reflector.reflect_async(trajectory_id, context)

    def reflect(
        self,
        trajectory_id: str,
        context: str | None = None,
    ) -> dict[str, Any]:
        """Reflect on a single trajectory (sync).

        Args:
            trajectory_id: Trajectory ID to reflect on
            context: Optional additional context

        Returns:
            Reflection results

        Example:
            >>> reflection = memory.reflect(traj_id)
            >>> print(reflection['helpful_strategies'])
        """
        import asyncio

        return asyncio.run(self.reflect_async(trajectory_id, context))

    async def batch_reflect_async(
        self,
        agent_id: str | None = None,
        since: str | None = None,
        min_trajectories: int = 10,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        """Batch reflection across multiple trajectories (async).

        Args:
            agent_id: Filter by agent ID (defaults to current agent)
            since: ISO timestamp to filter trajectories (e.g., "2025-10-01T00:00:00Z")
            min_trajectories: Minimum trajectories needed for batch reflection
            task_type: Filter by task type

        Returns:
            Dictionary with batch reflection results:
                - trajectories_analyzed: Count
                - common_patterns: List of common successful patterns
                - common_failures: List of common failure patterns
                - reflection_ids: List of reflection memory IDs

        Example:
            >>> patterns = await memory.batch_reflect_async(
            ...     since="2025-10-01T00:00:00Z",
            ...     min_trajectories=10
            ... )
            >>> print(f"Analyzed {patterns['trajectories_analyzed']} trajectories")
        """
        from datetime import datetime

        from nexus.core.ace.reflection import Reflector
        from nexus.core.ace.trajectory import TrajectoryManager
        from nexus.storage.models import TrajectoryModel

        target_agent_id = agent_id or self.agent_id

        # Query trajectories
        query = self.session.query(TrajectoryModel).filter_by(agent_id=target_agent_id)

        if since:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            query = query.filter(TrajectoryModel.started_at >= since_dt)

        if task_type:
            query = query.filter_by(task_type=task_type)

        trajectories = query.order_by(TrajectoryModel.started_at.desc()).limit(100).all()

        if len(trajectories) < min_trajectories:
            return {
                "trajectories_analyzed": len(trajectories),
                "error": f"Need at least {min_trajectories} trajectories, found {len(trajectories)}",
                "common_patterns": [],
                "common_failures": [],
                "reflection_ids": [],
            }

        # Create managers
        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            target_agent_id,
            self.tenant_id,
        )

        reflector = Reflector(
            self.session,
            self.backend,
            self.llm_provider,
            traj_mgr,
            self.user_id or "system",
            target_agent_id,
            self.tenant_id,
        )

        # Reflect on each trajectory
        all_helpful = []
        all_harmful = []
        reflection_ids = []

        for traj in trajectories:
            try:
                # If no LLM provider, use fallback reflection
                if self.llm_provider is None:
                    # Use direct fallback instead of async call
                    trajectory_data = traj_mgr.get_trajectory(traj.trajectory_id)
                    if trajectory_data:
                        reflection_data = reflector._create_fallback_reflection(trajectory_data)
                        memory_id = reflector._store_reflection(traj.trajectory_id, reflection_data)
                        reflection = {
                            "memory_id": memory_id,
                            "helpful_strategies": reflection_data.get("helpful_strategies", []),
                            "harmful_patterns": reflection_data.get("harmful_patterns", []),
                        }
                    else:
                        continue
                else:
                    reflection = await reflector.reflect_async(traj.trajectory_id)

                all_helpful.extend(reflection.get("helpful_strategies", []))
                all_harmful.extend(reflection.get("harmful_patterns", []))
                reflection_ids.append(reflection.get("memory_id"))
            except Exception:
                # Skip failed reflections
                continue

        # Aggregate common patterns (simple frequency analysis)
        pattern_freq: dict[str, int] = {}
        for strategy in all_helpful:
            desc = strategy.get("description", "")
            pattern_freq[desc] = pattern_freq.get(desc, 0) + 1

        failure_freq: dict[str, int] = {}
        for pattern in all_harmful:
            desc = pattern.get("description", "")
            failure_freq[desc] = failure_freq.get(desc, 0) + 1

        # Get top patterns (appearing in 20%+ of trajectories)
        threshold = len(trajectories) * 0.2
        common_patterns = [
            {"description": desc, "frequency": count}
            for desc, count in pattern_freq.items()
            if count >= threshold
        ]
        common_failures = [
            {"description": desc, "frequency": count}
            for desc, count in failure_freq.items()
            if count >= threshold
        ]

        return {
            "trajectories_analyzed": len(trajectories),
            "common_patterns": sorted(common_patterns, key=lambda x: x["frequency"], reverse=True),
            "common_failures": sorted(common_failures, key=lambda x: x["frequency"], reverse=True),
            "reflection_ids": [rid for rid in reflection_ids if rid],
        }

    def batch_reflect(
        self,
        agent_id: str | None = None,
        since: str | None = None,
        min_trajectories: int = 10,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        """Batch reflection across multiple trajectories (sync).

        Args:
            agent_id: Filter by agent ID
            since: ISO timestamp to filter trajectories
            min_trajectories: Minimum trajectories needed
            task_type: Filter by task type

        Returns:
            Batch reflection results
        """
        import asyncio

        return asyncio.run(self.batch_reflect_async(agent_id, since, min_trajectories, task_type))

    def get_playbook(self, playbook_name: str = "default") -> dict[str, Any] | None:
        """Get agent's playbook.

        Args:
            playbook_name: Playbook name (default: "default")

        Returns:
            Playbook dict with strategies, or None if not found

        Example:
            >>> playbook = memory.get_playbook("default")
            >>> if playbook:
            ...     print(f"Version: {playbook['version']}")
            ...     for strategy in playbook['content']['strategies']:
            ...         print(f"  {strategy['type']}: {strategy['description']}")
        """
        from nexus.core.ace.playbook import PlaybookManager

        playbook_mgr = PlaybookManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.tenant_id,
        )

        # Query by name and agent_id
        playbooks = playbook_mgr.query_playbooks(
            agent_id=self.agent_id,
            name_pattern=playbook_name,
            limit=1,
        )

        if not playbooks:
            return None

        # Get full playbook with content
        return playbook_mgr.get_playbook(playbooks[0]["playbook_id"])

    def update_playbook(
        self,
        strategies: builtins.list[dict[str, Any]],
        playbook_name: str = "default",
    ) -> dict[str, Any]:
        """Update playbook with new strategies.

        Args:
            strategies: List of strategy dicts with:
                - category: 'helpful', 'harmful', or 'neutral'
                - pattern: Strategy description
                - context: Context where it applies
                - confidence: Confidence score (0.0-1.0)
            playbook_name: Playbook name (default: "default")

        Returns:
            Update result with playbook_id and strategies_added

        Example:
            >>> memory.update_playbook([
            ...     {
            ...         'category': 'helpful',
            ...         'pattern': 'Always validate input before processing',
            ...         'context': 'Data processing tasks',
            ...         'confidence': 0.9
            ...     }
            ... ])
        """
        from nexus.core.ace.playbook import PlaybookManager

        playbook_mgr = PlaybookManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.tenant_id,
        )

        # Get or create playbook
        playbook = self.get_playbook(playbook_name)
        if not playbook:
            # Create new playbook
            playbook_id = playbook_mgr.create_playbook(
                name=playbook_name,
                description=f"Playbook for {self.agent_id or 'agent'}",
                scope="agent",
            )
        else:
            playbook_id = playbook["playbook_id"]

        # Convert strategies to ACE format
        ace_strategies = []
        for s in strategies:
            ace_strategies.append(
                {
                    "type": s.get("category", "neutral"),  # helpful/harmful/neutral
                    "description": s.get("pattern", ""),
                    "evidence": s.get("context", ""),
                    "confidence": s.get("confidence", 0.5),
                }
            )

        # Update playbook
        playbook_mgr.update_playbook(playbook_id, strategies=ace_strategies)

        return {
            "playbook_id": playbook_id,
            "strategies_added": len(ace_strategies),
        }

    def curate_playbook(
        self,
        reflections: builtins.list[str],
        playbook_name: str = "default",
    ) -> dict[str, Any]:
        """Auto-curate playbook from reflection memories.

        Args:
            reflections: List of reflection memory IDs
            playbook_name: Playbook name (default: "default")

        Returns:
            Curation result with strategies_added and strategies_merged

        Example:
            >>> result = memory.curate_playbook(
            ...     reflections=["mem_123", "mem_456"],
            ...     playbook_name="default"
            ... )
            >>> print(f"Added {result['strategies_added']} new strategies")
        """
        from nexus.core.ace.curation import Curator
        from nexus.core.ace.playbook import PlaybookManager

        playbook_mgr = PlaybookManager(
            self.session,
            self.backend,
            self.user_id or "system",
            self.agent_id,
            self.tenant_id,
        )

        curator = Curator(self.session, self.backend, playbook_mgr)

        # Get or create playbook
        playbook = self.get_playbook(playbook_name)
        if not playbook:
            playbook_id = playbook_mgr.create_playbook(
                name=playbook_name,
                description=f"Playbook for {self.agent_id or 'agent'}",
                scope="agent",
            )
        else:
            playbook_id = playbook["playbook_id"]

        # Curate
        return curator.curate_playbook(playbook_id, reflections)

    async def consolidate_async(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        preserve_high_importance: bool = True,
        importance_threshold: float = 0.8,
    ) -> dict[str, Any]:
        """Consolidate memories to prevent context collapse (async).

        Args:
            memory_type: Filter by memory type (e.g., 'experience', 'reflection')
            scope: Filter by scope (e.g., 'agent', 'user')
            preserve_high_importance: Keep high-importance memories unconsolidated
            importance_threshold: Threshold for high importance (0.0-1.0)

        Returns:
            Consolidation report with:
                - memories_consolidated: Count
                - consolidations_created: Count
                - space_saved: Approximate reduction

        Example:
            >>> report = await memory.consolidate_async(
            ...     memory_type="experience",
            ...     scope="agent",
            ...     importance_threshold=0.8
            ... )
            >>> print(f"Consolidated {report['memories_consolidated']} memories")
        """
        from nexus.core.ace.consolidation import ConsolidationEngine

        consolidation_engine = ConsolidationEngine(
            self.session,
            self.backend,
            self.llm_provider,
            self.user_id or "system",
            self.agent_id,
            self.tenant_id,
        )

        # Determine max importance for consolidation
        max_importance = importance_threshold if preserve_high_importance else 1.0

        # Consolidate
        results = consolidation_engine.consolidate_by_criteria(
            memory_type=memory_type,
            scope=scope,
            importance_max=max_importance,
            batch_size=10,
            limit=100,
        )

        # Calculate stats
        total_consolidated = sum(r.get("memories_consolidated", 0) for r in results)
        total_created = len(results)

        return {
            "memories_consolidated": total_consolidated,
            "consolidations_created": total_created,
            "space_saved": total_consolidated - total_created,  # Approximate
        }

    def consolidate(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        preserve_high_importance: bool = True,
        importance_threshold: float = 0.8,
    ) -> dict[str, Any]:
        """Consolidate memories to prevent context collapse (sync).

        Args:
            memory_type: Filter by memory type
            scope: Filter by scope
            preserve_high_importance: Keep high-importance memories
            importance_threshold: Threshold for high importance

        Returns:
            Consolidation report
        """
        import asyncio

        return asyncio.run(
            self.consolidate_async(
                memory_type, scope, preserve_high_importance, importance_threshold
            )
        )

    async def execute_with_learning_async(
        self,
        task_fn: Any,
        task_description: str,
        task_type: str | None = None,
        auto_reflect: bool = True,
        auto_curate: bool = True,
        playbook_name: str = "default",
        **task_kwargs: Any,
    ) -> tuple[Any, str]:
        """Execute with automatic trajectory tracking + reflection + curation (async).

        Args:
            task_fn: Async function to execute
            task_description: Description of the task
            task_type: Optional task type
            auto_reflect: Automatically reflect on outcome (default True)
            auto_curate: Automatically curate playbook (default True)
            playbook_name: Playbook to curate (default "default")
            **task_kwargs: Arguments to pass to task_fn

        Returns:
            Tuple of (task_result, trajectory_id)

        Example:
            >>> async def process_data(filename):
            ...     # Process the data
            ...     return {"rows": 1000}
            >>>
            >>> result, traj_id = await memory.execute_with_learning_async(
            ...     process_data,
            ...     "Process customer orders",
            ...     auto_reflect=True,
            ...     auto_curate=True,
            ...     filename="orders.csv"
            ... )
        """
        from nexus.core.ace.learning_loop import LearningLoop

        learning_loop = LearningLoop(
            self.session,
            self.backend,
            self.llm_provider,
            self.user_id or "system",
            self.agent_id,
            self.tenant_id,
        )

        # Get or create playbook for curation
        playbook_id = None
        if auto_curate:
            playbook = self.get_playbook(playbook_name)
            if playbook:
                playbook_id = playbook["playbook_id"]

        # Execute with learning
        execution_result = await learning_loop.execute_with_learning_async(
            task_description=task_description,
            task_fn=task_fn,
            task_type=task_type,
            playbook_id=playbook_id,
            enable_reflection=auto_reflect,
            enable_curation=auto_curate,
            **task_kwargs,
        )

        return (execution_result["result"], execution_result["trajectory_id"])

    def execute_with_learning(
        self,
        task_fn: Any,
        task_description: str,
        task_type: str | None = None,
        auto_reflect: bool = True,
        auto_curate: bool = True,
        playbook_name: str = "default",
        **task_kwargs: Any,
    ) -> tuple[Any, str]:
        """Execute with automatic learning (sync).

        Args:
            task_fn: Function to execute (can be sync or async)
            task_description: Description of the task
            task_type: Optional task type
            auto_reflect: Automatically reflect
            auto_curate: Automatically curate playbook
            playbook_name: Playbook to curate
            **task_kwargs: Arguments to pass to task_fn

        Returns:
            Tuple of (task_result, trajectory_id)
        """
        import asyncio

        return asyncio.run(
            self.execute_with_learning_async(
                task_fn,
                task_description,
                task_type,
                auto_reflect,
                auto_curate,
                playbook_name,
                **task_kwargs,
            )
        )

    def query_trajectories(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """Query execution trajectories.

        Args:
            agent_id: Filter by agent ID (defaults to current agent)
            status: Filter by status (e.g., 'success', 'failure', 'partial')
            limit: Maximum number of results

        Returns:
            List of trajectory dictionaries

        Example:
            >>> trajectories = memory.query_trajectories(status="success", limit=10)
            >>> for traj in trajectories:
            ...     print(f"{traj['trajectory_id']}: {traj['task_description']}")
        """
        from nexus.core.ace.trajectory import TrajectoryManager

        target_agent_id = agent_id or self.agent_id

        traj_mgr = TrajectoryManager(
            self.session,
            self.backend,
            self.user_id or "system",
            target_agent_id,
            self.tenant_id,
        )

        return traj_mgr.query_trajectories(
            agent_id=target_agent_id,
            status=status,
            limit=limit,
        )

    def query_playbooks(
        self,
        agent_id: str | None = None,
        scope: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        """Query playbooks.

        Args:
            agent_id: Filter by agent ID (defaults to current agent)
            scope: Filter by scope (e.g., 'agent', 'user', 'global')
            limit: Maximum number of results

        Returns:
            List of playbook dictionaries

        Example:
            >>> playbooks = memory.query_playbooks(scope="agent", limit=10)
            >>> for pb in playbooks:
            ...     print(f"{pb['name']}: v{pb['version']}")
        """
        from nexus.core.ace.playbook import PlaybookManager

        target_agent_id = agent_id or self.agent_id

        playbook_mgr = PlaybookManager(
            self.session,
            self.backend,
            self.user_id or "system",
            target_agent_id,
            self.tenant_id,
        )

        return playbook_mgr.query_playbooks(
            agent_id=target_agent_id,
            scope=scope,
            limit=limit,
        )

    def process_relearning(
        self,
        limit: int = 10,
    ) -> builtins.list[dict[str, Any]]:
        """Process trajectories flagged for re-learning.

        This processes trajectories that have received feedback after completion,
        re-reflecting on them with updated scores to improve agent learning.

        Args:
            limit: Maximum number of trajectories to process

        Returns:
            List of re-learning results with trajectory_id, success, and reflection_id/error

        Example:
            >>> results = memory.process_relearning(limit=5)
            >>> for result in results:
            ...     if result['success']:
            ...         print(f"Re-learned {result['trajectory_id']}")
        """
        from nexus.core.ace.learning_loop import LearningLoop

        # Initialize learning loop
        learning_loop = LearningLoop(
            session=self.session,
            backend=self.backend,
            user_id=self.user_id or "system",
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            llm_provider=self.llm_provider,
        )

        # Process relearning queue
        return learning_loop.process_relearning_queue(limit)
