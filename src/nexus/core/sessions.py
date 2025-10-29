"""Session management for Nexus (v0.5.0).

Manages user sessions with support for:
- Temporary sessions (with TTL)
- Persistent sessions (no TTL, "Remember me")
- Session-scoped resources (auto-cleanup)
- Background cleanup task

See: docs/design/AGENT_IDENTITY_AND_SESSIONS.md
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from nexus.storage.models import (
    MemoryConfigModel,
    MemoryModel,
    UserSessionModel,
    WorkspaceConfigModel,
)


def create_session(
    session: Session,
    user_id: str,
    agent_id: str | None = None,
    tenant_id: str | None = None,
    ttl: timedelta | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> UserSessionModel:
    """Create a new session.

    Args:
        session: Database session
        user_id: User identifier
        agent_id: Optional agent identifier (if agent session)
        tenant_id: Organization identifier
        ttl: Time-to-live (None = persistent session, "Remember me")
        ip_address: Client IP
        user_agent: Client user agent

    Returns:
        UserSessionModel

    Examples:
        >>> # Temporary session (8 hours)
        >>> sess = create_session(
        ...     db,
        ...     user_id="alice",
        ...     ttl=timedelta(hours=8)
        ... )

        >>> # Persistent session ("Remember me")
        >>> sess = create_session(
        ...     db,
        ...     user_id="alice",
        ...     ttl=None
        ... )
    """
    expires_at = None
    if ttl:
        expires_at = datetime.now(UTC) + ttl

    user_session = UserSessionModel(
        user_id=user_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        expires_at=expires_at,
        ip_address=ip_address,
        user_agent=user_agent,
    )

    session.add(user_session)
    session.flush()

    return user_session


def get_session(session: Session, session_id: str) -> UserSessionModel | None:
    """Get session by ID.

    Args:
        session: Database session
        session_id: Session identifier

    Returns:
        UserSessionModel or None if not found/expired
    """
    user_session = (
        session.query(UserSessionModel).filter(UserSessionModel.session_id == session_id).first()
    )

    if not user_session:
        return None

    # Check expiration
    if user_session.is_expired():
        return None

    return user_session


def update_session_activity(session: Session, session_id: str) -> bool:
    """Update last_activity timestamp.

    Call this on every request to track session activity.

    Args:
        session: Database session
        session_id: Session identifier

    Returns:
        True if updated, False if session not found
    """
    user_session = (
        session.query(UserSessionModel).filter(UserSessionModel.session_id == session_id).first()
    )

    if not user_session:
        return False

    user_session.last_activity = datetime.now(UTC)
    session.flush()
    return True


def delete_session_resources(session: Session, session_id: str) -> dict[str, int]:
    """Delete all resources associated with a session.

    Called when:
    - Session expires (background task)
    - User logs out explicitly

    Args:
        session: Database session
        session_id: Session to clean up

    Returns:
        Dict with counts: {"workspaces": N, "memories": N, "memory_configs": N}
    """
    counts = {}

    # Delete session-scoped workspace configs
    counts["workspace_configs"] = (
        session.query(WorkspaceConfigModel)
        .filter(WorkspaceConfigModel.session_id == session_id)
        .delete()
    )

    # Delete session-scoped memory configs
    counts["memory_configs"] = (
        session.query(MemoryConfigModel).filter(MemoryConfigModel.session_id == session_id).delete()
    )

    # Delete session-scoped memories
    counts["memories"] = (
        session.query(MemoryModel).filter(MemoryModel.session_id == session_id).delete()
    )

    session.flush()
    return counts


def delete_session(session: Session, session_id: str) -> bool:
    """Delete session and all session-scoped resources.

    Args:
        session: Database session
        session_id: Session to delete

    Returns:
        True if deleted, False if not found
    """
    # 1. Delete session-scoped resources
    delete_session_resources(session, session_id)

    # 2. Delete session
    result = (
        session.query(UserSessionModel).filter(UserSessionModel.session_id == session_id).delete()
    )

    session.flush()
    return result > 0


def cleanup_expired_sessions(session: Session) -> dict[str, int | dict[str, int]]:
    """Background task: Clean up expired sessions.

    Only deletes sessions with expires_at < now.
    Sessions with expires_at=None are preserved (persistent sessions).

    Args:
        session: Database session

    Returns:
        Dict with counts: {"sessions": N, "resources": {...}}

    Examples:
        >>> # Run as background task (every hour)
        >>> with SessionLocal() as db:
        ...     result = cleanup_expired_sessions(db)
        ...     db.commit()
        ...     print(f"Cleaned up {result['sessions']} sessions")
    """
    # Find expired sessions
    expired = (
        session.query(UserSessionModel)
        .filter(UserSessionModel.expires_at < datetime.now(UTC))
        .all()
    )

    total_resources = {"workspace_configs": 0, "memory_configs": 0, "memories": 0}

    for user_session in expired:
        # Delete resources
        resource_counts = delete_session_resources(session, user_session.session_id)
        for key, count in resource_counts.items():
            total_resources[key] = total_resources.get(key, 0) + count

        # Delete session
        session.delete(user_session)

    session.flush()

    return {"sessions": len(expired), "resources": total_resources}


def list_user_sessions(
    session: Session, user_id: str, include_expired: bool = False
) -> list[UserSessionModel]:
    """List all sessions for a user.

    Args:
        session: Database session
        user_id: User identifier
        include_expired: Include expired sessions

    Returns:
        List of UserSessionModel
    """
    query = session.query(UserSessionModel).filter(UserSessionModel.user_id == user_id)

    if not include_expired:
        # Filter out expired sessions
        query = query.filter(
            (UserSessionModel.expires_at.is_(None))
            | (UserSessionModel.expires_at > datetime.now(UTC))
        )

    return list(query.all())


def cleanup_inactive_sessions(
    session: Session, inactive_threshold: timedelta = timedelta(days=30)
) -> int:
    """Clean up sessions inactive for threshold period.

    Optional: Clean up sessions that haven't been used in N days,
    even if they haven't expired.

    Args:
        session: Database session
        inactive_threshold: Inactivity period (default: 30 days)

    Returns:
        Number of sessions deleted
    """
    cutoff = datetime.now(UTC) - inactive_threshold

    inactive = session.query(UserSessionModel).filter(UserSessionModel.last_activity < cutoff).all()

    count = 0
    for user_session in inactive:
        delete_session(session, user_session.session_id)
        count += 1

    session.flush()
    return count
