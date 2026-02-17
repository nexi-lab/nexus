"""Trajectory lifecycle protocol (ops-scenario-matrix S22: ACE).

Defines the contract for trajectory lifecycle management — starting,
logging steps, completing, querying, and retrieving individual trajectories.

Maps 1:1 to ``services/ace/trajectory.TrajectoryManager``.

Related ISP siblings (Issue #549):
    - ``FeedbackProtocol``    → ``services/ace/feedback.FeedbackManager``
    - ``PlaybookProtocol``    → ``services/ace/playbook.PlaybookManager``
    - ``ReflectionProtocol``  → ``services/ace/reflection.Reflector``
    - ``CurationProtocol``    → ``services/ace/curation.Curator``

Storage Affinity: **RecordStore** (trajectory records) +
                  **ObjectStore** (CAS trace blobs).

References:
    - docs/architecture/ops-scenario-matrix.md  (S22)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TrajectoryLifecycleProtocol(Protocol):
    """Service contract for trajectory lifecycle (TrajectoryManager).

    Covers starting a trajectory, logging steps, completing it,
    retrieving a single trajectory, and querying across trajectories.
    """

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
