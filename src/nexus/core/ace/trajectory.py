"""Trajectory tracking for ACE learning system."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from nexus.core.permissions import OperationContext, Permission
from nexus.storage.models import TrajectoryModel


class TrajectoryManager:
    """Manage execution trajectories for agent learning.

    Tracks task executions with steps, decisions, and outcomes
    for reflection and learning. Enforces ReBAC permissions.
    """

    def __init__(
        self,
        session: Session,
        backend: Any,
        user_id: str,
        agent_id: str | None = None,
        tenant_id: str | None = None,
        context: OperationContext | None = None,
    ):
        """Initialize trajectory manager.

        Args:
            session: Database session
            backend: Storage backend for CAS content
            user_id: User ID for ownership
            agent_id: Optional agent ID
            tenant_id: Optional tenant ID
            context: Optional operation context for permission checks
        """
        self.session = session
        self.backend = backend
        self.user_id = user_id
        self.agent_id = agent_id
        self.tenant_id = tenant_id
        self.context = context or OperationContext(
            user=user_id, groups=[], is_admin=False, is_system=False
        )
        self._active_trajectories: dict[str, dict[str, Any]] = {}

    def _check_permission(self, trajectory: TrajectoryModel, permission: Permission) -> bool:
        """Check if current user has permission to access trajectory.

        Uses identity-based ReBAC logic:
        1. Admin/system bypass
        2. Direct creator (agent matches)
        3. User ownership (same user_id)
        4. Tenant-scoped sharing (same tenant_id and scope='tenant')

        Args:
            trajectory: Trajectory model
            permission: Permission to check

        Returns:
            True if permission granted
        """
        # 1. Admin/system bypass
        if self.context.is_admin or self.context.is_system:
            return True

        # 2. Direct creator access
        if self.context.user == trajectory.agent_id:
            return True

        # 3. User ownership (same user created it)
        # 4. Tenant-scoped sharing (TODO: requires scope attribute in TrajectoryModel)
        # 5. Global scope (TODO: requires scope attribute in TrajectoryModel)
        return self.context.user == trajectory.user_id

    def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
        parent_trajectory_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        path: str | None = None,
    ) -> str:
        """Start a new trajectory.

        Args:
            task_description: Description of the task being executed
            task_type: Optional task type ('api_call', 'data_processing', 'reasoning', etc.)
            parent_trajectory_id: Optional parent trajectory for subtasks
            metadata: Additional metadata
            path: Optional path context for this trajectory

        Returns:
            trajectory_id: ID of the created trajectory

        Example:
            >>> traj_id = trajectory_mgr.start_trajectory("Deploy caching strategy", path="/project-a/")
            >>> # ... execute task ...
            >>> trajectory_mgr.complete_trajectory(traj_id, "success", success_score=0.95)
        """
        trajectory_id = str(uuid.uuid4())

        # Initialize trace data
        trace_data = {
            "steps": [],
            "decisions": [],
            "observations": [],
            "metadata": metadata or {},
            "started_at": datetime.now(UTC).isoformat(),
        }

        # Store trace in CAS
        trace_json = json.dumps(trace_data, indent=2).encode("utf-8")
        trace_hash = self.backend.write_content(trace_json)

        # Create trajectory record in database immediately with in_progress status
        now = datetime.now(UTC)
        trajectory = TrajectoryModel(
            trajectory_id=trajectory_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
            task_description=task_description,
            task_type=task_type or "general",
            parent_trajectory_id=parent_trajectory_id,
            trace_hash=trace_hash,
            status="in_progress",  # Mark as in-progress
            started_at=now,
            path=path,
        )

        self.session.add(trajectory)
        self.session.commit()

        # Also store in active trajectories (in-memory for quick access)
        self._active_trajectories[trajectory_id] = {
            "task_description": task_description,
            "task_type": task_type,
            "parent_trajectory_id": parent_trajectory_id,
            "trace": trace_data,
            "trace_hash": trace_hash,
            "start_time": now,
            "path": path,
        }

        return trajectory_id

    def log_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Log a step in the trajectory.

        Args:
            trajectory_id: Trajectory ID
            step_type: Type of step ('action', 'decision', 'observation', 'tool_call', etc.)
            description: Human-readable description
            result: Optional result data
            metadata: Additional metadata

        Example:
            >>> trajectory_mgr.log_step(
            ...     traj_id,
            ...     step_type="action",
            ...     description="Configured cache with 5min TTL",
            ...     result={"ttl_seconds": 300}
            ... )
        """
        # Try to get from memory first
        if trajectory_id not in self._active_trajectories:
            # Load from database if not in memory
            db_traj = (
                self.session.query(TrajectoryModel)
                .filter_by(trajectory_id=trajectory_id, status="in_progress")
                .first()
            )

            if not db_traj:
                raise ValueError(f"Trajectory {trajectory_id} not found or already completed")

            # Load trace from CAS
            trace_bytes = self.backend.read_content(db_traj.trace_hash)
            trace_data = json.loads(trace_bytes.decode("utf-8"))

            # Store in memory
            self._active_trajectories[trajectory_id] = {
                "task_description": db_traj.task_description,
                "task_type": db_traj.task_type,
                "parent_trajectory_id": db_traj.parent_trajectory_id,
                "trace": trace_data,
                "trace_hash": db_traj.trace_hash,
                "start_time": db_traj.started_at,
                "path": db_traj.path,
            }

        trace = self._active_trajectories[trajectory_id]["trace"]

        step_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "step_type": step_type,
            "description": description,
            "result": result,
            "metadata": metadata or {},
        }

        # Add to appropriate section based on step_type
        if step_type == "decision":
            trace["decisions"].append(step_entry)
        elif step_type == "observation":
            trace["observations"].append(step_entry)
        else:
            trace["steps"].append(step_entry)

        # Update trace in CAS and database
        trace_json = json.dumps(trace, indent=2).encode("utf-8")
        new_trace_hash = self.backend.write_content(trace_json)
        self._active_trajectories[trajectory_id]["trace_hash"] = new_trace_hash

        # Update database
        db_traj = self.session.query(TrajectoryModel).filter_by(trajectory_id=trajectory_id).first()
        if db_traj:
            db_traj.trace_hash = new_trace_hash
            self.session.commit()

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
            metrics: Performance metrics (duration_ms, tokens_used, cost_usd, etc.)

        Returns:
            trajectory_id: The completed trajectory ID

        Example:
            >>> trajectory_mgr.complete_trajectory(
            ...     traj_id,
            ...     status="success",
            ...     success_score=0.95,
            ...     metrics={"duration_ms": 1234, "tokens_used": 567}
            ... )
        """
        # Try to get from memory first
        if trajectory_id not in self._active_trajectories:
            # Load from database if not in memory
            db_traj = (
                self.session.query(TrajectoryModel)
                .filter_by(trajectory_id=trajectory_id, status="in_progress")
                .first()
            )

            if not db_traj:
                raise ValueError(f"Trajectory {trajectory_id} not found or already completed")

            # Load trace from CAS
            trace_bytes = self.backend.read_content(db_traj.trace_hash)
            trace_data = json.loads(trace_bytes.decode("utf-8"))

            # Store in memory temporarily
            self._active_trajectories[trajectory_id] = {
                "task_description": db_traj.task_description,
                "task_type": db_traj.task_type,
                "parent_trajectory_id": db_traj.parent_trajectory_id,
                "trace": trace_data,
                "trace_hash": db_traj.trace_hash,
                "start_time": db_traj.started_at,
                "path": db_traj.path,
            }

        traj_data = self._active_trajectories.pop(trajectory_id)
        trace = traj_data["trace"]

        # Add completion info to trace
        trace["completed_at"] = datetime.now(UTC).isoformat()
        trace["status"] = status
        trace["success_score"] = success_score

        # Store trace in CAS
        trace_json = json.dumps(trace, indent=2).encode("utf-8")
        trace_hash = self.backend.write_content(trace_json)

        # Calculate duration
        start_time = traj_data["start_time"]
        # Ensure start_time is timezone-aware
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=UTC)
        duration_ms = int((datetime.now(UTC) - start_time).total_seconds() * 1000)

        # Extract metrics
        metrics = metrics or {}
        tokens_used = metrics.get("tokens_used")
        cost_usd = metrics.get("cost_usd")

        # Update existing trajectory record (was created in start_trajectory)
        db_trajectory = (
            self.session.query(TrajectoryModel).filter_by(trajectory_id=trajectory_id).first()
        )

        if db_trajectory:
            # Update existing record
            db_trajectory.trace_hash = trace_hash
            db_trajectory.status = status
            db_trajectory.success_score = success_score
            db_trajectory.error_message = error_message
            db_trajectory.duration_ms = duration_ms
            db_trajectory.tokens_used = tokens_used
            db_trajectory.cost_usd = cost_usd
            db_trajectory.completed_at = datetime.now(UTC)
        else:
            # Fallback: create new record if not found
            db_trajectory = TrajectoryModel(
                trajectory_id=trajectory_id,
                user_id=self.user_id,
                agent_id=self.agent_id,
                tenant_id=self.tenant_id,
                task_description=traj_data["task_description"],
                task_type=traj_data["task_type"],
                trace_hash=trace_hash,
                status=status,
                success_score=success_score,
                error_message=error_message,
                duration_ms=duration_ms,
                tokens_used=tokens_used,
                cost_usd=cost_usd,
                parent_trajectory_id=traj_data["parent_trajectory_id"],
                started_at=traj_data["start_time"],
                completed_at=datetime.now(UTC),
                path=traj_data.get("path"),
            )
            self.session.add(db_trajectory)

        self.session.commit()

        return trajectory_id

    def get_trajectory(self, trajectory_id: str) -> dict[str, Any] | None:
        """Get trajectory by ID with trace content.

        Args:
            trajectory_id: Trajectory ID

        Returns:
            Dictionary with trajectory data and trace, or None if not found or no permission

        Raises:
            PermissionError: If user lacks READ permission
        """
        trajectory = (
            self.session.query(TrajectoryModel).filter_by(trajectory_id=trajectory_id).first()
        )
        if not trajectory:
            return None

        # Check READ permission
        if not self._check_permission(trajectory, Permission.READ):
            return None

        # Read trace from CAS
        trace_bytes = self.backend.read_content(trajectory.trace_hash)
        trace_data = json.loads(trace_bytes.decode("utf-8"))

        return {
            "trajectory_id": trajectory.trajectory_id,
            "user_id": trajectory.user_id,
            "agent_id": trajectory.agent_id,
            "task_description": trajectory.task_description,
            "task_type": trajectory.task_type,
            "status": trajectory.status,
            "success_score": trajectory.success_score,
            "error_message": trajectory.error_message,
            "duration_ms": trajectory.duration_ms,
            "tokens_used": trajectory.tokens_used,
            "cost_usd": trajectory.cost_usd,
            "started_at": trajectory.started_at.isoformat() if trajectory.started_at else None,
            "completed_at": trajectory.completed_at.isoformat()
            if trajectory.completed_at
            else None,
            "trace": trace_data,
        }

    def query_trajectories(
        self,
        agent_id: str | None = None,
        task_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        path: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query trajectories by filters with permission checks.

        Args:
            agent_id: Filter by agent ID
            task_type: Filter by task type
            status: Filter by status
            limit: Maximum results
            path: Filter by path context

        Returns:
            List of trajectory summaries (without full trace), filtered by permissions
        """
        query = self.session.query(TrajectoryModel)

        if agent_id:
            query = query.filter_by(agent_id=agent_id)
        if task_type:
            query = query.filter_by(task_type=task_type)
        if status:
            query = query.filter_by(status=status)
        if path:
            query = query.filter_by(path=path)

        query = query.order_by(TrajectoryModel.started_at.desc()).limit(
            limit * 2
        )  # Fetch extra for filtering
        trajectories = query.all()

        # Filter by permissions
        accessible_trajectories = [
            t for t in trajectories if self._check_permission(t, Permission.READ)
        ][:limit]  # Apply limit after permission filtering

        return [
            {
                "trajectory_id": t.trajectory_id,
                "user_id": t.user_id,
                "agent_id": t.agent_id,
                "task_description": t.task_description,
                "task_type": t.task_type,
                "status": t.status,
                "success_score": t.success_score,
                "duration_ms": t.duration_ms,
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "path": t.path,
            }
            for t in accessible_trajectories
        ]
