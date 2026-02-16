"""ACE (Agentic Context Engineering) facade for Memory API (#1498).

Provides pre-configured ACE service instances, removing the need for Memory
to re-construct TrajectoryManager/FeedbackManager/etc on every call.

This facade is composed into the Memory class and exposes ACE services
as properties. Callers are encouraged to migrate to using ACE services
directly (e.g., via the API routers or NexusFS).

Usage:
    ace = AceFacade(session, backend, llm_provider, user_id, agent_id, zone_id)
    traj_id = ace.trajectory.start_trajectory("Deploy caching", "deployment")
    ace.feedback.add_feedback(traj_id, "monitoring", score=0.8)
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class AceFacade:
    """Lazy-initialized facade for ACE subsystem services.

    Service instances are created on first access and cached.
    This avoids import overhead when ACE features aren't used.
    """

    def __init__(
        self,
        session: Session,
        backend: Any,
        llm_provider: Any,
        user_id: str,
        agent_id: str | None,
        zone_id: str | None,
    ) -> None:
        self._session = session
        self._backend = backend
        self._llm_provider = llm_provider
        self._user_id = user_id
        self._agent_id = agent_id
        self._zone_id = zone_id

        # Lazy-cached service instances
        self._trajectory: Any = None
        self._feedback: Any = None
        self._playbook: Any = None
        self._reflector: Any = None
        self._curator: Any = None
        self._consolidation: Any = None
        self._learning_loop: Any = None

    @property
    def trajectory(self) -> Any:
        """TrajectoryManager instance (lazy-loaded)."""
        if self._trajectory is None:
            from nexus.services.ace.trajectory import TrajectoryManager

            self._trajectory = TrajectoryManager(
                self._session,
                self._backend,
                self._user_id,
                self._agent_id,
                self._zone_id,
            )
        return self._trajectory

    @property
    def feedback(self) -> Any:
        """FeedbackManager instance (lazy-loaded)."""
        if self._feedback is None:
            from nexus.services.ace.feedback import FeedbackManager

            self._feedback = FeedbackManager(self._session)
        return self._feedback

    @property
    def playbook(self) -> Any:
        """PlaybookManager instance (lazy-loaded)."""
        if self._playbook is None:
            from nexus.services.ace.playbook import PlaybookManager

            self._playbook = PlaybookManager(
                self._session,
                self._backend,
                self._user_id,
                self._agent_id,
                self._zone_id,
            )
        return self._playbook

    @property
    def reflector(self) -> Any:
        """Reflector instance (lazy-loaded)."""
        if self._reflector is None:
            from nexus.services.ace.reflection import Reflector

            self._reflector = Reflector(
                self._session,
                self._backend,
                self._llm_provider,
                self.trajectory,
                self._user_id,
                self._agent_id,
                self._zone_id,
            )
        return self._reflector

    @property
    def curator(self) -> Any:
        """Curator instance (lazy-loaded)."""
        if self._curator is None:
            from nexus.services.ace.curation import Curator

            self._curator = Curator(self._session, self._backend, self.playbook)
        return self._curator

    @property
    def consolidation(self) -> Any:
        """ConsolidationEngine instance (lazy-loaded)."""
        if self._consolidation is None:
            from nexus.services.ace.consolidation import ConsolidationEngine

            self._consolidation = ConsolidationEngine(
                self._session,
                self._backend,
                self._llm_provider,
                self._user_id,
                self._agent_id,
                self._zone_id,
            )
        return self._consolidation

    @property
    def learning_loop(self) -> Any:
        """LearningLoop instance (lazy-loaded)."""
        if self._learning_loop is None:
            from nexus.services.ace.learning_loop import LearningLoop

            self._learning_loop = LearningLoop(
                session=self._session,
                backend=self._backend,
                user_id=self._user_id,
                agent_id=self._agent_id,
                zone_id=self._zone_id,
                llm_provider=self._llm_provider,
            )
        return self._learning_loop
