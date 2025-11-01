"""Simplified tests for session management module."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.sessions import (
    cleanup_expired_sessions,
    cleanup_inactive_sessions,
    create_session,
    delete_session,
    delete_session_resources,
    get_session,
    list_user_sessions,
    update_session_activity,
)
from nexus.storage.models import Base, MemoryModel, UserSessionModel


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


class TestCreateSession:
    """Test create_session function."""

    def test_create_temporary_session(self, session):
        """Test creating a temporary session with TTL."""
        user_session = create_session(
            session,
            user_id="alice",
            ttl=timedelta(hours=8),
            ip_address="127.0.0.1",
            user_agent="Mozilla/5.0",
        )

        assert user_session is not None
        assert user_session.session_id is not None
        assert user_session.user_id == "alice"
        assert user_session.expires_at is not None
        assert user_session.ip_address == "127.0.0.1"
        assert user_session.user_agent == "Mozilla/5.0"
        assert user_session.agent_id is None

    def test_create_persistent_session(self, session):
        """Test creating a persistent session without TTL."""
        user_session = create_session(
            session,
            user_id="alice",
            ttl=None,  # Persistent session
        )

        assert user_session is not None
        assert user_session.expires_at is None  # No expiration

    def test_create_agent_session(self, session):
        """Test creating a session for an agent."""
        user_session = create_session(
            session,
            user_id="alice",
            agent_id="agent1",
            ttl=timedelta(hours=1),
        )

        assert user_session is not None
        assert user_session.agent_id == "agent1"
        assert user_session.user_id == "alice"

    def test_create_session_with_tenant(self, session):
        """Test creating a session with tenant ID."""
        user_session = create_session(
            session,
            user_id="alice",
            tenant_id="acme",
            ttl=timedelta(hours=8),
        )

        assert user_session is not None
        assert user_session.tenant_id == "acme"

    def test_session_auto_generates_id(self, session):
        """Test that session ID is auto-generated."""
        sess1 = create_session(session, user_id="alice")
        sess2 = create_session(session, user_id="bob")

        assert sess1.session_id != sess2.session_id


class TestUpdateSessionActivity:
    """Test update_session_activity function."""

    def test_update_activity_success(self, session):
        """Test updating session activity timestamp."""
        user_session = create_session(session, user_id="alice")
        session.commit()

        original_activity = user_session.last_activity

        # Wait a tiny bit to ensure timestamp changes
        import time

        time.sleep(0.01)

        success = update_session_activity(session, user_session.session_id)

        assert success is True
        session.refresh(user_session)
        assert user_session.last_activity > original_activity

    def test_update_activity_nonexistent_session(self, session):
        """Test updating activity for non-existent session."""
        success = update_session_activity(session, "nonexistent-session-id")

        assert success is False


class TestDeleteSessionResources:
    """Test delete_session_resources function."""

    def test_delete_memories(self, session):
        """Test deleting session-scoped memories."""
        user_session = create_session(session, user_id="alice")
        session.commit()

        # Create memories
        memory = MemoryModel(
            content_hash="abc123",
            session_id=user_session.session_id,
            user_id="alice",
        )
        session.add(memory)
        session.commit()

        counts = delete_session_resources(session, user_session.session_id)

        assert counts["memories"] == 1


class TestDeleteSession:
    """Test delete_session function."""

    def test_delete_existing_session(self, session):
        """Test deleting an existing session."""
        user_session = create_session(session, user_id="alice")
        session.commit()

        success = delete_session(session, user_session.session_id)

        assert success is True

        # Verify session is deleted
        retrieved = (
            session.query(UserSessionModel).filter_by(session_id=user_session.session_id).first()
        )
        assert retrieved is None

    def test_delete_nonexistent_session(self, session):
        """Test deleting a non-existent session."""
        success = delete_session(session, "nonexistent-session-id")

        assert success is False


class TestCleanupExpiredSessions:
    """Test cleanup_expired_sessions function."""

    def test_cleanup_expired_sessions(self, session):
        """Test cleaning up expired sessions."""
        # Create expired session
        expired_session = UserSessionModel(
            user_id="alice",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(expired_session)
        session.commit()

        result = cleanup_expired_sessions(session)

        assert result["sessions"] == 1

        # Verify session is deleted
        remaining = (
            session.query(UserSessionModel).filter_by(session_id=expired_session.session_id).first()
        )
        assert remaining is None

    def test_cleanup_preserves_valid_sessions(self, session):
        """Test that valid sessions are preserved."""
        # Create valid session
        valid_session = create_session(session, user_id="alice", ttl=timedelta(hours=8))
        session.commit()

        result = cleanup_expired_sessions(session)

        assert result["sessions"] == 0

        # Verify session still exists
        remaining = (
            session.query(UserSessionModel).filter_by(session_id=valid_session.session_id).first()
        )
        assert remaining is not None

    def test_cleanup_preserves_persistent_sessions(self, session):
        """Test that persistent sessions (expires_at=None) are preserved."""
        # Create persistent session
        persistent_session = create_session(session, user_id="alice", ttl=None)
        session.commit()

        result = cleanup_expired_sessions(session)

        assert result["sessions"] == 0

        # Verify session still exists
        remaining = (
            session.query(UserSessionModel)
            .filter_by(session_id=persistent_session.session_id)
            .first()
        )
        assert remaining is not None


class TestListUserSessions:
    """Test list_user_sessions function."""

    def test_list_user_sessions(self, session):
        """Test listing all sessions for a user."""
        # Create sessions for alice
        sess1 = create_session(session, user_id="alice", ttl=timedelta(hours=8))
        sess2 = create_session(session, user_id="alice", ttl=None)

        # Create session for bob
        create_session(session, user_id="bob", ttl=timedelta(hours=8))

        session.commit()

        alice_sessions = list_user_sessions(session, user_id="alice")

        assert len(alice_sessions) == 2
        session_ids = {s.session_id for s in alice_sessions}
        assert sess1.session_id in session_ids
        assert sess2.session_id in session_ids

    def test_list_includes_persistent_sessions(self, session):
        """Test that persistent sessions are included."""
        # Create persistent session
        create_session(session, user_id="alice", ttl=None)

        # Create temporary session
        create_session(session, user_id="alice", ttl=timedelta(hours=8))

        session.commit()

        alice_sessions = list_user_sessions(session, user_id="alice")

        assert len(alice_sessions) == 2


class TestCleanupInactiveSessions:
    """Test cleanup_inactive_sessions function."""

    def test_cleanup_inactive_sessions(self, session):
        """Test cleaning up inactive sessions."""
        # Create inactive session
        inactive = UserSessionModel(
            user_id="alice",
            last_activity=datetime.now(UTC) - timedelta(days=31),
        )
        session.add(inactive)
        session.commit()

        count = cleanup_inactive_sessions(session, inactive_threshold=timedelta(days=30))

        assert count == 1

        # Verify session is deleted
        remaining = (
            session.query(UserSessionModel).filter_by(session_id=inactive.session_id).first()
        )
        assert remaining is None

    def test_cleanup_preserves_active_sessions(self, session):
        """Test that active sessions are preserved."""
        # Create active session
        active = create_session(session, user_id="alice")
        session.commit()

        count = cleanup_inactive_sessions(session, inactive_threshold=timedelta(days=30))

        assert count == 0

        # Verify session still exists
        remaining = session.query(UserSessionModel).filter_by(session_id=active.session_id).first()
        assert remaining is not None


class TestSessionBasics:
    """Basic session tests."""

    def test_session_creation_and_retrieval(self, session):
        """Test basic session workflow."""
        # Create session
        user_session = create_session(session, user_id="alice")
        session.commit()

        # Retrieve session
        retrieved = get_session(session, user_session.session_id)

        assert retrieved is not None
        assert retrieved.session_id == user_session.session_id
        assert retrieved.user_id == "alice"

    def test_multi_user_session_isolation(self, session):
        """Test that sessions are properly isolated between users."""
        # Create sessions for different users
        _ = create_session(session, user_id="alice")
        _ = create_session(session, user_id="bob")
        session.commit()

        # List sessions for each user
        alice_sessions = list_user_sessions(session, user_id="alice")
        bob_sessions = list_user_sessions(session, user_id="bob")

        assert len(alice_sessions) == 1
        assert len(bob_sessions) == 1
        assert alice_sessions[0].session_id != bob_sessions[0].session_id
