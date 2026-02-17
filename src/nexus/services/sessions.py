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
    zone_id: str | None = None,
    ttl: timedelta | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> UserSessionModel:
    """Create a new session.

    Args:
        session: Database session
        user_id: User identifier
        agent_id: Optional agent identifier (if agent session)
        zone_id: Organization identifier
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
        zone_id=zone_id,
        expires_at=expires_at,
        ip_address=ip_address,
        user_agent=user_agent,
    )

    session.add(user_session)
    session.flush()

    return user_session


def get_session(
    session: Session, session_id: str, zone_id: str | None = None
) -> UserSessionModel | None:
    """Get session by ID.

    Args:
        session: Database session
        session_id: Session identifier
        zone_id: Zone ID for multi-tenant isolation

    Returns:
        UserSessionModel or None if not found/expired
    """
    query = session.query(UserSessionModel).filter(UserSessionModel.session_id == session_id)
    if zone_id is not None:
        query = query.filter(UserSessionModel.zone_id == zone_id)
    user_session = query.first()

    if not user_session:
        return None

    # Check expiration
    if user_session.is_expired():
        return None

    return user_session


def update_session_activity(session: Session, session_id: str, zone_id: str | None = None) -> bool:
    """Update last_activity timestamp.

    Call this on every request to track session activity.

    Args:
        session: Database session
        session_id: Session identifier
        zone_id: Zone ID for multi-tenant isolation

    Returns:
        True if updated, False if session not found
    """
    query = session.query(UserSessionModel).filter(UserSessionModel.session_id == session_id)
    if zone_id is not None:
        query = query.filter(UserSessionModel.zone_id == zone_id)
    user_session = query.first()

    if not user_session:
        return False

    user_session.last_activity = datetime.now(UTC)
    session.flush()
    return True


def delete_session_resources(
    session: Session, session_id: str, zone_id: str | None = None
) -> dict[str, int]:
    """Delete all resources associated with a session.

    Called when:
    - Session expires (background task)
    - User logs out explicitly

    Args:
        session: Database session
        session_id: Session to clean up
        zone_id: Zone ID for multi-tenant isolation

    Returns:
        Dict with counts: {"workspaces": N, "memories": N, "memory_configs": N}
    """
    counts = {}

    # Delete session-scoped workspace configs
    wc_query = session.query(WorkspaceConfigModel).filter(
        WorkspaceConfigModel.session_id == session_id
    )
    counts["workspace_configs"] = wc_query.delete()

    # Delete session-scoped memory configs
    mc_query = session.query(MemoryConfigModel).filter(MemoryConfigModel.session_id == session_id)
    counts["memory_configs"] = mc_query.delete()

    # Delete session-scoped memories
    mem_query = session.query(MemoryModel).filter(MemoryModel.session_id == session_id)
    if zone_id is not None:
        mem_query = mem_query.filter(MemoryModel.zone_id == zone_id)
    counts["memories"] = mem_query.delete()

    session.flush()
    return counts


def delete_session(session: Session, session_id: str, zone_id: str | None = None) -> bool:
    """Delete session and all session-scoped resources.

    Args:
        session: Database session
        session_id: Session to delete
        zone_id: Zone ID for multi-tenant isolation

    Returns:
        True if deleted, False if not found
    """
    # 1. Delete session-scoped resources
    delete_session_resources(session, session_id, zone_id=zone_id)

    # 2. Delete session
    del_query = session.query(UserSessionModel).filter(UserSessionModel.session_id == session_id)
    if zone_id is not None:
        del_query = del_query.filter(UserSessionModel.zone_id == zone_id)
    result = del_query.delete()

    session.flush()
    return result > 0


def cleanup_expired_sessions(
    session: Session, zone_id: str | None = None
) -> dict[str, int | dict[str, int]]:
    """Background task: Clean up expired sessions.

    Only deletes sessions with expires_at < now.
    Sessions with expires_at=None are preserved (persistent sessions).

    Args:
        session: Database session
        zone_id: Zone ID for multi-tenant isolation

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
    expired_query = session.query(UserSessionModel).filter(
        UserSessionModel.expires_at < datetime.now(UTC)
    )
    if zone_id is not None:
        expired_query = expired_query.filter(UserSessionModel.zone_id == zone_id)
    expired = expired_query.all()

    total_resources = {"workspace_configs": 0, "memory_configs": 0, "memories": 0}

    for user_session in expired:
        # Delete resources
        resource_counts = delete_session_resources(
            session, user_session.session_id, zone_id=zone_id
        )
        for key, count in resource_counts.items():
            total_resources[key] = total_resources.get(key, 0) + count

        # Delete session
        session.delete(user_session)

    session.flush()

    return {"sessions": len(expired), "resources": total_resources}


def list_user_sessions(
    session: Session,
    user_id: str,
    include_expired: bool = False,
    zone_id: str | None = None,
) -> list[UserSessionModel]:
    """List all sessions for a user.

    Args:
        session: Database session
        user_id: User identifier
        include_expired: Include expired sessions
        zone_id: Zone ID for multi-tenant isolation

    Returns:
        List of UserSessionModel
    """
    query = session.query(UserSessionModel).filter(UserSessionModel.user_id == user_id)
    if zone_id is not None:
        query = query.filter(UserSessionModel.zone_id == zone_id)

    if not include_expired:
        # Filter out expired sessions
        query = query.filter(
            (UserSessionModel.expires_at.is_(None))
            | (UserSessionModel.expires_at > datetime.now(UTC))
        )

    return list(query.all())


def cleanup_inactive_sessions(
    session: Session,
    inactive_threshold: timedelta = timedelta(days=30),
    zone_id: str | None = None,
) -> int:
    """Clean up sessions inactive for threshold period.

    Optional: Clean up sessions that haven't been used in N days,
    even if they haven't expired.

    Args:
        session: Database session
        inactive_threshold: Inactivity period (default: 30 days)
        zone_id: Zone ID for multi-tenant isolation

    Returns:
        Number of sessions deleted
    """
    cutoff = datetime.now(UTC) - inactive_threshold

    # Collect all inactive session IDs in a single query
    inactive_query = session.query(UserSessionModel.session_id).filter(
        UserSessionModel.last_activity < cutoff
    )
    if zone_id is not None:
        inactive_query = inactive_query.filter(UserSessionModel.zone_id == zone_id)
    inactive_ids = [row[0] for row in inactive_query.all()]

    if not inactive_ids:
        return 0

    # Bulk-delete related resources (avoids N+1 per-session queries)
    session.query(WorkspaceConfigModel).filter(
        WorkspaceConfigModel.session_id.in_(inactive_ids)
    ).delete(synchronize_session=False)

    session.query(MemoryConfigModel).filter(MemoryConfigModel.session_id.in_(inactive_ids)).delete(
        synchronize_session=False
    )

    session.query(MemoryModel).filter(MemoryModel.session_id.in_(inactive_ids)).delete(
        synchronize_session=False
    )

    session.query(UserSessionModel).filter(UserSessionModel.session_id.in_(inactive_ids)).delete(
        synchronize_session=False
    )

    session.flush()
    return len(inactive_ids)
