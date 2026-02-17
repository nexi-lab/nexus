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

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from nexus.services.ace.consolidation import ConsolidationEngine
    from nexus.services.ace.curation import Curator
    from nexus.services.ace.feedback import FeedbackManager
    from nexus.services.ace.learning_loop import LearningLoop
    from nexus.services.ace.playbook import PlaybookManager
    from nexus.services.ace.reflection import Reflector
    from nexus.services.ace.trajectory import TrajectoryManager

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
        self._trajectory: TrajectoryManager | None = None
        self._feedback: FeedbackManager | None = None
        self._playbook: PlaybookManager | None = None
        self._reflector: Reflector | None = None
        self._curator: Curator | None = None
        self._consolidation: ConsolidationEngine | None = None
        self._learning_loop: LearningLoop | None = None

    @property
    def trajectory(self) -> "TrajectoryManager":
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
    def feedback(self) -> "FeedbackManager":
        """FeedbackManager instance (lazy-loaded)."""
        if self._feedback is None:
            from nexus.services.ace.feedback import FeedbackManager

            self._feedback = FeedbackManager(self._session, zone_id=self._zone_id)
        return self._feedback

    @property
    def playbook(self) -> "PlaybookManager":
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
    def reflector(self) -> "Reflector":
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
    def curator(self) -> "Curator":
        """Curator instance (lazy-loaded)."""
        if self._curator is None:
            from nexus.services.ace.curation import Curator

            self._curator = Curator(self._session, self._backend, self.playbook)
        return self._curator

    @property
    def consolidation(self) -> "ConsolidationEngine":
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
    def learning_loop(self) -> "LearningLoop":
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
