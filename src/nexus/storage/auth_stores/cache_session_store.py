"""CacheSessionStore — session storage backed by CacheStoreABC.

Migrates UserSessionModel from RecordStore (SQLAlchemy) to CacheStore
(Dragonfly/In-Memory) per data-storage-matrix.md Part 6:
"Sessions are ephemeral KV with TTL, no relational features needed."

Key format: session:{session_id} → JSON blob
TTL: native CacheStore expiry for temporary sessions
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from nexus.contracts.auth_store_types import SessionDTO
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.contracts.cache_store import CacheStoreABC

KEY_PREFIX = "session:"


class CacheSessionStore:
    """Session store backed by CacheStoreABC (Dragonfly/In-Memory).

    Provides session CRUD with native TTL expiry. Admin queries
    (list all sessions for a user) use CacheStore pattern scan.
    """

    def __init__(self, cache: "CacheStoreABC") -> None:
        self._cache = cache

    @staticmethod
    def _key(session_id: str) -> str:
        return f"{KEY_PREFIX}{session_id}"

    @staticmethod
    def _serialize(dto: SessionDTO) -> bytes:
        data = {
            "session_id": dto.session_id,
            "user_id": dto.user_id,
            "agent_id": dto.agent_id,
            "zone_id": dto.zone_id,
            "created_at": dto.created_at.isoformat() if dto.created_at else None,
            "expires_at": dto.expires_at.isoformat() if dto.expires_at else None,
            "last_activity": dto.last_activity.isoformat() if dto.last_activity else None,
            "ip_address": dto.ip_address,
            "user_agent": dto.user_agent,
        }
        return json.dumps(data).encode()

    @staticmethod
    def _deserialize(raw: bytes) -> SessionDTO:
        data = json.loads(raw)
        return SessionDTO(
            session_id=data["session_id"],
            user_id=data["user_id"],
            agent_id=data.get("agent_id"),
            zone_id=data.get("zone_id", ROOT_ZONE_ID),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at")
            else None,
            expires_at=datetime.fromisoformat(data["expires_at"])
            if data.get("expires_at")
            else None,
            last_activity=(
                datetime.fromisoformat(data["last_activity"]) if data.get("last_activity") else None
            ),
            ip_address=data.get("ip_address"),
            user_agent=data.get("user_agent"),
        )

    async def create(
        self,
        user_id: str,
        agent_id: str | None = None,
        zone_id: str | None = None,
        ttl_seconds: int | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> SessionDTO:
        """Create a new session in CacheStore."""
        session_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds) if ttl_seconds else None

        dto = SessionDTO(
            session_id=session_id,
            user_id=user_id,
            agent_id=agent_id,
            zone_id=zone_id or ROOT_ZONE_ID,
            created_at=now,
            expires_at=expires_at,
            last_activity=now,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self._cache.set(self._key(session_id), self._serialize(dto), ttl=ttl_seconds)
        return dto

    async def get(self, session_id: str) -> SessionDTO | None:
        """Get session by ID. Returns None if not found or expired."""
        raw = await self._cache.get(self._key(session_id))
        if raw is None:
            return None
        dto = self._deserialize(raw)
        if dto.is_expired():
            await self._cache.delete(self._key(session_id))
            return None
        return dto

    async def update_activity(self, session_id: str) -> bool:
        """Update last_activity timestamp. Returns False if session not found."""
        raw = await self._cache.get(self._key(session_id))
        if raw is None:
            return False
        dto = self._deserialize(raw)
        now = datetime.now(UTC)
        updated = SessionDTO(
            session_id=dto.session_id,
            user_id=dto.user_id,
            agent_id=dto.agent_id,
            zone_id=dto.zone_id,
            created_at=dto.created_at,
            expires_at=dto.expires_at,
            last_activity=now,
            ip_address=dto.ip_address,
            user_agent=dto.user_agent,
        )
        # Preserve remaining TTL
        ttl = None
        if updated.expires_at:
            remaining = (updated.expires_at - now).total_seconds()
            ttl = max(1, int(remaining))
        await self._cache.set(self._key(session_id), self._serialize(updated), ttl=ttl)
        return True

    async def delete(self, session_id: str) -> bool:
        """Delete a session. Returns True if it existed."""
        return await self._cache.delete(self._key(session_id))

    async def list_for_user(self, user_id: str, include_expired: bool = False) -> list[SessionDTO]:
        """List all sessions for a user (pattern scan — rare admin operation)."""
        keys = await self._cache.keys_by_pattern(f"{KEY_PREFIX}*")
        results: list[SessionDTO] = []
        for key in keys:
            raw = await self._cache.get(key)
            if raw is None:
                continue
            dto = self._deserialize(raw)
            if dto.user_id != user_id:
                continue
            if not include_expired and dto.is_expired():
                continue
            results.append(dto)
        return results

    async def find_expired(self) -> list[SessionDTO]:
        """Find all expired sessions (for resource cleanup)."""
        keys = await self._cache.keys_by_pattern(f"{KEY_PREFIX}*")
        expired: list[SessionDTO] = []
        for key in keys:
            raw = await self._cache.get(key)
            if raw is None:
                continue
            dto = self._deserialize(raw)
            if dto.is_expired():
                expired.append(dto)
        return expired

    async def find_inactive(self, threshold: timedelta) -> list[SessionDTO]:
        """Find sessions inactive for longer than threshold."""
        keys = await self._cache.keys_by_pattern(f"{KEY_PREFIX}*")
        inactive: list[SessionDTO] = []
        cutoff = datetime.now(UTC) - threshold
        for key in keys:
            raw = await self._cache.get(key)
            if raw is None:
                continue
            dto = self._deserialize(raw)
            if dto.last_activity and dto.last_activity < cutoff:
                inactive.append(dto)
        return inactive
