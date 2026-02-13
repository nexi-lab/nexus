"""Dynamic feedback system for trajectories."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from nexus.storage.models import TrajectoryFeedbackModel, TrajectoryModel


class FeedbackManager:
    """Manage dynamic feedback for trajectories.

    Enables adding feedback to completed trajectories for production scenarios:
    - Production monitoring alerts
    - Human ratings and reviews
    - A/B test results
    - Long-term metrics
    """

    def __init__(self, session: Session):
        """Initialize feedback manager.

        Args:
            session: Database session
        """
        self.session = session

    def add_feedback(
        self,
        trajectory_id: str,
        feedback_type: str,
        score: float | None = None,
        source: str | None = None,
        message: str | None = None,
        metrics: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> str:
        """Add feedback to a completed trajectory.

        Args:
            trajectory_id: Trajectory to add feedback to
            feedback_type: Category of feedback ('human', 'monitoring', 'ab_test', 'production')
            score: Revised success score (0.0-1.0)
            source: Identifier of feedback source (e.g., 'user:alice', 'datadog')
            message: Human-readable explanation
            metrics: Additional metrics as dict
            timestamp: When feedback occurred (defaults to now)

        Returns:
            feedback_id: ID of the feedback entry

        Example:
            >>> # Production monitoring feedback
            >>> feedback_mgr.add_feedback(
            ...     traj_id,
            ...     feedback_type="monitoring_alert",
            ...     score=0.3,  # Revised from 0.95 → 0.3
            ...     source="datadog_monitor",
            ...     message="Error rate spiked to 15%",
            ...     metrics={"error_rate": 0.15, "alerts": 47}
            ... )
        """
        feedback_id = str(uuid.uuid4())

        # Create feedback record
        feedback = TrajectoryFeedbackModel(
            feedback_id=feedback_id,
            trajectory_id=trajectory_id,
            feedback_type=feedback_type,
            revised_score=score,
            source=source,
            message=message,
            metrics_json=json.dumps(metrics) if metrics else None,
            created_at=timestamp or datetime.now(UTC),
        )

        self.session.add(feedback)

        # Update trajectory feedback tracking
        self._update_trajectory_feedback_tracking(trajectory_id, score)

        self.session.commit()

        return feedback_id

    def get_trajectory_feedback(self, trajectory_id: str) -> list[dict[str, Any]]:
        """Get all feedback for a trajectory.

        Args:
            trajectory_id: Trajectory ID

        Returns:
            List of feedback dicts with score, type, source, timestamp

        Example:
            >>> feedback_list = feedback_mgr.get_trajectory_feedback(traj_id)
            >>> for f in feedback_list:
            ...     print(f"{f['created_at']}: {f['message']} (score={f['revised_score']})")
        """
        feedbacks = (
            self.session.query(TrajectoryFeedbackModel)
            .filter_by(trajectory_id=trajectory_id)
            .order_by(TrajectoryFeedbackModel.created_at.asc())
            .all()
        )

        return [
            {
                "feedback_id": f.feedback_id,
                "feedback_type": f.feedback_type,
                "revised_score": f.revised_score,
                "source": f.source,
                "message": f.message,
                "metrics": json.loads(f.metrics_json) if f.metrics_json else None,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in feedbacks
        ]

    def get_effective_score(
        self,
        trajectory_id: str,
        strategy: Literal["latest", "average", "weighted"] = "latest",
    ) -> float:
        """Get current effective score for trajectory.

        Args:
            trajectory_id: Trajectory to score
            strategy: Scoring strategy
                - 'latest': Most recent feedback score
                - 'average': Mean of all feedback scores
                - 'weighted': Time-weighted (recent = higher weight)

        Returns:
            Effective score (0.0-1.0)

        Example:
            >>> score = feedback_mgr.get_effective_score(traj_id, strategy="weighted")
            >>> print(f"Effective score: {score:.2f}")
        """
        trajectory = (
            self.session.query(TrajectoryModel).filter_by(trajectory_id=trajectory_id).first()
        )
        if not trajectory:
            raise ValueError(f"Trajectory {trajectory_id} not found")

        # Get all feedback
        feedbacks = self.get_trajectory_feedback(trajectory_id)

        # Filter feedback with scores
        scored_feedbacks = [f for f in feedbacks if f["revised_score"] is not None]

        if not scored_feedbacks:
            # Fall back to initial success_score
            return trajectory.success_score or 0.0

        if strategy == "latest":
            return float(scored_feedbacks[-1]["revised_score"])

        elif strategy == "average":
            scores = [float(f["revised_score"]) for f in scored_feedbacks]
            return sum(scores) / len(scores)

        elif strategy == "weighted":
            # Time-weighted: recent feedback weighted higher
            now = datetime.now(UTC)
            weighted_sum = 0.0
            weight_sum = 0.0

            for feedback in scored_feedbacks:
                created_at_str = feedback["created_at"]
                if not created_at_str:
                    continue

                created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                age_days = (now - created_at).days

                # Exponential decay: weight = 1 / (1 + age_days)
                weight = 1.0 / (1.0 + age_days)
                weighted_sum += feedback["revised_score"] * weight
                weight_sum += weight

            return weighted_sum / weight_sum if weight_sum > 0 else 0.5

        else:
            raise ValueError(f"Unknown strategy: {strategy}")

    def mark_for_relearning(
        self,
        trajectory_id: str,
        _reason: str,
        priority: int = 5,
    ) -> None:
        """Flag trajectory for re-reflection.

        Args:
            trajectory_id: Trajectory to re-learn from
            _reason: Why re-learning is needed (currently not stored, but available for future use)
            priority: Urgency (1=low, 10=critical)

        Example:
            >>> feedback_mgr.mark_for_relearning(
            ...     traj_id,
            ...     reason="production_failure",
            ...     priority=9
            ... )
        """
        trajectory = (
            self.session.query(TrajectoryModel).filter_by(trajectory_id=trajectory_id).first()
        )
        if not trajectory:
            raise ValueError(f"Trajectory {trajectory_id} not found")

        # Update relearning flags
        trajectory.needs_relearning = True  # PostgreSQL boolean
        trajectory.relearning_priority = max(0, min(priority, 10))  # Clamp to 0-10

        self.session.commit()

    def batch_add_feedback(
        self,
        feedback_items: list[dict[str, Any]],
    ) -> list[str]:
        """Add feedback to multiple trajectories at once.

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
            >>> feedback_ids = feedback_mgr.batch_add_feedback(feedback_items)
        """
        feedback_ids = []

        for item in feedback_items:
            feedback_id = self.add_feedback(
                trajectory_id=item["trajectory_id"],
                feedback_type=item.get("feedback_type", "unknown"),
                score=item.get("score"),
                source=item.get("source"),
                message=item.get("message"),
                metrics=item.get("metrics"),
                timestamp=item.get("timestamp"),
            )
            feedback_ids.append(feedback_id)

        return feedback_ids

    def get_relearning_queue(
        self,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get trajectories flagged for re-learning.

        Args:
            limit: Maximum trajectories to return

        Returns:
            List of trajectory summaries needing re-learning

        Example:
            >>> queue = feedback_mgr.get_relearning_queue(limit=5)
            >>> for item in queue:
            ...     print(f"Priority {item['priority']}: {item['task_description']}")
        """
        trajectories = (
            self.session.query(TrajectoryModel)
            .filter(TrajectoryModel.needs_relearning == True)  # noqa: E712
            .order_by(
                TrajectoryModel.relearning_priority.desc(),
                TrajectoryModel.last_feedback_at.desc(),
            )
            .limit(limit)
            .all()
        )

        return [
            {
                "trajectory_id": t.trajectory_id,
                "task_description": t.task_description,
                "priority": t.relearning_priority,
                "feedback_count": t.feedback_count,
                "effective_score": t.effective_score,
                "last_feedback_at": t.last_feedback_at.isoformat() if t.last_feedback_at else None,
            }
            for t in trajectories
        ]

    def clear_relearning_flag(self, trajectory_id: str) -> None:
        """Clear relearning flag after re-reflection.

        Args:
            trajectory_id: Trajectory ID

        Example:
            >>> # After re-reflecting
            >>> feedback_mgr.clear_relearning_flag(traj_id)
        """
        trajectory = (
            self.session.query(TrajectoryModel).filter_by(trajectory_id=trajectory_id).first()
        )
        if trajectory:
            trajectory.needs_relearning = False  # PostgreSQL boolean
            trajectory.relearning_priority = 0
            self.session.commit()

    def _update_trajectory_feedback_tracking(
        self,
        trajectory_id: str,
        score: float | None,
    ) -> None:
        """Update trajectory feedback tracking fields.

        Args:
            trajectory_id: Trajectory ID
            score: New score (or None)
        """
        trajectory = (
            self.session.query(TrajectoryModel).filter_by(trajectory_id=trajectory_id).first()
        )
        if not trajectory:
            return

        # Increment feedback count
        trajectory.feedback_count += 1

        # Update effective score (using 'latest' strategy by default)
        if score is not None:
            trajectory.effective_score = score

        # Update last feedback timestamp
        trajectory.last_feedback_at = datetime.now(UTC)

        # Auto-mark for relearning if score changed significantly
        if (
            score is not None
            and trajectory.success_score is not None
            and abs(score - trajectory.success_score) > 0.3
        ):
            trajectory.needs_relearning = True  # PostgreSQL boolean
            trajectory.relearning_priority = max(
                trajectory.relearning_priority,
                7,  # High priority for significant changes
            )
