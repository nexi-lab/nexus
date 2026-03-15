"""Session management for Nexus.

Manages user sessions backed by CacheStore (Dragonfly/In-Memory) with support for:
- Temporary sessions (with TTL)
- Persistent sessions (no TTL, "Remember me")
- Session-scoped resource cleanup (PathRegistration, Memory in RecordStore)
- Background cleanup task

Migrated from SQLAlchemy ORM (UserSessionModel) to CacheStoreABC per
data-storage-matrix.md Part 6: sessions are ephemeral KV with TTL.
"""

from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session as DBSession

from nexus.contracts.auth_store_types import SessionDTO
from nexus.storage.auth_stores.cache_session_store import CacheSessionStore


async def create_session(
    store: CacheSessionStore,
    user_id: str,
    agent_id: str | None = None,
    zone_id: str | None = None,
    ttl: timedelta | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> SessionDTO:
    """Create a new session in CacheStore.

    Args:
        store: CacheSessionStore instance
        user_id: User identifier
        agent_id: Optional agent identifier
        zone_id: Organization identifier
        ttl: Time-to-live (None = persistent session)
        ip_address: Client IP
        user_agent: Client user agent

    Returns:
        SessionDTO
    """
    ttl_seconds = int(ttl.total_seconds()) if ttl else None
    return await store.create(
        user_id=user_id,
        agent_id=agent_id,
        zone_id=zone_id,
        ttl_seconds=ttl_seconds,
        ip_address=ip_address,
        user_agent=user_agent,
    )


async def get_session(store: CacheSessionStore, session_id: str) -> SessionDTO | None:
    """Get session by ID. Returns None if not found or expired."""
    return await store.get(session_id)


async def update_session_activity(store: CacheSessionStore, session_id: str) -> bool:
    """Update last_activity timestamp. Returns False if not found."""
    return await store.update_activity(session_id)


def delete_session_resources(db_session: "DBSession", session_id: str) -> dict[str, int]:
    """Delete all RecordStore resources associated with a session.

    PathRegistrationModel and MemoryModel stay in RecordStore — only
    the session record itself moved to CacheStore.
    """
    from sqlalchemy import delete

    from nexus.storage.models import MemoryModel, PathRegistrationModel

    counts: dict[str, int] = {}

    ws_result: Any = db_session.execute(
        delete(PathRegistrationModel).where(PathRegistrationModel.session_id == session_id)
    )
    counts["workspace_configs"] = ws_result.rowcount

    mem_result: Any = db_session.execute(
        delete(MemoryModel).where(MemoryModel.session_id == session_id)
    )
    counts["memories"] = mem_result.rowcount

    db_session.flush()
    return counts


async def delete_session(
    store: CacheSessionStore,
    db_session: "DBSession",
    session_id: str,
) -> bool:
    """Delete session and all session-scoped resources.

    Args:
        store: CacheSessionStore instance
        db_session: SQLAlchemy session (for resource cleanup in RecordStore)
        session_id: Session to delete

    Returns:
        True if deleted, False if not found
    """
    delete_session_resources(db_session, session_id)
    return await store.delete(session_id)


async def cleanup_expired_sessions(
    store: CacheSessionStore,
    db_session: "DBSession",
) -> dict[str, int | dict[str, int]]:
    """Clean up expired sessions and their associated resources.

    Finds expired sessions in CacheStore, deletes associated
    RecordStore resources, then removes the sessions.
    """
    expired = await store.find_expired()

    total_resources: dict[str, int] = {"workspace_configs": 0, "memories": 0}

    for dto in expired:
        resource_counts = delete_session_resources(db_session, dto.session_id)
        for key, count in resource_counts.items():
            total_resources[key] = total_resources.get(key, 0) + count
        await store.delete(dto.session_id)

    db_session.flush()
    return {"sessions": len(expired), "resources": total_resources}


async def list_user_sessions(
    store: CacheSessionStore,
    user_id: str,
    include_expired: bool = False,
) -> list[SessionDTO]:
    """List all sessions for a user."""
    return await store.list_for_user(user_id, include_expired=include_expired)


async def cleanup_inactive_sessions(
    store: CacheSessionStore,
    db_session: "DBSession",
    inactive_threshold: timedelta = timedelta(days=30),
) -> int:
    """Clean up sessions inactive for threshold period.

    Deletes associated RecordStore resources, then removes sessions from CacheStore.
    """
    from sqlalchemy import delete

    from nexus.storage.models import MemoryModel, PathRegistrationModel

    inactive = await store.find_inactive(inactive_threshold)
    if not inactive:
        return 0

    inactive_ids = [dto.session_id for dto in inactive]

    # Bulk-delete related resources in RecordStore
    db_session.execute(
        delete(PathRegistrationModel).where(PathRegistrationModel.session_id.in_(inactive_ids))
    )
    db_session.execute(delete(MemoryModel).where(MemoryModel.session_id.in_(inactive_ids)))

    # Delete sessions from CacheStore
    for sid in inactive_ids:
        await store.delete(sid)

    db_session.flush()
    return len(inactive_ids)
