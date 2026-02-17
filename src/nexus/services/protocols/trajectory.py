"""Trajectory / ACE service protocol (ops-scenario-matrix S22: ACE).

Defines the contract for Agentic Continuous Evaluation — tracking task
executions as trajectories, reflecting on outcomes, curating playbooks,
and managing feedback loops for agent learning.

Storage Affinity: **RecordStore** (trajectory / playbook / feedback records) +
                  **ObjectStore** (CAS trace blobs) +
                  **CacheStore** (LLM reflection cache).

References:
    - docs/architecture/ops-scenario-matrix.md  (S22)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable


@runtime_checkable
class TrajectoryProtocol(Protocol):
    """Service contract for ACE (Agentic Continuous Evaluation).

    Covers trajectory lifecycle, feedback, playbook management, reflection,
    and curation.  Each method group maps to one of the concrete managers
    in ``services/ace/`` (TrajectoryManager, FeedbackManager,
    PlaybookManager, Reflector, Curator).
    """

    # ── Trajectory lifecycle ──────────────────────────────────────────

    def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
        parent_trajectory_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        path: str | None = None,
    ) -> str: ...

    def log_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> str: ...

    def get_trajectory(
        self,
        trajectory_id: str,
    ) -> dict[str, Any] | None: ...

    def query_trajectories(
        self,
        agent_id: str | None = None,
        task_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
        path: str | None = None,
    ) -> list[dict[str, Any]]: ...

    # ── Feedback ──────────────────────────────────────────────────────

    def add_feedback(
        self,
        trajectory_id: str,
        feedback_type: str,
        score: float | None = None,
        source: str | None = None,
        message: str | None = None,
        metrics: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> str: ...

    def get_trajectory_feedback(
        self,
        trajectory_id: str,
    ) -> list[dict[str, Any]]: ...

    def get_effective_score(
        self,
        trajectory_id: str,
        strategy: Literal["latest", "average", "weighted"] = "latest",
    ) -> float: ...

    def mark_for_relearning(
        self,
        trajectory_id: str,
        reason: str,
        priority: int = 5,
    ) -> None: ...

    def get_relearning_queue(
        self,
        limit: int = 10,
    ) -> list[dict[str, Any]]: ...

    def clear_relearning_flag(
        self,
        trajectory_id: str,
    ) -> None: ...

    # ── Playbook management ───────────────────────────────────────────

    def create_playbook(
        self,
        name: str,
        description: str | None = None,
        scope: Literal["agent", "user", "zone", "global"] = "agent",
        visibility: Literal["private", "shared", "public"] = "private",
        initial_strategies: list[dict[str, Any]] | None = None,
    ) -> str: ...

    def get_playbook(
        self,
        playbook_id: str,
    ) -> dict[str, Any] | None: ...

    def update_playbook(
        self,
        playbook_id: str,
        strategies: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
        increment_version: bool = True,
    ) -> None: ...

    def record_usage(
        self,
        playbook_id: str,
        success: bool,
        improvement_score: float | None = None,
    ) -> None: ...

    def query_playbooks(
        self,
        agent_id: str | None = None,
        scope: str | None = None,
        name_pattern: str | None = None,
        limit: int = 50,
        path: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def delete_playbook(
        self,
        playbook_id: str,
    ) -> bool: ...

    def get_relevant_strategies(
        self,
        playbook_id: str,
        task_description: str,
        strategy_type: Literal["helpful", "harmful", "neutral"] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]: ...

    # ── Reflection ────────────────────────────────────────────────────

    async def reflect_async(
        self,
        trajectory_id: str,
        context: str | None = None,
        reflection_prompt: str | None = None,
    ) -> dict[str, Any]: ...

    # ── Curation ──────────────────────────────────────────────────────

    def curate_playbook(
        self,
        playbook_id: str,
        reflection_memory_ids: list[str],
        merge_threshold: float = 0.7,
    ) -> dict[str, Any]: ...

    def curate_from_trajectory(
        self,
        playbook_id: str,
        trajectory_id: str,
    ) -> dict[str, Any] | None: ...
