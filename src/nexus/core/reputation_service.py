"""Reputation service for feedback and score management (Issue #1356).

Manages the feedback → event → materialized score pipeline:
- Submit feedback: validate, create event, update materialized score.
- Query reputation: composite score lookup with TTLCache.
- Leaderboard: zone-scoped ranking by composite score.

Uses SessionMixin for session lifecycle, TTLCache for read performance,
and Bayesian Beta math for scoring.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from cachetools import TTLCache
from sqlalchemy import select

from nexus.core.reputation_math import compute_composite_score
from nexus.core.reputation_records import ReputationEvent, ReputationScore
from nexus.storage.models._base import _generate_uuid
from nexus.storage.models.reputation_event import ReputationEventModel
from nexus.storage.models.reputation_score import ReputationScoreModel
from nexus.storage.session_mixin import SessionMixin

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

# Maximum leaderboard entries to prevent unbounded queries
MAX_LEADERBOARD_LIMIT = 1000

# Outcome to alpha/beta increment mapping
_OUTCOME_INCREMENTS: dict[str, tuple[float, float]] = {
    "positive": (1.0, 0.0),
    "negative": (0.0, 1.0),
    "neutral": (0.3, 0.3),
    "mixed": (0.5, 0.5),
}


class ReputationService(SessionMixin):
    """Feedback and reputation score management.

    Thread-safe: cache access synchronized via _cache_lock.

    Args:
        session_factory: SQLAlchemy sessionmaker for database access.
        cache_maxsize: Max entries in the reputation TTLCache.
        cache_ttl: TTL in seconds for cached scores.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cache_maxsize: int = 10_000,
        cache_ttl: int = 60,
    ) -> None:
        self._session_factory = session_factory
        self._cache_lock = threading.Lock()
        self._score_cache: TTLCache[str, ReputationScore | None] = TTLCache(
            maxsize=cache_maxsize, ttl=cache_ttl
        )

    def submit_feedback(
        self,
        rater_agent_id: str,
        rated_agent_id: str,
        exchange_id: str,
        zone_id: str,
        outcome: str,
        reliability_score: float | None = None,
        quality_score: float | None = None,
        timeliness_score: float | None = None,
        fairness_score: float | None = None,
        evidence_hash: str | None = None,
        context: str = "general",
    ) -> ReputationEvent:
        """Submit feedback for an exchange, creating an event and updating scores.

        Args:
            rater_agent_id: Agent providing feedback.
            rated_agent_id: Agent being rated.
            exchange_id: Exchange this feedback pertains to.
            zone_id: Zone scope.
            outcome: "positive", "negative", "neutral", or "mixed".
            reliability_score: Optional dimension score (0.0-1.0).
            quality_score: Optional dimension score (0.0-1.0).
            timeliness_score: Optional dimension score (0.0-1.0).
            fairness_score: Optional dimension score (0.0-1.0).
            evidence_hash: Optional SHA-256 hash of evidence.
            context: Reputation context (default "general").

        Returns:
            ReputationEvent record.

        Raises:
            ValueError: Self-rating, invalid outcome, or score out of range.
            DuplicateFeedbackError: Duplicate feedback for same exchange+rater.
        """
        # Validate inputs
        if rater_agent_id == rated_agent_id:
            msg = "Self-rating is not allowed"
            raise ValueError(msg)

        if outcome not in _OUTCOME_INCREMENTS:
            msg = f"Invalid outcome: {outcome!r}. Must be one of {list(_OUTCOME_INCREMENTS.keys())}"
            raise ValueError(msg)

        for name, val in [
            ("reliability_score", reliability_score),
            ("quality_score", quality_score),
            ("timeliness_score", timeliness_score),
            ("fairness_score", fairness_score),
        ]:
            if val is not None and not (0.0 <= val <= 1.0):
                msg = f"{name} must be between 0.0 and 1.0, got {val}"
                raise ValueError(msg)

        with self._get_session() as session:
            # Check for duplicate feedback
            existing = session.execute(
                select(ReputationEventModel).where(
                    ReputationEventModel.exchange_id == exchange_id,
                    ReputationEventModel.rater_agent_id == rater_agent_id,
                )
            ).scalar_one_or_none()

            if existing is not None:
                raise DuplicateFeedbackError(
                    f"Feedback already submitted for exchange {exchange_id} by {rater_agent_id}"
                )

            # Create event
            event_model = self._create_event(
                session=session,
                rater_agent_id=rater_agent_id,
                rated_agent_id=rated_agent_id,
                exchange_id=exchange_id,
                zone_id=zone_id,
                event_type="feedback",
                outcome=outcome,
                reliability_score=reliability_score,
                quality_score=quality_score,
                timeliness_score=timeliness_score,
                fairness_score=fairness_score,
                evidence_hash=evidence_hash,
                context=context,
            )

            # Update materialized score
            self._update_materialized_score(
                session=session,
                rated_agent_id=rated_agent_id,
                zone_id=zone_id,
                outcome=outcome,
                reliability_score=reliability_score,
                quality_score=quality_score,
                timeliness_score=timeliness_score,
                fairness_score=fairness_score,
                context=context,
            )

            # Invalidate cache BEFORE commit to prevent stale reads
            cache_key = f"{rated_agent_id}:{context}:all_time"
            with self._cache_lock:
                self._score_cache.pop(cache_key, None)

            record = self._event_model_to_record(event_model)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[REPUTATION] Feedback submitted: %s → %s (exchange=%s, outcome=%s)",
                rater_agent_id,
                rated_agent_id,
                exchange_id,
                outcome,
            )

        return record

    def get_reputation(
        self,
        agent_id: str,
        context: str = "general",
        window: str = "all_time",
    ) -> ReputationScore | None:
        """Get materialized reputation score for an agent.

        Args:
            agent_id: Agent to look up.
            context: Reputation context (default "general").
            window: Time window (default "all_time").

        Returns:
            ReputationScore record or None if not found.
        """
        cache_key = f"{agent_id}:{context}:{window}"

        with self._cache_lock:
            cached = self._score_cache.get(cache_key)
            if cached is not None:
                return cached

        with self._get_session() as session:
            model = session.execute(
                select(ReputationScoreModel).where(
                    ReputationScoreModel.agent_id == agent_id,
                    ReputationScoreModel.context == context,
                    ReputationScoreModel.window == window,
                )
            ).scalar_one_or_none()

            if model is None:
                return None

            record = self._score_model_to_record(model)

        with self._cache_lock:
            self._score_cache[cache_key] = record

        return record

    def get_leaderboard(
        self,
        zone_id: str,
        context: str = "general",
        limit: int = 50,
    ) -> list[ReputationScore]:
        """Get reputation leaderboard for a zone.

        Args:
            zone_id: Zone to query.
            context: Reputation context (default "general").
            limit: Max entries to return (default 50, max 1000).

        Returns:
            List of ReputationScore records ordered by composite_score descending.
        """
        limit = min(max(1, limit), MAX_LEADERBOARD_LIMIT)
        with self._get_session() as session:
            models = list(
                session.execute(
                    select(ReputationScoreModel)
                    .where(
                        ReputationScoreModel.zone_id == zone_id,
                        ReputationScoreModel.context == context,
                        ReputationScoreModel.window == "all_time",
                    )
                    .order_by(ReputationScoreModel.composite_score.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [self._score_model_to_record(m) for m in models]

    def get_feedback_for_exchange(
        self,
        exchange_id: str,
    ) -> list[ReputationEvent]:
        """Get all feedback events for an exchange.

        Args:
            exchange_id: Exchange to look up.

        Returns:
            List of ReputationEvent records.
        """
        with self._get_session() as session:
            models = list(
                session.execute(
                    select(ReputationEventModel)
                    .where(ReputationEventModel.exchange_id == exchange_id)
                    .order_by(ReputationEventModel.created_at.desc())
                )
                .scalars()
                .all()
            )
            return [self._event_model_to_record(m) for m in models]

    def _create_event(
        self,
        session: Session,
        rater_agent_id: str,
        rated_agent_id: str,
        exchange_id: str,
        zone_id: str,
        event_type: str,
        outcome: str,
        reliability_score: float | None,
        quality_score: float | None,
        timeliness_score: float | None,
        fairness_score: float | None,
        evidence_hash: str | None,
        context: str,
        weight: float = 1.0,
    ) -> ReputationEventModel:
        """Create a reputation event in the database."""
        event_id = _generate_uuid()
        now = datetime.now(UTC)

        record_hash = self._compute_record_hash(
            event_id=event_id,
            rater_agent_id=rater_agent_id,
            rated_agent_id=rated_agent_id,
            exchange_id=exchange_id,
            event_type=event_type,
            outcome=outcome,
            created_at=now,
        )

        model = ReputationEventModel(
            id=event_id,
            created_at=now,
            record_hash=record_hash,
            rater_agent_id=rater_agent_id,
            rated_agent_id=rated_agent_id,
            exchange_id=exchange_id,
            zone_id=zone_id,
            event_type=event_type,
            outcome=outcome,
            reliability_score=reliability_score,
            quality_score=quality_score,
            timeliness_score=timeliness_score,
            fairness_score=fairness_score,
            evidence_hash=evidence_hash,
            context=context,
            weight=weight,
        )
        session.add(model)
        session.flush()
        return model

    def _update_materialized_score(
        self,
        session: Session,
        rated_agent_id: str,
        zone_id: str,
        outcome: str,
        reliability_score: float | None,
        quality_score: float | None,
        timeliness_score: float | None,
        fairness_score: float | None,
        context: str,
    ) -> None:
        """Incrementally update the materialized reputation score."""
        # Get or create score record (all_time window)
        model = session.execute(
            select(ReputationScoreModel).where(
                ReputationScoreModel.agent_id == rated_agent_id,
                ReputationScoreModel.context == context,
                ReputationScoreModel.window == "all_time",
            )
        ).scalar_one_or_none()

        if model is None:
            model = ReputationScoreModel(
                agent_id=rated_agent_id,
                context=context,
                window="all_time",
                zone_id=zone_id,
            )
            session.add(model)
            session.flush()

        # Compute alpha/beta increments from outcome
        alpha_inc, beta_inc = _OUTCOME_INCREMENTS.get(outcome, (0.5, 0.5))

        # Update per-dimension parameters using dimension scores if provided
        self._update_dimension(model, "reliability", reliability_score, alpha_inc, beta_inc)
        self._update_dimension(model, "quality", quality_score, alpha_inc, beta_inc)
        self._update_dimension(model, "timeliness", timeliness_score, alpha_inc, beta_inc)
        self._update_dimension(model, "fairness", fairness_score, alpha_inc, beta_inc)

        # Update interaction counts
        model.total_interactions += 1
        if outcome == "positive":
            model.positive_interactions += 1
        elif outcome == "negative":
            model.negative_interactions += 1

        # Recompute composite score
        dimensions = {
            "reliability": (model.reliability_alpha, model.reliability_beta),
            "quality": (model.quality_alpha, model.quality_beta),
            "timeliness": (model.timeliness_alpha, model.timeliness_beta),
            "fairness": (model.fairness_alpha, model.fairness_beta),
        }
        model.composite_score, model.composite_confidence = compute_composite_score(dimensions)
        model.updated_at = datetime.now(UTC)

        session.flush()

    @staticmethod
    def _update_dimension(
        model: ReputationScoreModel,
        dimension: str,
        score: float | None,
        alpha_inc: float,
        beta_inc: float,
    ) -> None:
        """Update a single dimension's alpha/beta parameters.

        If a per-dimension score is provided (0.0-1.0), use it to scale
        the alpha/beta increment. Otherwise, use the outcome-based increment.
        """
        alpha_attr = f"{dimension}_alpha"
        beta_attr = f"{dimension}_beta"

        if score is not None:
            # Scale: score=1.0 → full alpha, score=0.0 → full beta
            setattr(model, alpha_attr, getattr(model, alpha_attr) + score)
            setattr(model, beta_attr, getattr(model, beta_attr) + (1.0 - score))
        else:
            setattr(model, alpha_attr, getattr(model, alpha_attr) + alpha_inc)
            setattr(model, beta_attr, getattr(model, beta_attr) + beta_inc)

    @staticmethod
    def _compute_record_hash(
        event_id: str,
        rater_agent_id: str,
        rated_agent_id: str,
        exchange_id: str,
        event_type: str,
        outcome: str,
        created_at: datetime,
    ) -> str:
        """Compute SHA-256 hash for tamper detection."""
        payload = json.dumps(
            {
                "id": event_id,
                "rater": rater_agent_id,
                "rated": rated_agent_id,
                "exchange": exchange_id,
                "type": event_type,
                "outcome": outcome,
                "ts": created_at.isoformat(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _event_model_to_record(model: ReputationEventModel) -> ReputationEvent:
        """Convert ORM model to frozen dataclass."""
        return ReputationEvent(
            id=model.id,
            rater_agent_id=model.rater_agent_id,
            rated_agent_id=model.rated_agent_id,
            exchange_id=model.exchange_id,
            zone_id=model.zone_id,
            event_type=model.event_type,
            outcome=model.outcome,
            reliability_score=model.reliability_score,
            quality_score=model.quality_score,
            timeliness_score=model.timeliness_score,
            fairness_score=model.fairness_score,
            evidence_hash=model.evidence_hash,
            context=model.context,
            weight=model.weight,
            record_hash=model.record_hash,
            created_at=model.created_at,
        )

    @staticmethod
    def _score_model_to_record(model: ReputationScoreModel) -> ReputationScore:
        """Convert ORM model to frozen dataclass."""
        return ReputationScore(
            agent_id=model.agent_id,
            context=model.context,
            window=model.window,
            reliability_alpha=model.reliability_alpha,
            reliability_beta=model.reliability_beta,
            quality_alpha=model.quality_alpha,
            quality_beta=model.quality_beta,
            timeliness_alpha=model.timeliness_alpha,
            timeliness_beta=model.timeliness_beta,
            fairness_alpha=model.fairness_alpha,
            fairness_beta=model.fairness_beta,
            composite_score=model.composite_score,
            composite_confidence=model.composite_confidence,
            total_interactions=model.total_interactions,
            positive_interactions=model.positive_interactions,
            negative_interactions=model.negative_interactions,
            disputed_interactions=model.disputed_interactions,
            global_trust_score=model.global_trust_score,
            updated_at=model.updated_at,
            zone_id=model.zone_id,
        )


class DuplicateFeedbackError(Exception):
    """Raised when duplicate feedback is submitted for the same exchange+rater."""
