"""ACE (Agentic Context Engineering) facade for Memory API (#1498).

Provides pre-configured ACE service instances, removing the need for Memory
to re-construct TrajectoryManager/FeedbackManager/etc on every call.

This facade is composed into the Memory class and exposes ACE services
as properties. Callers are encouraged to migrate to using ACE services
directly (e.g., via the API routers or NexusFS).

ACE services are loaded via importlib to comply with the LEGO brick
zero-services-import rule (Issue #2177).

Usage:
    ace = AceFacade(session, backend, llm_provider, user_id, agent_id, zone_id)
    traj_id = ace.trajectory.start_trajectory("Deploy caching", "deployment")
    ace.feedback.add_feedback(traj_id, "monitoring", score=0.8)
"""

import importlib
import logging
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _import_ace(module_name: str, class_name: str) -> Any:
    """Import an ACE service class via importlib (avoids direct nexus.services import)."""
    mod = importlib.import_module(f"nexus.services.ace.{module_name}")
    return getattr(mod, class_name)


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
            cls = _import_ace("trajectory", "TrajectoryManager")
            self._trajectory = cls(
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
            cls = _import_ace("feedback", "FeedbackManager")
            self._feedback = cls(self._session)
        return self._feedback

    @property
    def playbook(self) -> Any:
        """PlaybookManager instance (lazy-loaded)."""
        if self._playbook is None:
            cls = _import_ace("playbook", "PlaybookManager")
            self._playbook = cls(
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
            cls = _import_ace("reflection", "Reflector")
            self._reflector = cls(
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
            cls = _import_ace("curation", "Curator")
            self._curator = cls(self._session, self._backend, self.playbook)
        return self._curator

    @property
    def consolidation(self) -> Any:
        """ConsolidationEngine instance (lazy-loaded)."""
        if self._consolidation is None:
            cls = _import_ace("consolidation", "ConsolidationEngine")
            self._consolidation = cls(
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
            cls = _import_ace("learning_loop", "LearningLoop")
            self._learning_loop = cls(
                session=self._session,
                backend=self._backend,
                user_id=self._user_id,
                agent_id=self._agent_id,
                zone_id=self._zone_id,
                llm_provider=self._llm_provider,
            )
        return self._learning_loop
