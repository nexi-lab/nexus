"""Feedback protocol (ops-scenario-matrix S22: ACE).

Defines the contract for trajectory feedback — adding feedback,
querying feedback, computing effective scores, and managing the
relearning queue.

Maps 1:1 to ``services/ace/feedback.FeedbackManager``.

Storage Affinity: **RecordStore** (feedback records, relearning flags).

References:
    - docs/architecture/ops-scenario-matrix.md  (S22)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #549: ISP split of TrajectoryProtocol
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable


@runtime_checkable
class FeedbackProtocol(Protocol):
    """Service contract for trajectory feedback (FeedbackManager).

    Covers adding feedback, querying feedback for a trajectory,
    computing effective scores, and managing relearning flags.
    """

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
