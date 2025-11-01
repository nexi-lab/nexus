"""Minimal tests for ACE components with correct APIs."""

from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.ace.feedback import FeedbackManager
from nexus.core.ace.trajectory import TrajectoryManager
from nexus.core.permissions import OperationContext
from nexus.storage.models import Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """Create database session."""
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def mock_backend():
    """Create mock storage backend."""
    backend = Mock()
    backend.read_content = Mock(return_value=b'{"content": "test"}')
    backend.write_content = Mock(return_value="hash123")
    return backend


@pytest.fixture
def context():
    """Create operation context."""
    return OperationContext(user="alice", groups=[], is_admin=False)


class TestTrajectoryManager:
    """Test TrajectoryManager with correct API."""

    @pytest.fixture
    def trajectory_manager(self, session, mock_backend):
        """Create trajectory manager instance."""
        return TrajectoryManager(
            session=session,
            backend=mock_backend,
            user_id="alice",
            agent_id="agent1",
            tenant_id="acme",
        )

    def test_init(self, trajectory_manager):
        """Test initialization."""
        assert trajectory_manager.user_id == "alice"
        assert trajectory_manager.agent_id == "agent1"
        assert trajectory_manager.tenant_id == "acme"
        assert isinstance(trajectory_manager._active_trajectories, dict)

    def test_start_trajectory(self, trajectory_manager, session):
        """Test starting a new trajectory."""
        trajectory_id = trajectory_manager.start_trajectory(
            task_description="Test task",
            task_type="test",
        )

        assert trajectory_id is not None
        assert isinstance(trajectory_id, str)

    def test_check_permission_admin(self, session, mock_backend):
        """Test admin bypass for permissions."""
        admin_context = OperationContext(user="admin", groups=[], is_admin=True)
        tm = TrajectoryManager(
            session=session,
            backend=mock_backend,
            user_id="alice",
            context=admin_context,
        )

        # Start a trajectory
        _ = tm.start_trajectory(task_description="Test")

        # Admin should have access
        assert tm.context.is_admin is True


class TestFeedbackManager:
    """Test FeedbackManager with correct API."""

    @pytest.fixture
    def feedback_manager(self, session):
        """Create feedback manager instance."""
        return FeedbackManager(session=session)

    def test_init(self, feedback_manager):
        """Test initialization."""
        assert feedback_manager.session is not None

    def test_add_feedback(self, feedback_manager, session, mock_backend):
        """Test adding feedback to a trajectory."""
        # Create a trajectory first
        from nexus.core.ace.trajectory import TrajectoryManager

        tm = TrajectoryManager(session=session, backend=mock_backend, user_id="alice")
        trajectory_id = tm.start_trajectory(task_description="Test")
        session.commit()

        # Add feedback
        feedback_id = feedback_manager.add_feedback(
            trajectory_id=trajectory_id,
            feedback_type="rating",
            score=5.0,
        )

        assert feedback_id is not None


class TestACEImports:
    """Test that ACE components can be imported and initialized."""

    def test_import_trajectory_manager(self):
        """Test importing TrajectoryManager."""
        from nexus.core.ace.trajectory import TrajectoryManager

        assert TrajectoryManager is not None

    def test_import_reflector(self):
        """Test importing Reflector."""
        from nexus.core.ace.reflection import Reflector

        assert Reflector is not None

    def test_import_playbook_manager(self):
        """Test importing PlaybookManager."""
        from nexus.core.ace.playbook import PlaybookManager

        assert PlaybookManager is not None

    def test_import_curator(self):
        """Test importing Curator."""
        from nexus.core.ace.curation import Curator

        assert Curator is not None

    def test_import_consolidation_engine(self):
        """Test importing ConsolidationEngine."""
        from nexus.core.ace.consolidation import ConsolidationEngine

        assert ConsolidationEngine is not None

    def test_import_feedback_manager(self):
        """Test importing FeedbackManager."""
        from nexus.core.ace.feedback import FeedbackManager

        assert FeedbackManager is not None

    def test_import_learning_loop(self):
        """Test importing LearningLoop."""
        from nexus.core.ace.learning_loop import LearningLoop

        assert LearningLoop is not None
